"""Runs one story: worktree, implementation agent, deterministic checks, tests, local commit, bounded repair."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dridh.common.errors import DridhError
from dridh.config.dridh_yaml import ExecutionConfig
from dridh.executor.flow_coverage import check_report, prove, unproven_claims
from dridh.executor.policy import detect_test_command, new_mocks, run_test_command, skipped_tests
from dridh.integrations.opencode import AgentRunError
from dridh.orchestration.plan import extract_json_object
from dridh.orchestration.roster import parse_worker_result, paths_within
from dridh.state.execution import ExecutionRepository
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo
from dridh.workspace.worktree import WorktreeManager

AGENT = "dridh/executor"
TEST_DIRECTORIES = ("tests", "test", "src/test", "__tests__", "spec")
PROTECTED = re.compile(r"(^|/)(\.opencode|\.git)(/|$)|(^|/)\.env[^/]*$")
ACTOR = "executor"


def story_branch(run_id: str, story_id: str) -> str:
    return f"dridh/story/{story_id.lower()}-{run_id[:8]}"


class AgentRunner(Protocol):
    async def run(self, **kwargs: Any) -> Any: ...


@dataclass
class StoryOutcome:
    story_id: str
    passed: bool
    commit_sha: str | None
    branch: str
    attempts: int
    findings: list[str] = field(default_factory=list)
    summary: str = ""


def flow_rules(flows: list[dict[str, Any]]) -> str:
    """The story's approved flows and how to report and test them (empty for a story that owns no flow)."""
    if not flows:
        return ""
    listed = "\n".join(f"- {f['id']} ({f['type'].lower()}): {f['text']}" for f in flows)
    example = flows[0]["id"]
    tag = example.lower().replace("-", "_").replace(".", "_")
    return (
        f"\nApproved flows this story implements:\n{listed}\n"
        "Every flow you implement needs at least one test whose name contains its ID, for example "
        f"test_{tag}_<what_it_checks>, @DisplayName(\"{example} ...\") or it(\"[{example}] ...\"). Dridh runs the tests and "
        "checks that each named test passed; a flow you report IMPLEMENTED without such a passing test fails the story.\n"
        "Report every flow in \"flows\": IMPLEMENTED (built as written), CHANGED (built differently: say how in \"note\") or "
        "NOT_DONE (not built: say why in \"note\"). CHANGED and NOT_DONE become ledger entries a human must approve.\n"
    )


def contract_section(contract: list[str]) -> str:
    """The approved API/event references for the story, copied from the approved design (empty when there are none)."""
    if not contract:
        return ""
    listed = "\n".join(f"- {item}" for item in contract)
    return ("Approved contract for this story (copied from the approved design; implement these names and parameters exactly; "
            f"if a todo or acceptance criterion disagrees, the contract wins):\n{listed}\n")


def implementation_prompt(story: dict[str, Any], allowed: list[str], feedback: list[str]) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- (none)"

    text = (
        f"You are {AGENT}. Implement story {story['story_id']} of the approved plan inside this worktree.\n"
        f"Title: {story['title']}\nComponent: {story['component_id']}\nDescription: {story.get('description', '')}\n"
        + contract_section(story.get("approved_contract") or [])
        + f"Todos:\n{bullets(story['todos'])}\nAcceptance criteria:\n{bullets(story['acceptance_criteria'])}\n"
        f"Definition of done:\n{bullets(story['definition_of_done'])}\n"
        f"You may create or change files only under: {', '.join(allowed)}.\n"
        "Write tests that exercise real dependencies. Do not introduce mocks, stubs or fakes of the system under test or its "
        "dependencies; any new mock fails the story. You cannot run commands: Dridh runs the project's tests after you finish.\n"
        + flow_rules(story.get("flows") or [])
        + "Reply with exactly one JSON object and nothing else:\n"
        + ('{"status": "DONE" or "BLOCKED", "summary": "one sentence", "files_changed": ["relative/path"]'
           + (', "flows": [{"id": "<flow ID>", "status": "IMPLEMENTED" or "CHANGED" or "NOT_DONE", "note": "..."}]' if story.get("flows") else "")
           + "}\n")
        + "Report BLOCKED with a reason if you cannot complete the story."
    )
    if story.get("ci_findings"):
        text += ("\nThis story's pull request failed its required checks on GitHub. Fix only what these failures require, "
                 "keep the story's scope, and do not weaken or remove tests:\n" + bullets(story["ci_findings"]) + "\n")
    if story.get("review_notes"):
        text += "\nA human reviewer asked for changes to your earlier work on this story:\n" + bullets(story["review_notes"]) + "\n"
    if feedback:
        text += "\nThe previous attempt failed these deterministic checks; fix only these issues:\n" + bullets(feedback)
    return text


class StoryRunner:
    def __init__(self, workspace: WorkspaceInfo, runner: AgentRunner, settings: ExecutionConfig, *, model: str | None = None) -> None:
        self.workspace, self.runner, self.settings, self.model = workspace, runner, settings, model
        self.worktrees = WorktreeManager(workspace.project_root)

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    async def execute(self, run: dict[str, Any], story: dict[str, Any], prerequisite_commits: list[str], baseline: str | None = None) -> StoryOutcome:
        story_id = story["story_id"]
        branch = story_branch(run["id"], story_id)
        worktree_id = f"exec-{run['id'][:8]}-{story_id.lower()}"
        try:
            info = await asyncio.to_thread(self._prepare, worktree_id, story_id, branch, prerequisite_commits, baseline)
        except DridhError as exc:
            reason = [f"Worktree preparation failed: {exc.message}", *[str(item)[:300] for item in (exc.details or ())][:3]]
            attempt = self._start_attempt(run["id"], story_id, "REPAIR" if story.get("ci_findings") else "IMPLEMENT")
            self._finish(attempt, "FAILED", None, {"worktree_error": reason})  # a blocked story must always say why
            return StoryOutcome(story_id, False, None, branch, 1, reason)
        self._record_workspace(story_id, worktree_id, info, branch)
        story = {**story, "flows": self._flows(story_id)}
        allowed = list(story["file_ownership"]) + list(TEST_DIRECTORIES)
        findings: list[str] = []
        session_id: str | None = None
        ci_repair = bool(story.get("ci_findings"))
        head_before = (await asyncio.to_thread(self.worktrees._git, "rev-parse", "HEAD", cwd=info.path)) if ci_repair else None  # noqa: SLF001
        total = self.settings.max_repair_attempts + 1
        for number in range(1, total + 1):
            kind = "IMPLEMENT" if number == 1 and not ci_repair else "REPAIR"
            attempt = self._start_attempt(run["id"], story_id, kind)
            evidence: dict[str, Any] = {"kind": kind, **({"trigger": "CI", "ci_round": story.get("ci_round")} if ci_repair else {})}
            try:
                result = await self.runner.run(
                    agent=AGENT, prompt=implementation_prompt(story, allowed, findings), session_id=session_id, model=self.model,
                    title=f"Dridh Executor {story_id}", worktree_path=info.path,
                )
                session_id = getattr(result, "session_id", None) or session_id
                report = parse_worker_result(result.text)
                flow_report, report_findings = check_report((extract_json_object(result.text) or {}).get("flows"), [f["id"] for f in story["flows"]])
            except AgentRunError as exc:
                findings = [f"Agent run failed: {exc.message}"]
                self._finish(attempt, "FAILED", None, {**evidence, "agent_error": exc.to_dict()})
                return StoryOutcome(story_id, False, None, branch, number, findings)  # provider failures are never repaired
            except DridhError as exc:
                findings = [exc.message, *[str(item) for item in (exc.details or ())][:3]]
                self._finish(attempt, "FAILED", None, {**evidence, "contract": findings})
                continue
            evidence["summary"] = report.summary
            if report.status != "DONE":
                self._finish(attempt, "FAILED", None, {**evidence, "blocked": report.summary})
                return StoryOutcome(story_id, False, None, branch, number, [f"The agent reported BLOCKED: {report.summary}"], report.summary)
            if report_findings:  # an invalid flow report is repaired like a failing check, before any test runs
                findings = report_findings
                self._finish(attempt, "FAILED", None, {**evidence, "findings": findings})
                continue
            evidence["flow_report"] = flow_report
            try:
                findings, commit = await self._verify(worktree_id, info.path, story, allowed, story_id, evidence, self._original_base(worktree_id, info.base_commit), run_id=run["id"])
            except DridhError as exc:  # e.g. Git cannot commit: an environment problem the agent cannot repair
                reason = [f"Dridh could not verify or commit the story: {exc.message}", *[str(item)[:300] for item in (exc.details or ())][:3]]
                self._finish(attempt, "FAILED", None, {**evidence, "environment_error": reason})
                return StoryOutcome(story_id, False, None, branch, number, reason)
            if not findings and ci_repair and commit == head_before:
                findings = ["The repair produced no new commit, so the pull request is unchanged; change the code the failing checks cover."]
            if not findings:
                from dridh.jev.checkpoints import run_checkpoint

                evidence["jev"] = await asyncio.to_thread(
                    run_checkpoint, self.workspace, stage="executor", subject_id=f"{run['id']}:{story_id}", agent_id=AGENT,
                    session_id=session_id, summary=(
                        f"Story {story_id}: {story['title']}. Acceptance criteria: {len(story['acceptance_criteria'])}; changed files: "
                        f"{len(evidence.get('changed_files', []))}; project tests passed; no new mocks; attempt {number}."),
                    metadata={"source_revision_sha": commit})
                evidence["ledger_drafts"] = self._draft_deviations(story_id, flow_report, evidence)
                self._finish(attempt, "PASSED", commit, evidence)
                self._record_gate(story_id, commit, evidence)
                return StoryOutcome(story_id, True, commit, branch, number, [], report.summary)
            self._finish(attempt, "FAILED", None, {**evidence, "findings": findings})
            if any(item.startswith("No test command") for item in findings):
                break  # nothing the agent can repair
        return StoryOutcome(story_id, False, None, branch, number, findings)

    def discard(self, run: dict[str, Any], story_id: str) -> dict[str, Any]:
        """Remove the story's worktree and branch (a human decision) without losing work.

        Uncommitted changes are saved on a recovery ref first, and the branch tip is pinned on another before the branch is deleted.
        Attempts stay on record. Returns what was kept so the human can restore it.
        """
        worktree_id = f"exec-{run['id'][:8]}-{story_id.lower()}"
        branch = story_branch(run["id"], story_id)
        kept: dict[str, Any] = {"snapshot": None, "branch_tip": None}
        removed = self.worktrees.cleanup(worktree_id, force=True, reason=f"discard {story_id}")
        kept["snapshot"] = removed["snapshot"]
        try:
            tip = self.worktrees._git("rev-parse", "--verify", branch)  # noqa: SLF001
            kept["branch_tip"] = {"ref": self.worktrees.keep_ref(worktree_id, "branch-tip", tip), "commit": tip}
            self.worktrees._git("branch", "-D", branch)  # noqa: SLF001
        except DridhError:
            pass  # the branch did not exist
        with self._store() as store, store.transaction() as connection:
            repo = ExecutionRepository(connection, self.workspace.workspace_id)
            repo.record_worktree(worktree_id=worktree_id, path=str(self.worktrees.managed_root / worktree_id), story_id=story_id, status="CLEANED")
            repo.record_branch(branch_id=f"branch-{worktree_id}", name=branch, story_id=story_id, status="DELETED")
        return kept

    # ----------------------------------------------------------------- helpers

    def _flows(self, story_id: str) -> list[dict[str, Any]]:
        from dridh.ledger.flows import flows_for_story

        with self._store() as store:
            return flows_for_story(store.read(), self.workspace.workspace_id, story_id)

    def _draft_deviations(self, story_id: str, flow_report: dict[str, dict[str, str]], evidence: dict[str, Any]) -> list[str]:
        """Write the flows reported CHANGED or NOT_DONE to the ledger as drafts awaiting a human decision. A failure is
        recorded, not raised: the delivery check then still blocks, because the deviation has no ledger entry."""
        from dridh.ledger.drafts import add_drafts, drafts_from_report

        drafts = drafts_from_report(story_id, flow_report)
        if not drafts:
            return []
        try:
            return add_drafts(self.workspace, drafts)["written"]
        except DridhError as exc:
            evidence.setdefault("warnings", []).append(f"The deviations could not be written to the ledger: {exc.message}")
            return []

    def _prepare(self, worktree_id: str, story_id: str, branch: str, prerequisites: list[str], baseline: str | None = None) -> Any:
        if baseline:  # delivery is on: start from the verified, pushed integration commit
            return self.worktrees.allocate_from_commit(worktree_id, story_id, baseline, branch)
        if prerequisites:
            base = self.worktrees.integrate_prerequisites(f"int-{worktree_id}", prerequisites)
            return self.worktrees.allocate_from_commit(worktree_id, story_id, base, branch)
        return self.worktrees.allocate(worktree_id, story_id, branch)

    async def _verify(self, worktree_id: str, path: Path, story: dict[str, Any], allowed: list[str],
                      story_id: str, evidence: dict[str, Any], base_commit: str, *, run_id: str) -> tuple[list[str], str | None]:
        git = self.worktrees._git  # noqa: SLF001 - the manager is this package's own worktree primitive
        await asyncio.to_thread(self.worktrees.exclude_test_byproducts)
        await asyncio.to_thread(git, "add", "-A", cwd=path)
        await asyncio.to_thread(self.worktrees.unstage_test_byproducts, path)
        changed = [line for line in (await asyncio.to_thread(git, "diff", "--cached", "--name-only", "HEAD", cwd=path)).splitlines() if line]
        evidence["changed_files"] = changed
        findings: list[str] = []
        committed = [line for line in (await asyncio.to_thread(git, "diff", "--name-only", base_commit, "HEAD", cwd=path)).splitlines() if line]
        if not changed and not committed:  # a reworked story may already carry its earlier commits
            findings.append("The story produced no file changes.")
        protected = [item for item in changed if PROTECTED.search(item)]
        if protected:
            findings.append(f"Protected files were changed: {', '.join(protected[:5])}.")
        outside = [] if (not story["file_ownership"] and story.get("infrastructure")) else paths_within(tuple(allowed), tuple(changed))
        if outside:
            findings.append(f"Files outside the story's ownership were changed: {', '.join(outside[:5])}.")
        diff = await asyncio.to_thread(git, "diff", "--cached", "HEAD", cwd=path)
        mocks = new_mocks(diff)
        if mocks:
            findings.append(f"New mocks are not allowed (use real dependencies): {mocks[0]}")
        evidence["new_mocks"] = mocks[:5]
        if findings:
            return findings, None
        command = self.settings.test_command or detect_test_command(path)
        if command is None:
            return ["No test command was recognised for this project; set execution.test_command."], None
        test = await self._run_tests(command, path, run_id=run_id, story_id=story_id, worktree_id=worktree_id)
        evidence["tests"] = test
        skipped = skipped_tests(str(test.get("output") or ""))
        if skipped:  # never a failure (skips can be legitimate), but the reviewer must see it
            test["skipped"] = skipped
            evidence.setdefault("warnings", []).append(
                f"{skipped} test(s) were skipped. Check that none of them is this story's acceptance test before approving.")
        if test.get("infrastructure_failure"):
            return [f"The test environment failed, not the code (tests did not run): {str(test['output'])[-800:]}"], None
        if not test["passed"]:
            reason = "timed out" if test["timed_out"] else f"exited {test['exit_code']}"
            return [f"Tests ({' '.join(command)}) {reason}: {str(test['output'])[-800:]}"], None
        if story.get("flows"):  # each flow reported IMPLEMENTED must be proven by a passing test named for it
            proof = prove([f["id"] for f in story["flows"]], test, path)
            evidence["flow_proof"] = proof
            unproven = unproven_claims(evidence.get("flow_report") or {}, proof)
            if unproven:
                return unproven, None
        commit = await asyncio.to_thread(self.worktrees.commit_changes, worktree_id, f"{story_id}: {story['title']}"[:200])
        return [], commit

    async def _run_tests(self, command: list[str], path: Path, *, run_id: str, story_id: str, worktree_id: str) -> dict[str, Any]:
        """Run the story's tests in a container when configured and possible, else on the host."""
        from dridh.executor import containers as c

        config = self.settings.containers
        timeout = self.settings.test_timeout_seconds
        if config.mode == "off":
            return {**await run_test_command(command, path, timeout=timeout), "containers": {"mode": "host", "reason": "execution.containers.mode is off"}}
        available, detail = await c.docker_available()
        kind = c.project_kind(command)
        image = config.image or c.DEFAULT_IMAGES.get(kind or "")
        if not available or image is None:
            reason = detail if not available else "no container image is known for this project; set execution.containers.image"
            if config.mode == "docker":
                return {"command": command, "passed": False, "exit_code": None, "timed_out": False, "infrastructure_failure": True,
                        "output": f"execution.containers.mode is docker but {reason}.", "containers": {"mode": "refused", "reason": reason}}
            return {**await run_test_command(command, path, timeout=timeout), "containers": {"mode": "host", "reason": reason}}
        plan = c.ContainerPlan(
            image=image, setup=config.setup if config.setup is not None else c.default_setup(kind, path, command), command=command,
            compose_file=c.find_compose_file(path, config.compose_file), memory=config.memory, cpus=config.cpus,
            docker_socket=config.docker_socket, names=c.environment_names(self.workspace.workspace_id, run_id, story_id),
            labels=c.labels(self.workspace.workspace_id, run_id, story_id))
        self._record_environment(plan, story_id, worktree_id, "RUNNING", {})
        result = await c.run_in_container(plan, path, timeout=timeout)
        record = result.get("containers", {})
        self._record_environment(plan, story_id, worktree_id, "CLEANED" if record.get("cleanup") is not None else record.get("status", "FAILED"), record)
        return result

    def _record_environment(self, plan: Any, story_id: str, worktree_id: str, status: str, record: dict[str, Any]) -> None:
        with self._store() as store, store.transaction() as connection:
            ExecutionRepository(connection, self.workspace.workspace_id).record_docker_environment(
                environment_id=plan.names["id"], compose_project=plan.names["project"], network_name=record.get("network") or plan.names["network"],
                story_id=story_id, worktree_id=worktree_id, status=status, image_digest=record.get("image_digest"),
                evidence={**record, "result": record.get("status")})

    def _original_base(self, worktree_id: str, fallback: str) -> str:
        """The commit the story's worktree was first created from (a reused worktree reports its current HEAD)."""
        with self._store() as store:
            row = store.read().execute("SELECT base_sha FROM worktree WHERE id=? AND workspace_id=?", (worktree_id, self.workspace.workspace_id)).fetchone()
        return (row["base_sha"] if row and row["base_sha"] else fallback)

    def _record_workspace(self, story_id: str, worktree_id: str, info: Any, branch: str) -> None:
        with self._store() as store, store.transaction() as connection:
            repo = ExecutionRepository(connection, self.workspace.workspace_id)
            repo.record_branch(branch_id=f"branch-{worktree_id}", name=branch, story_id=story_id, base_ref=info.base_commit)
            repo.record_worktree(worktree_id=worktree_id, path=str(info.path), story_id=story_id,
                                 branch_id=f"branch-{worktree_id}", base_sha=info.base_commit)

    def _start_attempt(self, run_id: str, story_id: str, kind: str) -> str:
        with self._store() as store, store.transaction() as connection:
            return RunRepository(connection, self.workspace.workspace_id).start_attempt(
                run_id=run_id, story_id=story_id, kind=kind, actor=ACTOR)["id"]

    def _finish(self, attempt_id: str, status: str, commit: str | None, evidence: dict[str, Any]) -> None:
        with self._store() as store, store.transaction() as connection:
            RunRepository(connection, self.workspace.workspace_id).finish_attempt(
                attempt_id, status=status, commit_sha=commit, evidence=evidence, actor=ACTOR)

    def _record_gate(self, story_id: str, commit: str | None, evidence: dict[str, Any]) -> None:
        with self._store() as store, store.transaction() as connection:
            ExecutionRepository(connection, self.workspace.workspace_id).record_gate(
                gate_id=str(uuid.uuid4()), story_id=story_id, gate="TESTS_PASSED", status="PASS",
                evidence={"tests": evidence.get("tests", {}).get("command"), "changed_files": evidence.get("changed_files")},
                revision_sha=commit)
