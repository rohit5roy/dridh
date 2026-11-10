"""The Executor run controller (decisions D-P15-01..06).

Python owns every story status and every run state change; the model only edits files in a story's worktree.
A run starts only after a human approved the plan *and* typed a per-run confirmation. Stories run in dependency
order, at most ``max_parallel`` at a time. A story that passes its tests moves to ``IN_REVIEW``; its dependents
start only once it is ``COMPLETED`` (released by a human whole-lot review, see ``review_lot``). A story that cannot be
repaired within its budget becomes ``BLOCKED`` with its evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.config.dridh_yaml import ExecutionConfig, load_config
from dridh.coordinator.lifecycle import FeatureLifecycle
from dridh.executor.story_runner import AGENT, StoryOutcome, StoryRunner
from dridh.integrations.opencode import OpenCodeRunner
from dridh.planner.service import PlannerService
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo

try:
    import fcntl
except ImportError:  # Windows: no cross-process driver lock
    fcntl = None  # type: ignore[assignment]

TERMINAL = ("COMPLETED", "STOPPED", "FAILED")


class ExecutionController:
    def __init__(self, workspace: WorkspaceInfo, *, planner: PlannerService | None = None, runner: Any = None,
                 story_runner: StoryRunner | None = None, model: str | None = None, settings: ExecutionConfig | None = None, delivery: Any = None) -> None:
        self.workspace = workspace
        self.planner = planner or PlannerService(workspace)
        self.settings = settings or self._load_settings()
        self._runner, self._story_runner, self.model = runner, story_runner, model
        self.delivery = delivery  # a DeliveryEngine when delivery is enabled: dependents then also need DEPENDENTS_UNLOCKED
        self._status_lock = asyncio.Lock()

    # ------------------------------------------------------------- plumbing

    def _load_settings(self) -> ExecutionConfig:
        path = self.workspace.root / "dridh.yaml"
        return load_config(path).execution if path.is_file() else ExecutionConfig()

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    def _story_runner_instance(self) -> StoryRunner:
        if self._story_runner is None:
            runner = self._runner or OpenCodeRunner(
                executable=os.environ.get("DRIDH_OPENCODE_BIN", "opencode"), workspace_root=self.workspace.project_root,
                config_dir=os.environ.get("OPENCODE_CONFIG_DIR"), timeout=self.settings.agent_timeout_seconds,
                env={key: os.environ[key] for key in ("OPENCODE_CONFIG_DIR", "OPENCODE_PROVIDER_CREDENTIAL", "OPENCODE_AUTH_FILE", "OPENROUTER_API_KEY") if os.environ.get(key)},
            )
            self._story_runner = StoryRunner(self.workspace, runner, self.settings, model=self.model)
        return self._story_runner

    def _run(self, run_id: str) -> dict[str, Any]:
        with self._store() as store:
            return RunRepository(store.read(), self.workspace.workspace_id).get_run(run_id)

    def _set_run(self, run_id: str, status: str, reason: str | None, actor: str) -> dict[str, Any]:
        with self._store() as store, store.transaction() as connection:
            repo = RunRepository(connection, self.workspace.workspace_id)
            return repo.set_run_status(repo.get_run(run_id), status, reason=reason, actor=actor)

    # ---------------------------------------------------------------- start

    def _context(self, feature_ref: str) -> tuple[dict[str, Any], str]:
        status = FeatureLifecycle(self.workspace).status(feature_ref)
        feature = status["feature"]
        if feature["stage"] != "EXECUTION" or feature["status"] != "ACTIVE":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"Feature '{feature['slug']}' is at {feature['stage']} ({feature['status']}), not an active EXECUTION stage.",
                             recovery="Approve the plan (`dridh planner approve`) and run `dridh feature sync`.")
        if status["stale"]:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, f"The feature is stale: {status['stale']['reason']}")
        session_id = status["sessions"]["planner_session_id"]
        if not session_id:
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The feature has no Planner session.")
        return feature, session_id

    def _require_git_identity(self) -> None:
        """Story work is committed to local branches; Git refuses without an identity, so say so before anything runs."""
        missing = []
        for key in ("user.name", "user.email"):
            found = subprocess.run(["git", "config", "--get", key], cwd=self.workspace.project_root, capture_output=True, text=True, check=False)  # noqa: S603, S607
            if found.returncode != 0 or not found.stdout.strip():
                missing.append(key)
        if missing:
            raise DridhError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Git cannot commit story work in this repository: " + " and ".join(missing) + " not set.",
                             recovery='Set them, for example: git config user.name "Your Name" && git config user.email you@example.com (in this repository or globally).')

    def challenge(self, feature_ref: str) -> dict[str, Any]:
        self._require_git_identity()
        feature, session_id = self._context(feature_ref)
        gate = self.planner.gate(session_id)
        if not gate.get("unlocked"):
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The plan is not approved for execution.", details=(str(gate.get("reason", "")),))
        stories = self.planner.execution_stories(session_id)
        pending = [s for s in stories if s["status"] != "COMPLETED"]
        if not pending:
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Every story is already COMPLETED.")
        return {
            "feature": feature["slug"], "planner_session_id": session_id, "plan_digest": gate["plan_digest"],
            "stories": len(stories), "pending": len(pending), "max_parallel": self.settings.max_parallel_stories,
            "max_repairs": self.settings.max_repair_attempts,
            "confirmation": f"EXECUTE {feature['slug']} {gate['plan_digest'][:12]}",
        }

    def start(self, feature_ref: str, *, confirmed_by: str, confirmation: str) -> dict[str, Any]:
        challenge = self.challenge(feature_ref)
        if confirmation != challenge["confirmation"]:
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The execution confirmation did not match.")
        confirmed_by = confirmed_by.strip()[:100]
        if not confirmed_by:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "confirmed_by is required.")
        feature, session_id = self._context(feature_ref)
        with self._store() as store, store.transaction() as connection:
            return RunRepository(connection, self.workspace.workspace_id).create_run(
                feature_id=feature["id"], planner_session_id=session_id, plan_digest=challenge["plan_digest"],
                max_parallel=self.settings.max_parallel_stories, max_repairs=self.settings.max_repair_attempts,
                confirmed_by=confirmed_by, actor=f"human:{confirmed_by}")

    # ---------------------------------------------------------------- state

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        with self._store() as store:
            repo = RunRepository(store.read(), self.workspace.workspace_id)
            attempts = repo.attempts(run_id)
            lots = repo.lots(run_id)
        stories = self.planner.execution_stories(run["planner_session_id"])
        counts: dict[str, int] = {}
        for story in stories:
            counts[story["status"]] = counts.get(story["status"], 0) + 1
        flagged = sorted({a["story_id"] for a in attempts if a["status"] == "PASSED" and (a["evidence"].get("jev") or {}).get("flagged")})
        latest: dict[str, dict[str, Any]] = {}
        for attempt in attempts:  # the latest passing attempt of each story is what a reviewer approves
            if attempt["status"] == "PASSED":
                latest[attempt["story_id"]] = attempt
        warnings = [{"story_id": story_id, "warning": text} for story_id, attempt in sorted(latest.items())
                    for text in attempt["evidence"].get("warnings") or []]
        return {"run": run, "stories": counts, "total": len(stories), "lots": lots, "jev_flagged_stories": flagged,
                "review_warnings": warnings,
                "attempts": [{k: a[k] for k in ("story_id", "attempt_no", "kind", "status", "commit_sha")} for a in attempts]}

    def stop(self, run_id: str, *, actor: str) -> dict[str, Any]:
        return self._set_run(run_id, "STOPPED", "stopped by a human", actor)

    @contextlib.contextmanager
    def _driver_lock(self, run_id: str, *, wait: bool = False) -> Iterator[bool]:
        """Hold the run's driver lock. Yields ``True`` if acquired, ``False`` if another live process holds it.

        The operating system drops the lock when its holder dies, so a crashed driver can never leave a run stuck.
        """
        if fcntl is None:
            yield True
            return
        folder = Path(self.workspace.root) / "locks"
        folder.mkdir(parents=True, exist_ok=True)
        handle = (folder / f"run-{run_id}.lock").open("a")
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                acquired = True
            except OSError:
                acquired = False
            yield acquired
        finally:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def resume(self, run_id: str, *, actor: str) -> dict[str, Any]:
        """Resume a paused run, or recover a RUNNING run whose driving process died (nothing holds its driver lock)."""
        run = self._run(run_id)
        if run["status"] == "RUNNING":
            with self._driver_lock(run_id) as free:
                if not free:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Another process is driving this run right now.",
                                     recovery="Wait for it to finish or pause, or stop that process first.")
            from dridh.state.reviews import _audit

            with self._store() as store, store.transaction() as connection:  # record the state before recovering from it
                _audit(connection, self.workspace.workspace_id, actor, "execution.recover", "execution_run", run_id,
                       {"reason": "the driving process died", "revision": run["revision"]})
            return run  # drive() resets stories left IN_PROGRESS and continues from their worktrees
        if run["status"] != "PAUSED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"The run is {run['status']}, not paused.")
        return self._set_run(run_id, "RUNNING", None, actor)

    # ---------------------------------------------------------------- drive

    async def drive(self, run_id: str) -> dict[str, Any]:
        """Run ready stories until nothing can progress; the run then completes or pauses with a reason."""
        run = self._run(run_id)
        if run["status"] != "RUNNING":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"The run is {run['status']}, not running.")
        with self._driver_lock(run_id) as free:
            if not free:
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Another process is already driving this run.")
            return await self._drive(run_id, run)

    async def _drive(self, run_id: str, run: dict[str, Any]) -> dict[str, Any]:
        session_id, runner = run["planner_session_id"], self._story_runner_instance()
        running: dict[asyncio.Task[StoryOutcome], str] = {}
        try:
            stories = self._stories(run_id, session_id)
            if stories is None:
                return self.status(run_id)
            await self._clean_stale_containers(run_id)
            await self._status({s["story_id"]: "NOT_STARTED" for s in stories if s["status"] == "IN_PROGRESS"}, session_id)
            while True:
                if self._run(run_id)["status"] != "RUNNING":
                    break
                stories = self._stories(run_id, session_id)
                if stories is None:
                    break
                by_id = {s["story_id"]: s for s in stories}
                lot = self._checkpoint(run, stories, drained=not running)
                if lot is None and not running and self._lots(run_id, "AWAITING_REVIEW"):
                    lot = self._lots(run_id, "AWAITING_REVIEW")[0]
                if lot is None:
                    busy = set(running.values())
                    ready = [s for s in stories if s["status"] == "NOT_STARTED" and s["story_id"] not in busy
                             and all(by_id[d]["status"] == "COMPLETED" and self._unlocked(d) for d in s["dependencies"])]
                    launch = ready[: max(0, run["max_parallel"] - len(running))]
                    if launch:
                        await self._status({s["story_id"]: "IN_PROGRESS" for s in launch}, session_id)
                        for story in launch:
                            task = asyncio.ensure_future(runner.execute(run, self._with_notes(run_id, story), self._prerequisites(run_id, story, by_id), self._baseline(run, story)))
                            running[task] = story["story_id"]
                if not running:
                    return self._settle(run_id, stories)
                done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                updates: dict[str, str] = {}
                for task in done:
                    story_id = running.pop(task)
                    try:
                        outcome = task.result()
                    except Exception as exc:  # noqa: BLE001 - one story's crash must not lose the others
                        updates[story_id] = "BLOCKED"
                        self._log_crash(run_id, story_id, exc)
                        self._close_open_attempts(run_id, story_id, exc)
                        continue
                    updates[story_id] = "IN_REVIEW" if outcome.passed else "BLOCKED"
                await self._status(updates, session_id)
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_when=asyncio.ALL_COMPLETED, return_exceptions=True)
                await self._status({sid: "NOT_STARTED" for sid in running.values()}, session_id)
        return self.status(run_id)

    def _stories(self, run_id: str, session_id: str) -> list[dict[str, Any]] | None:
        """The plan's stories, or ``None`` after pausing the run because the approved plan is no longer valid."""
        try:
            return self.planner.execution_stories(session_id)
        except DridhError as exc:
            if exc.code != ErrorCode.APPROVAL_REQUIRED:
                raise
            self._set_run(run_id, "PAUSED", f"The approved plan is no longer valid: {'; '.join(exc.details or (exc.message,))}"[:500], "executor")
            return None

    def _with_notes(self, run_id: str, story: dict[str, Any]) -> dict[str, Any]:
        with self._store() as store:
            notes = RunRepository(store.read(), self.workspace.workspace_id).rework_notes(run_id, story["story_id"])
        return dict(story, review_notes=notes) if notes else story

    def _lots(self, run_id: str, status: str | None = None) -> list[dict[str, Any]]:
        with self._store() as store:
            lots = RunRepository(store.read(), self.workspace.workspace_id).lots(run_id)
        return [lot for lot in lots if status is None or lot["status"] == status]

    def _checkpoint(self, run: dict[str, Any], stories: list[dict[str, Any]], *, drained: bool) -> dict[str, Any] | None:
        """Open (or reopen) a review lot when the pathfinder or a percentage threshold is reached.

        Returns the lot that now awaits review, if any. Running stories may keep finishing; none is started.
        """
        run_id = run["id"]
        lots = self._lots(run_id)
        waiting = [lot for lot in lots if lot["status"] == "AWAITING_REVIEW"]
        if waiting:
            return waiting[0]
        status = {s["story_id"]: s["status"] for s in stories}
        for lot in lots:  # reworked stories that pass again return to the same checkpoint
            if lot["status"] == "REWORK_REQUIRED" and lot["kind"] != "STARVATION" and all(status.get(sid) == "IN_REVIEW" for sid in lot["story_ids"]):
                with self._store() as store, store.transaction() as connection:
                    return RunRepository(connection, self.workspace.workspace_id).reopen_lot(lot["id"], actor="executor")
        lotted = {sid for lot in lots if lot["kind"] != "STARVATION" and lot["status"] in ("AWAITING_REVIEW", "APPROVED") for sid in lot["story_ids"]}
        fresh = sorted(sid for sid, value in status.items() if value == "IN_REVIEW" and sid not in lotted)
        if not fresh:
            return None
        done = sum(1 for value in status.values() if value in ("IN_REVIEW", "COMPLETED"))
        percent = 100 * done // max(1, len(status))
        used = {lot["threshold_percent"] for lot in lots if lot["threshold_percent"]}
        kind, threshold = None, None
        if self.settings.pathfinder_checkpoint and not any(lot["kind"] == "PATHFINDER" for lot in lots):
            kind = "PATHFINDER"
        else:
            crossed = [t for t in self.settings.checkpoint_percentages if percent >= t and t not in used]
            if crossed:
                threshold = max(crossed)
                kind = "FINAL" if threshold == 100 else "THRESHOLD"
        if kind is None and drained and not self._startable(status, stories):
            # Nothing runs and nothing can start until these stories are reviewed: without a lot the run would wait
            # forever (found live: 25% and 50% were used, 4 of 5 done, and the last story depended on the unlotted one).
            kind, threshold = ("FINAL" if percent >= 100 else "THRESHOLD"), max(1, min(percent, 100))
        if kind is None:
            return None
        with self._store() as store, store.transaction() as connection:
            return RunRepository(connection, self.workspace.workspace_id).open_lot(
                run_id=run_id, kind=kind, story_ids=fresh, threshold_percent=threshold, actor="executor")

    def _startable(self, status: dict[str, str], stories: list[dict[str, Any]]) -> bool:
        """Whether any not-started story could start now (all its dependencies completed and unlocked)."""
        return any(s["status"] == "NOT_STARTED" and all(status.get(d) == "COMPLETED" and self._unlocked(d) for d in s["dependencies"])
                   for s in stories)

    def _unlocked(self, story_id: str) -> bool:
        return self.delivery is None or self.delivery.gates(story_id).get("DEPENDENTS_UNLOCKED", {}).get("status") == "PASS"

    def _baseline(self, run: dict[str, Any], story: dict[str, Any]) -> str | None:
        return self.delivery.baseline(run["feature_id"]) if self.delivery is not None and story["dependencies"] else None

    def _prerequisites(self, run_id: str, story: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
        with self._store() as store:
            repo = RunRepository(store.read(), self.workspace.workspace_id)
            return [c for c in (repo.passing_commit(run_id, dep) for dep in story["dependencies"]) if c]

    async def _clean_stale_containers(self, run_id: str) -> None:
        """Remove test containers, networks, volumes and Compose projects a crashed drive of this run left (scenario 11)."""
        if self.settings.containers.mode == "off":
            return
        from dridh.executor import containers

        available, _ = await containers.docker_available()
        if available:
            await containers.cleanup_stale(self.workspace.workspace_id, run_id=run_id)

    async def _status(self, updates: dict[str, str], session_id: str) -> None:
        if updates:
            async with self._status_lock:
                await asyncio.to_thread(self.planner.update_story_statuses, session_id, updates, actor=AGENT)

    def _settle(self, run_id: str, stories: list[dict[str, Any]]) -> dict[str, Any]:
        states = {s["status"] for s in stories}
        waiting = self._lots(run_id, "AWAITING_REVIEW")
        if states == {"COMPLETED"}:
            self._set_run(run_id, "COMPLETED", None, "executor")
        elif waiting:
            lot = waiting[0]
            self._set_run(run_id, "PAUSED", f"Checkpoint lot {lot['ordinal']} ({lot['kind'].lower()}, {len(lot['story_ids'])} stories) awaits a whole-lot human verdict.", "executor")
        elif "BLOCKED" in states:
            blocked = sorted(s["story_id"] for s in stories if s["status"] == "BLOCKED")
            with self._store() as store, store.transaction() as connection:
                RunRepository(connection, self.workspace.workspace_id).open_lot(
                    run_id=run_id, kind="STARVATION", story_ids=blocked, threshold_percent=None, actor="executor")
            self._set_run(run_id, "PAUSED", "Blocked stories are starving the remaining stories; a human must send them back for rework, discard them or stop the run.", "executor")
        elif "IN_REVIEW" in states:
            self._set_run(run_id, "PAUSED", "Stories are awaiting human review before their dependents can start.", "executor")
        elif self.delivery is not None and any(s["status"] == "NOT_STARTED" and s["dependencies"] and all(
                d["status"] == "COMPLETED" for d in stories if d["story_id"] in s["dependencies"]) for s in stories):
            self._set_run(run_id, "PAUSED", "Completed stories await delivery (`dridh deliver run`) before their dependents can start.", "executor")
        else:
            self._set_run(run_id, "PAUSED", "No story can start.", "executor")
        return self.status(run_id)

    # --------------------------------------------------------------- review

    def _lot_by_ordinal(self, run_id: str, ordinal: int) -> dict[str, Any]:
        for lot in self._lots(run_id):
            if lot["ordinal"] == ordinal:
                return lot
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"The run has no lot {ordinal}.")

    def review_challenge(self, run_id: str, ordinal: int, verdict: str) -> dict[str, Any]:
        if verdict not in ("approved", "rework"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "verdict must be 'approved' or 'rework'.")
        lot = self._lot_by_ordinal(run_id, ordinal)
        if lot["status"] != "AWAITING_REVIEW":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"Lot {ordinal} is {lot['status']}, not awaiting review.")
        if lot["kind"] == "STARVATION" and verdict == "approved":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Blocked stories cannot be approved; send them back for rework.")
        return {"lot": lot, "confirmation": f"{verdict.upper()} LOT {run_id[:8]} {ordinal}"}

    def review_lot(self, run_id: str, ordinal: int, *, verdict: str, reviewer: str, notes: str, confirmation: str) -> dict[str, Any]:
        """A human's whole-lot verdict: approved releases every story in the lot, rework sends them all back."""
        challenge = self.review_challenge(run_id, ordinal, verdict)
        if confirmation != challenge["confirmation"]:
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The review confirmation did not match.")
        reviewer = reviewer.strip()[:100]
        if not reviewer:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "reviewer is required.")
        if verdict == "rework" and not notes.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Say what must change (--notes) when sending a lot back for rework.")
        lot, run = challenge["lot"], self._run(run_id)
        with self._store() as store, store.transaction() as connection:
            RunRepository(connection, self.workspace.workspace_id).review_lot(
                lot["id"], verdict="APPROVED" if verdict == "approved" else "REWORK_REQUIRED", reviewer=reviewer, notes=notes, actor=f"human:{reviewer}")
        session_id = run["planner_session_id"]
        if verdict == "approved":
            self.planner.update_story_statuses(session_id, {sid: "COMPLETED" for sid in lot["story_ids"]}, actor=f"human:{reviewer}")
            if all(s["status"] == "COMPLETED" for s in self.planner.execution_stories(session_id)):
                self._set_run(run_id, "COMPLETED", None, f"human:{reviewer}")
        else:
            self.planner.update_story_statuses(session_id, {sid: "NOT_STARTED" for sid in lot["story_ids"]}, actor=f"human:{reviewer}")
        return self.status(run_id)

    def discard_story(self, run_id: str, story_id: str, *, reviewer: str) -> dict[str, Any]:
        """Throw away one story's worktree and branch and let it start again; nothing else is affected."""
        run = self._run(run_id)
        if run["status"] == "RUNNING":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Pause or stop the run before discarding a story.")
        stories = {s["story_id"]: s for s in self.planner.execution_stories(run["planner_session_id"])}
        if story_id not in stories or stories[story_id]["status"] in ("COMPLETED", "IN_PROGRESS"):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Only a story that is not COMPLETED or IN_PROGRESS can be discarded.")
        kept = self._story_runner_instance().discard(run, story_id)
        from dridh.state.reviews import _audit

        with self._store() as store, store.transaction() as connection:  # state is recorded before the story is reset
            _audit(connection, self.workspace.workspace_id, f"human:{reviewer}", "execution.discard", "story", story_id,
                   {"run_id": run_id, "snapshot": (kept["snapshot"] or {}).get("ref"), "branch_tip": (kept["branch_tip"] or {}).get("ref")})
        self.planner.update_story_statuses(run["planner_session_id"], {story_id: "NOT_STARTED"}, actor=f"human:{reviewer}")
        return {**self.status(run_id), "recovery": kept}

    def _close_open_attempts(self, run_id: str, story_id: str, exc: Exception) -> None:
        """A crashed story must not leave an attempt RUNNING: close it with the reason, so status and review show why."""
        reason = f"{type(exc).__name__}: {str(redact(str(exc)))[:500]}"
        with self._store() as store, store.transaction() as connection:
            repo = RunRepository(connection, self.workspace.workspace_id)
            for attempt in repo.attempts(run_id, story_id):
                if attempt["status"] == "RUNNING":
                    repo.finish_attempt(attempt["id"], status="FAILED", commit_sha=None, evidence={"crash": reason}, actor="dridh:executor")

    def _log_crash(self, run_id: str, story_id: str, exc: Exception) -> None:
        folder = Path(self.workspace.root) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "executor.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{run_id} {story_id} {type(exc).__name__}: {exc}\n")
