"""The delivery engine (plan Phase 16).

For each reviewed story: push its branch and open a PR on a deterministic base, evaluate the required checks for
that exact head SHA, integrate the story into ``dridh/integration/<feature>`` locally, verify it, push that branch
with a guarded fast-forward (never forced), index the integrated revision and only then record
``DEPENDENTS_UNLOCKED`` in one transaction. Remote approval and merge are never prerequisites; ``REMOTE_MERGED``
is only observed.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from dridh.code_reacher.indexer import CodeIndexer
from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.config.dridh_yaml import DeliveryConfig, ExecutionConfig
from dridh.delivery.github import GhCli, GitHubAdapter, parse_remote
from dridh.delivery.policy import FAILING, evaluate_checks, select_base
from dridh.executor.policy import detect_test_command, run_test_command
from dridh.executor.story_runner import story_branch
from dridh.planner.service import PlannerService
from dridh.state.execution import ExecutionRepository
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo
from dridh.workspace.worktree import WorktreeManager

ACTOR = "delivery"
ELIGIBLE_STATUSES = ("IN_REVIEW", "COMPLETED")


class DeliveryEngine:
    def __init__(self, workspace: WorkspaceInfo, *, config: DeliveryConfig, execution: ExecutionConfig | None = None,
                 planner: PlannerService | None = None, adapter: GitHubAdapter | None = None, indexer: Any = None) -> None:
        self.workspace, self.config = workspace, config
        self.execution = execution or ExecutionConfig()
        self.planner = planner or PlannerService(workspace)
        self._adapter, self._indexer = adapter, indexer
        self.root = workspace.project_root
        self.worktrees = WorktreeManager(self.root)

    # ------------------------------------------------------------ plumbing

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, check=False, timeout=300)
        if check and done.returncode != 0:
            raise DridhError(ErrorCode.PROCESS_EXECUTION_FAILED, f"git {args[0]} failed.", details=(str(redact(done.stderr.strip()))[:300],))
        return done

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    def adapter(self) -> GitHubAdapter:
        if self._adapter is None:
            owner, repo = parse_remote(self._git("remote", "get-url", self.config.remote).stdout)
            self._adapter = GhCli(owner, repo)
        return self._adapter

    def _gate(self, story_id: str, gate: str, status: str, evidence: dict[str, Any], sha: str | None = None) -> None:
        with self._store() as store, store.transaction() as connection:
            ExecutionRepository(connection, self.workspace.workspace_id).record_gate(
                gate_id=str(uuid.uuid4()), story_id=story_id, gate=gate, status=status, evidence=evidence, revision_sha=sha)

    def gates(self, story_id: str) -> dict[str, dict[str, Any]]:
        with self._store() as store:
            rows = store.read().execute("SELECT gate,status,revision_sha,evidence_json FROM story_gate WHERE workspace_id=? AND story_id=?",
                                        (self.workspace.workspace_id, story_id)).fetchall()
        return {r["gate"]: {"status": r["status"], "sha": r["revision_sha"]} for r in rows}

    def _run_and_story(self, run_id: str, story_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], str]:
        with self._store() as store:
            repo = RunRepository(store.read(), self.workspace.workspace_id)
            run = repo.get_run(run_id)
            commit = repo.passing_commit(run_id, story_id)
            feature = store.read().execute("SELECT slug FROM feature WHERE id=?", (run["feature_id"],)).fetchone()["slug"]
        stories = {s["story_id"]: s for s in self.planner.execution_stories(run["planner_session_id"])}
        if story_id not in stories:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown story '{story_id}'.")
        if stories[story_id]["status"] not in ELIGIBLE_STATUSES or not commit:
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"{story_id} has not passed its tests and review yet.")
        return run, stories[story_id], stories, feature

    def integration_branch(self, feature: str) -> str:
        return f"dridh/integration/{feature}"

    # ------------------------------------------------------------- PR step

    def _transitive(self, story_id: str, stories: dict[str, dict[str, Any]]) -> set[str]:
        found: set[str] = set()
        todo = list(stories[story_id]["dependencies"])
        while todo:
            item = todo.pop()
            if item not in found:
                found.add(item)
                todo.extend(stories[item]["dependencies"])
        return found

    def _remote_has(self, branch: str) -> bool:
        return bool(self._git("ls-remote", "--heads", self.config.remote, branch, check=False).stdout.strip())

    def create_pr(self, run_id: str, story_id: str) -> dict[str, Any]:
        run, story, stories, feature = self._run_and_story(run_id, story_id)
        branch = story_branch(run_id, story_id)
        head = self._git("rev-parse", branch).stdout.strip()
        with self._store() as store:
            existing = store.read().execute("SELECT * FROM pull_request WHERE workspace_id=? AND story_id=? ORDER BY created_at DESC LIMIT 1",
                                            (self.workspace.workspace_id, story_id)).fetchone()
            commits = {dep: RunRepository(store.read(), self.workspace.workspace_id).passing_commit(run_id, dep) for dep in self._transitive(story_id, stories)}
        if existing is not None and existing["head_sha"] == head and existing["state"] == "OPEN":
            return {"created": False, "story_id": story_id, "number": existing["number"], "url": existing["url"], "base": existing["base_ref"]}
        integration = self.integration_branch(feature)

        def ancestry(ref: str, needed: set[str]) -> bool:
            return all(commits.get(dep) and self._git("merge-base", "--is-ancestor", commits[dep], f"{self.config.remote}/{ref}", check=False).returncode == 0
                       for dep in needed)

        self._git("fetch", self.config.remote, check=False)
        chosen = select_base(direct=story["dependencies"], transitive=self._transitive(story_id, stories), target=self.config.target_branch,
                             integration_branch=integration,
                             predecessor_branch=lambda dep: story_branch(run_id, dep) if self._remote_has(story_branch(run_id, dep)) else None,
                             contains_ancestry=ancestry)
        base = chosen["base"]
        if not self._remote_has(base):
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, f"The PR base '{base}' is not on {self.config.remote}; integrate the predecessors first.",
                             details=(chosen["rule"],))
        needed = {dep: commits.get(dep) for dep in self._transitive(story_id, stories)}
        remote_base = f"{self.config.remote}/{base}"
        for dep, commit in needed.items():  # the base must hold every predecessor, and so must the story itself
            if not commit or any(self._git("merge-base", "--is-ancestor", commit, ref, check=False).returncode != 0 for ref in (remote_base, head)):
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, f"The PR base '{base}' or the story branch does not contain predecessor {dep}; reconcile before opening a PR.")
        if self._git("merge-base", remote_base, head, check=False).returncode != 0:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, f"The PR base '{base}' shares no history with the story branch.")
        self._git("push", self.config.remote, f"{branch}:{branch}")  # never forced
        title = f"{story_id}: {story['title']}"[:200]
        created = self.adapter().create_pr(title=title, body=self._body(story, stories, branch), head=branch, base=base)
        with self._store() as store, store.transaction() as connection:
            ExecutionRepository(connection, self.workspace.workspace_id).record_pull_request(
                pr_id=f"pr-{run_id[:8]}-{story_id.lower()}", title=title, head_ref=branch, head_sha=head, base_ref=base, story_id=story_id,
                number=created["number"], url=created["url"], base_sha=created.get("base_sha"))
        self._gate(story_id, "PR_CREATED", "PASS", {"number": created["number"], "url": created["url"], "base": base, "rule": chosen["rule"]}, head)
        return {"created": True, "story_id": story_id, "number": created["number"], "url": created["url"], "base": base, "rule": chosen["rule"]}

    def _body(self, story: dict[str, Any], stories: dict[str, dict[str, Any]], branch: str) -> str:
        def lines(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) or "- (none)"

        deps = [f"{dep} ({stories[dep]['title']})" for dep in story["dependencies"]]
        return (f"## {story['story_id']}: {story['title']}\n\nComponent: {story['component_id']}\n"
                f"Use cases: {', '.join(story['use_case_ids']) or 'none'}\n\n### Depends on\n{lines(deps)}\n\n"
                f"### Acceptance criteria\n{lines(story['acceptance_criteria'])}\n\n### Definition of done\n{lines(story['definition_of_done'])}\n\n"
                f"### Test evidence\nThe story's tests passed locally before this PR was opened (gate `TESTS_PASSED`).\n\n"
                f"Branch: `{branch}`. Opened by Dridh; merging is a human decision.")

    # ---------------------------------------------------------- check step

    def refresh_checks(self, story_id: str) -> dict[str, Any]:
        """Evaluate the required checks for the PR's recorded head SHA, and invalidate if the head moved."""
        with self._store() as store:
            pr = store.read().execute("SELECT * FROM pull_request WHERE workspace_id=? AND story_id=? ORDER BY created_at DESC LIMIT 1",
                                      (self.workspace.workspace_id, story_id)).fetchone()
        if pr is None:
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, f"{story_id} has no pull request.")
        view = self.adapter().pr_view(int(pr["number"]))
        sha = pr["head_sha"]
        if view["head_sha"] != sha and self._behind(view["head_sha"], sha):
            # GitHub moves a PR's head a few seconds after a push: the head it reports is an ancestor of the commit
            # Dridh pushed (found live after a CI repair). Not a moved head; wait for GitHub to catch up.
            return {"story_id": story_id, "state": "PENDING", "head_sha": sha, "failures": [],
                    "reasons": [f"GitHub still reports {view['head_sha'][:10]} as the PR head; waiting for it to show {sha[:10]}."]}
        if view["head_sha"] != sha:
            for gate in ("GHA_PASSED", "DEPENDENTS_UNLOCKED"):
                if self.gates(story_id).get(gate, {}).get("status") == "PASS":
                    self._gate(story_id, gate, "FAIL", {"reason": "The PR head SHA changed after it was evaluated.", "recorded": sha, "current": view["head_sha"]}, view["head_sha"])
            return {"story_id": story_id, "state": "STALE", "reasons": [f"The PR head moved from {sha[:10]} to {view['head_sha'][:10]}; push is not a Dridh action, so reconcile the branch."]}
        runs = self.adapter().check_runs(sha)
        with self._store() as store, store.transaction() as connection:
            repo = ExecutionRepository(connection, self.workspace.workspace_id)
            for check in runs:
                repo.record_check(check_id=f"{pr['id']}:{check.get('run_id') or check['name']}:{check['name']}", pr_id=pr["id"], name=check["name"],
                                  head_sha=sha, status=check["status"], conclusion=check.get("conclusion"), run_id=check.get("run_id"),
                                  url=check.get("url"), started_at=check.get("started_at"), completed_at=check.get("completed_at"))
        result = evaluate_checks(runs, required=self.config.required_checks, optional=self.config.optional_checks)
        failures = [{"name": c["name"], "conclusion": c.get("conclusion"), "url": c.get("url"), "summary": c.get("summary", "")}
                    for c in runs if c["status"] == "completed" and c.get("conclusion") in FAILING]
        gate_status = {"PASS": "PASS", "PENDING": "PENDING", "FAIL": "FAIL"}[result["state"]]
        self._gate(story_id, "GHA_PASSED", gate_status, {"reasons": result["reasons"], "checks": len(runs), "failures": failures[:10]}, sha)
        if view.get("merged"):
            self._gate(story_id, "REMOTE_MERGED", "PASS", {"state": view["state"]}, sha)
        return {"story_id": story_id, "state": result["state"], "reasons": result["reasons"], "head_sha": sha, "failures": failures[:10]}

    def _behind(self, reported: str, recorded: str) -> bool:
        """True when GitHub's reported head is a strict ancestor of the head Dridh recorded (GitHub lagging a push)."""
        done = self._git("merge-base", "--is-ancestor", reported, recorded, check=False)
        return done.returncode == 0

    def wait_for_checks(self, story_id: str, *, sleep=time.sleep) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + self.config.checks_timeout_seconds
        while True:
            result = self.refresh_checks(story_id)
            if result["state"] != "PENDING" or time.monotonic() >= deadline:
                return result
            sleep(self.config.poll_seconds)

    # ------------------------------------------------- CI failure repair (16.7)

    def _ci_rounds(self, run_id: str, story_id: str) -> int:
        with self._store() as store:
            attempts = RunRepository(store.read(), self.workspace.workspace_id).attempts(run_id, story_id)
        return len({a["evidence"].get("ci_round") for a in attempts if a["evidence"].get("trigger") == "CI"})

    def repair_failed_checks(self, run_id: str, story_id: str, *, runner: Any) -> dict[str, Any]:
        """One bounded round: give the failing check details to the Executor, push its fix and re-evaluate the new head SHA.

        ``runner`` is a StoryRunner. The repair reuses the story's worktree and branch, must produce a new commit, and still
        passes the ownership, no-new-mock and test checks. The push is an ordinary fast-forward, never forced.
        """
        before = self.refresh_checks(story_id)
        if before["state"] != "FAIL":
            return {**before, "repaired": False, "rounds": self._ci_rounds(run_id, story_id)}
        rounds = self._ci_rounds(run_id, story_id)
        if rounds >= self.config.max_ci_repairs:
            return {**before, "repaired": False, "rounds": rounds, "exhausted": True,
                    "message": "The CI repair budget is spent; a human must decide (Dridh never retries without bound)."}
        findings = [f"Check '{f['name']}' concluded {f['conclusion']}" + (f": {f['summary']}" if f["summary"] else "") + (f" ({f['url']})" if f["url"] else "")
                    for f in before["failures"]] or list(before["reasons"])
        run, story, _stories, _feature = self._run_and_story(run_id, story_id)
        outcome = asyncio.run(runner.execute(run, dict(story, ci_findings=findings, ci_round=rounds + 1), [], None))
        if not outcome.passed:
            return {**before, "repaired": False, "rounds": rounds + 1, "findings": outcome.findings}
        branch = story_branch(run_id, story_id)
        self._git("push", self.config.remote, f"{branch}:{branch}")  # fast-forward; a rejected push raises
        with self._store() as store:
            pr = store.read().execute("SELECT * FROM pull_request WHERE workspace_id=? AND story_id=? ORDER BY created_at DESC LIMIT 1",
                                      (self.workspace.workspace_id, story_id)).fetchone()
        with self._store() as store, store.transaction() as connection:
            ExecutionRepository(connection, self.workspace.workspace_id).record_pull_request(
                pr_id=pr["id"], title=pr["title"], head_ref=pr["head_ref"], head_sha=outcome.commit_sha or "", base_ref=pr["base_ref"],
                story_id=story_id, number=pr["number"], url=pr["url"], base_sha=pr["base_sha"])
        self._gate(story_id, "GHA_PASSED", "PENDING", {"reasons": ["Waiting for checks on the repaired commit."]}, outcome.commit_sha)
        return {"story_id": story_id, "state": "PENDING", "repaired": True, "rounds": rounds + 1, "head_sha": outcome.commit_sha,
                "previous_failures": before["failures"]}

    def repair_until_green(self, run_id: str, story_id: str, *, runner: Any, sleep=time.sleep) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        """Wait for checks; while they fail and budget remains, repair, push and wait again."""
        result = self.wait_for_checks(story_id, sleep=sleep)
        while result["state"] == "FAIL":
            step = self.repair_failed_checks(run_id, story_id, runner=runner)
            if not step.get("repaired"):
                return {**result, **{k: v for k, v in step.items() if k in ("exhausted", "message", "findings", "rounds")}}
            result = {**self.wait_for_checks(story_id, sleep=sleep), "rounds": step["rounds"], "repaired": True}
        return result

    def deliver_story(self, run_id: str, story_id: str, *, runner: Any = None, sleep=time.sleep) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        """PR, checks (with bounded CI repair when ``runner`` is given), then local integration and unlock."""
        pr = self.create_pr(run_id, story_id)
        if runner is not None and self.config.max_ci_repairs > 0:
            checks = self.repair_until_green(run_id, story_id, runner=runner, sleep=sleep)
        else:
            checks = self.wait_for_checks(story_id, sleep=sleep)
        if checks["state"] != "PASS":
            return {"story_id": story_id, "pr": pr, "checks": checks, "integration": None}
        return {"story_id": story_id, "pr": pr, "checks": checks, "integration": self.integrate(run_id, story_id)}

    # ---------------------------------------------------- integration step

    def integrate(self, run_id: str, story_id: str) -> dict[str, Any]:
        run, story, stories, feature = self._run_and_story(run_id, story_id)
        if story["status"] != "COMPLETED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"{story_id} has not been approved in a reviewed lot (status {story['status']}).")
        gates = self.gates(story_id)
        with self._store() as store:
            pr = store.read().execute("SELECT * FROM pull_request WHERE workspace_id=? AND story_id=? ORDER BY created_at DESC LIMIT 1",
                                      (self.workspace.workspace_id, story_id)).fetchone()
        branch = story_branch(run_id, story_id)
        head = self._git("rev-parse", branch).stdout.strip()
        if pr is None or gates.get("GHA_PASSED", {}).get("status") != "PASS" or gates["GHA_PASSED"]["sha"] != head or pr["head_sha"] != head:
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, f"{story_id} has no passing required checks for its current head commit.",
                             recovery="Run `dridh deliver checks` after the PR checks finish.")
        if self.adapter().pr_view(int(pr["number"]))["head_sha"] != head:
            self.refresh_checks(story_id)
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The PR head changed since its checks passed; eligibility was withdrawn.")
        name = self.integration_branch(feature)
        worktree = self._integration_worktree(feature, name)
        merge = self._git("merge", "--no-ff", "-m", f"Integrate {story_id}: {story['title']}"[:200], head, cwd=worktree, check=False)
        if merge.returncode != 0:
            self._git("merge", "--abort", cwd=worktree, check=False)
            self._gate(story_id, "LOCALLY_INTEGRATED", "BLOCKED", {"reason": "merge conflict", "detail": str(redact(merge.stdout + merge.stderr))[:400]}, head)
            return {"story_id": story_id, "integrated": False, "blocked": "The story conflicts with the integration branch; the PR and worktree are preserved."}
        merged = self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        command = self.execution.test_command or detect_test_command(worktree)
        if command is None:
            self._git("reset", "--hard", "HEAD^", cwd=worktree)
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "No test command is recognised for integration verification; set execution.test_command.")
        test = asyncio.run(run_test_command(command, worktree, timeout=self.execution.test_timeout_seconds))
        if not test["passed"]:
            self._git("reset", "--hard", "HEAD^", cwd=worktree)
            self._gate(story_id, "LOCALLY_INTEGRATED", "FAIL", {"tests": test["command"], "exit_code": test["exit_code"], "output": test["output"][-600:]}, head)
            return {"story_id": story_id, "integrated": False, "blocked": "Integration verification failed; the merge was undone."}
        self._gate(story_id, "LOCALLY_INTEGRATED", "PASS", {"tests": test["command"]}, merged)
        pushed = self._git("push", self.config.remote, f"{name}:{name}", cwd=worktree, check=False)  # fast-forward only, never forced
        if pushed.returncode != 0:
            self._gate(story_id, "INTEGRATION_PUSHED", "BLOCKED", {"reason": "the remote integration branch diverged", "detail": str(redact(pushed.stderr))[:400]}, merged)
            return {"story_id": story_id, "integrated": True, "blocked": "The remote integration branch diverged; reconcile it manually (Dridh never force-pushes)."}
        self._gate(story_id, "INTEGRATION_PUSHED", "PASS", {"remote_ref": f"{self.config.remote}/{name}"}, merged)
        with self._store() as store, store.transaction() as connection:
            repo = ExecutionRepository(connection, self.workspace.workspace_id)
            predecessors = sorted({story_id, *self._transitive(story_id, stories)})
            repo.record_integration(integration_id=str(uuid.uuid4()), branch_name=name, commit_sha=merged, feature_id=run["feature_id"],
                                    remote_ref=f"{self.config.remote}/{name}", source_pr_id=pr["id"], predecessor_story_ids=predecessors,
                                    verification={"tests": test["command"]})
        try:
            indexed = (self._indexer or CodeIndexer(self.workspace)).index(root=worktree)
        except Exception as exc:  # noqa: BLE001 - a failed index must be recorded, never left as a crash after the push
            self._gate(story_id, "INDEXED", "FAIL", {"error": f"{type(exc).__name__}: {str(redact(str(exc)))[:200]}", "integration_commit": merged}, merged)
            return {"story_id": story_id, "integrated": True, "blocked": "Code Reacher failed to index the integrated revision; dependents stay locked. "
                    "The integration is pushed and safe: fix the cause and run `dridh deliver run` again.", "integration_commit": merged}
        index_sha = self._index_revision()
        if index_sha != merged:
            self._gate(story_id, "INDEXED", "FAIL", {"index_revision": index_sha, "integration_commit": merged, "result": indexed.get("status")}, merged)
            return {"story_id": story_id, "integrated": True, "blocked": "The code index does not match the integration commit."}
        from dridh.code_reacher.mapping import refresh_mapping

        self._gate(story_id, "INDEXED", "PASS", {"index_revision": index_sha, "story_mapping": refresh_mapping(self.workspace)}, merged)
        with self._store() as store, store.transaction() as connection:  # all evidence is present: unlock in one transaction
            present = {r["gate"]: (r["status"], r["revision_sha"]) for r in connection.execute(
                "SELECT gate,status,revision_sha FROM story_gate WHERE workspace_id=? AND story_id=?", (self.workspace.workspace_id, story_id))}
            required = ("TESTS_PASSED", "PR_CREATED", "GHA_PASSED", "LOCALLY_INTEGRATED", "INTEGRATION_PUSHED", "INDEXED")
            missing = [gate for gate in required if present.get(gate, ("", ""))[0] != "PASS"]
            if missing:
                raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "Unlock evidence is incomplete.", details=tuple(missing))
            ExecutionRepository(connection, self.workspace.workspace_id).record_gate(
                gate_id=str(uuid.uuid4()), story_id=story_id, gate="DEPENDENTS_UNLOCKED", status="PASS",
                evidence={"integration_commit": merged, "branch": name}, revision_sha=merged)
        return {"story_id": story_id, "integrated": True, "unlocked": True, "integration_commit": merged, "branch": name}

    def _integration_worktree(self, feature: str, name: str) -> Path:
        worktree_id = f"integration-{feature}"
        self._git("fetch", self.config.remote, check=False)
        remote_ref = f"{self.config.remote}/{name}"
        if self._git("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0:
            base = self._git("rev-parse", remote_ref).stdout.strip()
        else:
            target = f"{self.config.remote}/{self.config.target_branch}"
            base = self._git("rev-parse", target if self._git("rev-parse", "--verify", "--quiet", target, check=False).returncode == 0
                             else self.config.target_branch).stdout.strip()
        info = self.worktrees.allocate_from_commit(worktree_id, feature, base, name)
        if self._git("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0:
            ahead = self._git("merge-base", "--is-ancestor", remote_ref, "HEAD", cwd=info.path, check=False).returncode == 0
            if not ahead:
                behind = self._git("merge", "--ff-only", remote_ref, cwd=info.path, check=False)
                if behind.returncode != 0:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The local integration branch diverged from the remote; reconcile it (no force-push).")
        return info.path

    def _index_revision(self) -> str | None:
        with self._store() as store:
            row = store.read().execute("SELECT source_sha FROM code_index_run WHERE workspace_id=? AND status='SUCCEEDED' ORDER BY started_at DESC, rowid DESC LIMIT 1",
                                       (self.workspace.workspace_id,)).fetchone()
        return row["source_sha"] if row else None

    def baseline(self, feature_id: str) -> str | None:
        """The latest pushed integration commit for a feature (the base for dependent story worktrees)."""
        with self._store() as store:
            row = store.read().execute("SELECT commit_sha FROM integration_commit WHERE workspace_id=? AND feature_id=? AND remote_ref IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                       (self.workspace.workspace_id, feature_id)).fetchone()
        return row["commit_sha"] if row else None

    def status(self, run_id: str) -> list[dict[str, Any]]:
        with self._store() as store:
            run = RunRepository(store.read(), self.workspace.workspace_id).get_run(run_id)
        return [{"story_id": s["story_id"], "status": s["status"], "gates": {k: v["status"] for k, v in self.gates(s["story_id"]).items()}}
                for s in self.planner.execution_stories(run["planner_session_id"])]
