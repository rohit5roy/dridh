"""Sequential orchestration engine, exercised with a deterministic in-process worker.

These tests cover the state machine, plan validation, dependency and approval
gates, deterministic acceptance, failure classification, leases, recovery and
cancellation. They do not start OpenCode; the Docker E2E covers that path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.integrations.opencode import AgentErrorCode, AgentRunError, AgentRunResult
from dridh.orchestration.engine import EngineSettings, WorkflowEngine
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace, open_workspace

GREETER = 'def greet(name):\n    return f"Hello, {name}!"\n'
TEST_GREETER = (
    "import os\nimport sys\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
    "import unittest\nfrom src.greeter import greet\n\n\n"
    "class GreetTest(unittest.TestCase):\n"
    "    def test_greet(self):\n"
    "        self.assertEqual(greet('Ada'), 'Hello, Ada!')\n\n\n"
    "if __name__ == '__main__':\n    unittest.main()\n"
)


def plan_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": "dridh.plan",
        "schema_version": 1,
        "objective": "Create a greeting function",
        "stages": [
            {
                "id": "S-01",
                "title": "Greeting",
                "tasks": [
                    {
                        "id": "T-001",
                        "title": "Greeting module",
                        "agent": "dridh/code-reacher",
                        "depends_on": [],
                        "allowed_paths": ["src/"],
                        "instructions": "Create src/greeter.py with greet(name).",
                        "max_attempts": 2,
                        "acceptance": [
                            {"type": "file_exists", "path": "src/greeter.py"},
                            {"type": "file_contains", "path": "src/greeter.py", "text": "def greet"},
                        ],
                    },
                    {
                        "id": "T-002",
                        "title": "Tests",
                        "agent": "dridh/code-reacher",
                        "depends_on": ["T-001"],
                        "allowed_paths": ["tests/"],
                        "instructions": "Write tests for greet.",
                        "acceptance": [{"type": "command", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]}],
                    },
                ],
            }
        ],
        "acceptance": [],
    }
    document.update(overrides)
    return document


class Worker:
    """A scripted worker. Each agent maps to a function from prompt to reply text, or raises."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []
        self.session_inputs: list[str | None] = []
        self.behaviour: dict[str, Any] = {}

    async def run(self, *, agent: str, prompt: str, title: str | None = None, session_id: str | None = None, worktree_path=None) -> AgentRunResult:
        self.calls.append((agent, prompt))
        self.session_inputs.append(session_id)
        # Use worktree_path as root if provided, else self.root.
        if worktree_path is not None:
            self.root = worktree_path
        handler = self.behaviour.get(agent) or self.behaviour.get("*")
        reply = handler(self, prompt) if handler else ""
        if isinstance(reply, BaseException):
            raise reply
        return AgentRunResult(
            agent=agent,
            prompt=prompt,
            session_id="ses_testsession0001",
            text=reply,
            exit_code=0,
            duration_seconds=0.01,
        )


def planner_reply(worker: Worker, prompt: str) -> str:
    return "```json\n" + json.dumps(plan_document()) + "\n```"


def honest_coder(worker: Worker, prompt: str) -> str:
    if "T-001" in prompt:
        (worker.root / "src").mkdir(exist_ok=True)
        (worker.root / "src" / "greeter.py").write_text(GREETER)
        return json.dumps({"status": "DONE", "summary": "wrote greeter", "files_changed": ["src/greeter.py"]})
    (worker.root / "tests").mkdir(exist_ok=True)
    (worker.root / "tests" / "test_greeter.py").write_text(TEST_GREETER)
    return json.dumps({"status": "DONE", "summary": "wrote tests", "files_changed": ["tests/test_greeter.py"]})


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def settings() -> EngineSettings:
    return EngineSettings(
        worker_timeout_seconds=5,
        acceptance_timeout_seconds=30,
        lease_ttl_seconds=60,
        cancel_poll_seconds=0.05,
        require_plan_approval=True,
    )


def engine_for(project: Path, worker: Worker, settings: EngineSettings) -> WorkflowEngine:
    info, _ = init_workspace(project)
    return WorkflowEngine(info, actor="test", settings=settings, runner=worker)


def workspace_for(project: Path):
    return open_workspace(project)


def test_full_sequential_workflow_completes_with_evidence_and_verified_ledger(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    created = engine.create("Create a greeting function")
    workflow_id = created["workflow_id"]
    planned = asyncio.run(engine.plan(workflow_id))
    assert planned["state"] == "AWAITING_APPROVAL"
    assert [task["id"] for task in planned["plan"]["tasks"]] == ["T-001", "T-002"]

    with pytest.raises(DridhError) as early:
        asyncio.run(engine.execute_task(workflow_id))
    assert early.value.code is ErrorCode.APPROVAL_REQUIRED
    assert [agent for agent, _ in worker.calls] == ["dridh/planner"]

    engine.approve(workflow_id, reviewer="owner@terminal", method="terminal-confirmation")
    first = asyncio.run(engine.execute_task(workflow_id))
    assert first["outcome"] == "DONE" and first["task"] == "T-001"
    second = asyncio.run(engine.execute_task(workflow_id))
    assert second["outcome"] == "COMPLETED"
    assert [check["passed"] for check in second["task_checks"]] == [True]

    final = engine.status(workflow_id)
    assert final["state"] == "COMPLETED"
    assert all(task["state"] == "DONE" for task in final["plan"]["tasks"])
    assert final["ledger"]["verified"] is True
    report = engine.report(workflow_id)
    text = (project / report["report_path"]).read_text()
    assert "COMPLETED" in text and "PASS" in text and "ses_testsession0001" in text
    assert json.loads((project / report["json_path"]).read_text())["schema"] == "dridh.report"


def test_tasks_run_in_plan_order_and_dependencies_are_enforced(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    with pytest.raises(DridhError) as out_of_order:
        asyncio.run(engine.execute_task(workflow_id, "T-002"))
    assert out_of_order.value.code is ErrorCode.DEPENDENCY_NOT_READY
    assert all(agent != "dridh/code-reacher" for agent, _ in worker.calls)
    assert engine.next_task(workflow_id)["task"] == "T-001"


def test_plan_validation_repairs_invalid_plan_and_keeps_feedback(project: Path, settings) -> None:
    broken = plan_document(stages=[{"id": "S-01", "title": "Bad", "tasks": [
        {"id": "T-001", "title": "x", "agent": "dridh/coordinator", "depends_on": ["T-002"], "allowed_paths": ["../escape"],
         "instructions": "x", "acceptance": [{"type": "command", "argv": ["sh", "-c", "rm -rf /"]}]},
        {"id": "T-002", "title": "y", "agent": "dridh/code-reacher", "depends_on": ["T-002"], "allowed_paths": [".git/x"],
         "instructions": "y", "acceptance": [{"type": "file_exists", "path": "src/x.py"}]},
    ]}])
    worker = Worker(project)
    calls = 0

    def repair_invalid_plan(w: Worker, prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(broken)
        assert "Correction feedback from the previous attempt" in prompt
        for expected in ("not executable", "must be relative", "only 'python3 -m unittest'", "must appear earlier in the plan", "reserved"):
            assert expected in prompt, expected
        return planner_reply(w, prompt)

    worker.behaviour = {"dridh/planner": repair_invalid_plan}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Repair a broken plan")["workflow_id"]
    outcome = asyncio.run(engine.plan(workflow_id))
    assert outcome["state"] == "AWAITING_APPROVAL"
    assert calls == 2
    assert worker.session_inputs[-2:] == [None, "ses_testsession0001"]
    assert engine.verify_ledger()["ok"] is True


def test_worker_self_report_does_not_pass_acceptance(project: Path, settings) -> None:
    def lying_coder(worker: Worker, prompt: str) -> str:
        return json.dumps({"status": "DONE", "summary": "all done", "files_changed": ["src/greeter.py"]})

    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": lying_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    outcome = asyncio.run(engine.run_to_end(workflow_id))
    assert outcome["outcome"] == "FAILED" and outcome["code"] == "ACCEPTANCE_FAILED"
    assert outcome["blocked_for_human"] is True
    status = engine.status(workflow_id)
    assert status["state"] == "FAILED"
    assert status["plan"]["tasks"][0]["state"] == "FAILED"


def test_changes_outside_allowed_paths_fail_the_task(project: Path, settings) -> None:
    def wandering_coder(worker: Worker, prompt: str) -> str:
        (worker.root / "secrets.txt").write_text("nope")
        return json.dumps({"status": "DONE", "summary": "x", "files_changed": ["secrets.txt"]})

    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": wandering_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    outcome = asyncio.run(engine.execute_task(workflow_id))
    assert outcome["outcome"] == "FAILED" and outcome["code"] == "WORKER_FAILED"


def test_rejected_plan_returns_to_planning_and_blocks_execution(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    assert engine.reject(workflow_id, reviewer="owner", method="terminal-confirmation", reason="scope")["state"] == "PLANNING"
    with pytest.raises(DridhError) as blocked:
        asyncio.run(engine.execute_task(workflow_id))
    assert blocked.value.code is ErrorCode.INVALID_TRANSITION
    with pytest.raises(DridhError):
        engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation") and None


def test_timeout_is_retried_then_exhausted(project: Path, settings) -> None:
    calls = {"count": 0}

    def slow_then_ok(worker: Worker, prompt: str) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            return AgentRunError(AgentErrorCode.TIMEOUT, "timed out")
        return honest_coder(worker, prompt)

    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": slow_then_ok}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    interrupted = asyncio.run(engine.execute_task(workflow_id))
    assert interrupted["outcome"] == "INTERRUPTED" and interrupted["retryable"] is True
    assert engine.status(workflow_id)["state"] == "EXECUTING"
    retried = asyncio.run(engine.execute_task(workflow_id))
    assert retried["outcome"] == "DONE"
    assert engine.status(workflow_id)["plan"]["tasks"][0]["attempts"] == 2


def test_timeouts_exhaust_attempts_and_fail_the_workflow(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {
        "dridh/planner": planner_reply,
        "dridh/code-reacher": lambda w, p: AgentRunError(AgentErrorCode.TIMEOUT, "timed out"),
    }
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    asyncio.run(engine.execute_task(workflow_id))
    final = asyncio.run(engine.execute_task(workflow_id))
    assert final["outcome"] == "FAILED" and final["code"] == "WORKER_TIMEOUT"
    assert engine.status(workflow_id)["state"] == "FAILED"


def test_cancellation_before_execution_finishes_the_workflow(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    cancelled = engine.cancel(workflow_id, reason="owner stopped the run")
    assert cancelled["state"] == "CANCELLED"
    with pytest.raises(DridhError) as after:
        asyncio.run(engine.execute_task(workflow_id))
    assert after.value.code is ErrorCode.INVALID_TRANSITION
    with pytest.raises(DridhError):
        engine.cancel(workflow_id, reason="again")


def test_interrupted_run_is_recovered_from_an_expired_lease(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    info = workspace_for(project)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    with StateStore(info.state_db, info.workspace_id) as store:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET state='IN_PROGRESS', attempts=1, lease_token='dead', lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id='T-001'"
            )
            conn.execute("UPDATE workflows SET state='EXECUTING'")
    recovered = asyncio.run(engine.execute_task(workflow_id))
    assert recovered["outcome"] == "DONE"
    with StateStore(info.state_db, info.workspace_id) as store:
        events = [row[0] for row in store.read().execute("SELECT event_type FROM ledger ORDER BY seq")]
    assert "TASK_INTERRUPTED" in events
    assert engine.verify_ledger()["ok"] is True


def test_only_one_task_may_be_in_progress(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    info = workspace_for(project)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    with StateStore(info.state_db, info.workspace_id) as store:
        with store.transaction() as conn:
            conn.execute("UPDATE tasks SET state='IN_PROGRESS', lease_expires_at='2999-01-01T00:00:00+00:00' WHERE id='T-001'")
            conn.execute("UPDATE workflows SET state='EXECUTING'")
    with pytest.raises(DridhError) as busy:
        asyncio.run(engine.execute_task(workflow_id))
    assert busy.value.code is ErrorCode.DEPENDENCY_NOT_READY


def test_summary_text_containing_a_secret_is_redacted_in_the_report(project: Path, settings) -> None:
    def leaky_coder(worker: Worker, prompt: str) -> str:
        (worker.root / "src").mkdir(exist_ok=True)
        (worker.root / "src" / "greeter.py").write_text(GREETER)
        return json.dumps({"status": "DONE", "summary": "used api_key=synthetic-leak-9999", "files_changed": ["src/greeter.py"]})

    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": leaky_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    asyncio.run(engine.execute_task(workflow_id))
    report = engine.report(workflow_id)
    assert "synthetic-leak-9999" not in (project / report["report_path"]).read_text()
    assert "synthetic-leak-9999" not in (project / report["json_path"]).read_text()


def test_undeclared_writes_are_detected_from_disk_not_from_the_report(project: Path, settings) -> None:
    def quiet_wanderer(worker: Worker, prompt: str) -> str:
        (worker.root / "src").mkdir(exist_ok=True)
        (worker.root / "src" / "greeter.py").write_text(GREETER)
        (worker.root / "undeclared.txt").write_text("written outside the allowed paths")
        return json.dumps({"status": "DONE", "summary": "greeter only", "files_changed": ["src/greeter.py"]})

    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": quiet_wanderer}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    outcome = asyncio.run(engine.execute_task(workflow_id))
    assert outcome["outcome"] == "FAILED" and outcome["code"] == "WORKER_FAILED"
    assert "undeclared.txt" in " ".join(outcome["details"])


def test_parallel_execution_respects_dependencies_and_runs_concurrently(tmp_path: Path, settings) -> None:
    """Diamond dependency: A -> B, A -> C, B+C -> D. A runs first, then B and C in parallel, then D."""

    def independent_worker(worker: Worker, prompt: str) -> str:
        # Determine which task from the prompt.
        if "T-001" in prompt or "module" in prompt.lower():
            (worker.root / "src").mkdir(exist_ok=True)
            (worker.root / "src" / "greeter.py").write_text(GREETER)
            return json.dumps({"status": "DONE", "summary": "module written", "files_changed": ["src/greeter.py"]})
        if "T-002" in prompt or "tests" in prompt.lower():
            (worker.root / "tests").mkdir(exist_ok=True)
            (worker.root / "tests" / "test_greeter.py").write_text(TEST_GREETER)
            return json.dumps({"status": "DONE", "summary": "tests written", "files_changed": ["tests/test_greeter.py"]})
        if "T-003" in prompt or "docs" in prompt.lower():
            (worker.root / "docs").mkdir(exist_ok=True)
            (worker.root / "docs" / "note.md").write_text("notes")
            return json.dumps({"status": "DONE", "summary": "docs written", "files_changed": ["docs/note.md"]})
        if "T-004" in prompt or "report" in prompt.lower():
            (worker.root / "src").mkdir(exist_ok=True)
            (worker.root / "src" / "report.txt").write_text("report")
            return json.dumps({"status": "DONE", "summary": "final report", "files_changed": ["src/report.txt"]})
        return json.dumps({"status": "DONE", "summary": "done", "files_changed": []})

    # Build a 4-task diamond plan.
    parallel_plan = {
        "schema": "dridh.plan",
        "schema_version": 1,
        "objective": "Parallel greeting",
        "stages": [
            {
                "id": "S-01",
                "title": "Parallel",
                "tasks": [
                    {"id": "T-001", "title": "Module", "agent": "dridh/code-reacher", "depends_on": [], "allowed_paths": ["src/"], "instructions": "Create module", "max_attempts": 2, "acceptance": [{"type": "file_exists", "path": "src/greeter.py"}]},
                    {"id": "T-002", "title": "Tests", "agent": "dridh/code-reacher", "depends_on": ["T-001"], "allowed_paths": ["tests/"], "instructions": "Create tests", "max_attempts": 2, "acceptance": [{"type": "file_exists", "path": "tests/test_greeter.py"}]},
                    {"id": "T-003", "title": "Docs", "agent": "dridh/code-reacher", "depends_on": ["T-001"], "allowed_paths": ["docs/"], "instructions": "Create docs", "max_attempts": 2, "acceptance": [{"type": "file_exists", "path": "docs/note.md"}]},
                    {"id": "T-004", "title": "Report", "agent": "dridh/code-reacher", "depends_on": ["T-002", "T-003"], "allowed_paths": ["src/"], "instructions": "Final report", "max_attempts": 2, "acceptance": [{"type": "file_exists", "path": "src/report.txt"}]},
                ],
            }
        ],
        "acceptance": [],
    }

    # Monkey-patch plan_document usage by directly creating the engine and setting plan.
    import subprocess
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True)
    worker = Worker(project)
    # Override planner to return our parallel plan.
    def planner_reply(w, prompt):
        return json.dumps(parallel_plan)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": independent_worker}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Parallel greeting")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    result = asyncio.run(engine.run_parallel(workflow_id, concurrency=2))
    assert result["state"] == "COMPLETED", result
    task_states = {t["id"]: t["state"] for t in result["plan"]["tasks"]}
    assert task_states == {"T-001": "DONE", "T-002": "DONE", "T-003": "DONE", "T-004": "DONE"}


def test_clarification_and_qa_are_recorded(project: Path, settings) -> None:
    worker = Worker(project)
    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": honest_coder}
    engine = engine_for(project, worker, settings)
    workflow_id = engine.create("Clarify me")["workflow_id"]
    result = engine.record_clarification(workflow_id, "What format?", "JSON", source="user")
    assert result["question"] == "What format?"
    assert result["answer"] == "JSON"
    qa = engine.record_qa_result(workflow_id, "T-001", "reviewer:alice", "PASS", "Looks good")
    assert qa["verdict"] == "PASS"
    summary = engine.qa_summary(workflow_id)
    assert len(summary["results"]) == 1
    # Invalid verdict raises.
    with pytest.raises(DridhError):
        engine.record_qa_result(workflow_id, "T-001", "reviewer:alice", "MAYBE", "nope")


def test_planner_gets_deterministic_findings_and_repairs_invalid_plan(tmp_path: Path, settings) -> None:
    worker = Worker(tmp_path)
    calls = 0

    def planner(worker: Worker, prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(plan_document(stages=[]))
        assert "Correction feedback from the previous attempt" in prompt
        assert "stages must be a list" in prompt
        return planner_reply(worker, prompt)

    worker.behaviour = {"dridh/planner": planner}
    engine = engine_for(tmp_path, worker, settings)
    workflow_id = engine.create("Repair a malformed plan")[ "workflow_id" ]
    planned = asyncio.run(engine.plan(workflow_id))
    assert planned["state"] == "AWAITING_APPROVAL"
    assert calls == 2
    assert worker.session_inputs[0] is None
    assert worker.session_inputs[1] == "ses_testsession0001"
    with StateStore(open_workspace(tmp_path).state_db, open_workspace(tmp_path).workspace_id) as store:
        rows = store.read().execute(
            "SELECT decision, status, output_json FROM jev_evaluation WHERE subject_type='PLAN' AND subject_id=? ORDER BY started_at",
            (workflow_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["status"] == "FALLBACK"
        assert rows[1]["status"] == "FALLBACK"
        assert rows[0]["decision"] == "REPAIR"
        assert rows[1]["decision"] == "FALLBACK_DETERMINISTIC"
        first_output = json.loads(rows[0]["output_json"])
        assert first_output["deterministic_findings"]
        assert first_output["evaluation_level"] == "TASK"


def test_planner_stops_after_three_repair_attempts_and_persists_blocker(tmp_path: Path, settings) -> None:
    worker = Worker(tmp_path)
    worker.behaviour = {"dridh/planner": lambda w, p: json.dumps(plan_document(stages=[]))}
    engine = engine_for(tmp_path, worker, settings)
    workflow_id = engine.create("Never produce a valid plan")["workflow_id"]
    with pytest.raises(DridhError) as failure:
        asyncio.run(engine.plan(workflow_id))
    assert failure.value.code is ErrorCode.PLAN_INVALID
    assert "three repair attempts" in failure.value.message
    assert len([agent for agent, _ in worker.calls if agent == "dridh/planner"]) == 4
    status = engine.status(workflow_id)
    assert status["state"] == "FAILED"
    with StateStore(open_workspace(tmp_path).state_db, open_workspace(tmp_path).workspace_id) as store:
        rows = store.read().execute(
            "SELECT decision FROM jev_evaluation WHERE subject_type='PLAN' AND subject_id=?",
            (workflow_id,),
        ).fetchall()
        assert len(rows) == 4
        event = store.read().execute(
            "SELECT payload FROM ledger WHERE workflow_id=? AND event_type='PLAN_REPAIR_BLOCKED'",
            (workflow_id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["attempts"] == 4


def test_task_acceptance_failure_is_retried_with_persisted_feedback(tmp_path: Path, settings) -> None:
    worker = Worker(tmp_path)
    code_reacher_calls = 0

    def coder(worker: Worker, prompt: str) -> str:
        nonlocal code_reacher_calls
        code_reacher_calls += 1
        if code_reacher_calls == 1:
            (worker.root / "src").mkdir(exist_ok=True)
            (worker.root / "src" / "greeter.py").write_text("MESSAGE = 'missing greet function'\\n")
            return json.dumps({"status": "DONE", "summary": "created module but not the required function", "files_changed": ["src/greeter.py"]})
        assert "Correction feedback from the previous attempt" in prompt
        assert "file_contains" in prompt
        assert "Current artifact SHA256" in prompt
        assert "Preserve unrelated content" in prompt
        (worker.root / "src").mkdir(exist_ok=True)
        (worker.root / "src" / "greeter.py").write_text(GREETER)
        return json.dumps({"status": "DONE", "summary": "implemented the required function", "files_changed": ["src/greeter.py"]})

    worker.behaviour = {"dridh/planner": planner_reply, "dridh/code-reacher": coder}
    engine = engine_for(tmp_path, worker, settings)
    workflow_id = engine.create("Repair a task after failed acceptance")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    first = asyncio.run(engine.execute_task(workflow_id, "T-001"))
    assert first["outcome"] == "INTERRUPTED"
    second = asyncio.run(engine.execute_task(workflow_id, "T-001"))
    assert second["outcome"] == "DONE"
    assert code_reacher_calls == 2
    assert worker.session_inputs[-2:] == [None, "ses_testsession0001"]
    with StateStore(open_workspace(tmp_path).state_db, open_workspace(tmp_path).workspace_id) as store:
        row = store.read().execute(
            "SELECT state,attempts,failure_message FROM tasks WHERE workflow_id=? AND id='T-001'",
            (workflow_id,),
        ).fetchone()
        assert row["state"] == "DONE"
        assert row["attempts"] == 2
        assert row["failure_message"] is None
        events = store.read().execute(
            "SELECT event_type FROM ledger WHERE workflow_id=? AND event_type='TASK_INTERRUPTED'",
            (workflow_id,),
        ).fetchall()
        assert events
        evaluations = store.read().execute(
            "SELECT subject_type,decision,output_json FROM jev_evaluation WHERE workspace_id=?",
            (open_workspace(tmp_path).workspace_id,),
        ).fetchall()
        assert any(item["subject_type"] == "TASK" for item in evaluations)
        assert any(item["subject_type"] == "TURN" for item in evaluations)
        task_eval = next(item for item in evaluations if item["subject_type"] == "TASK")
        task_output = json.loads(task_eval["output_json"])
        assert any("file_contains" in item for item in task_output["repair_feedback"])
        assert any("Current artifact SHA256" in item for item in task_output["repair_feedback"])


def test_run_to_end_automatically_retries_interrupted_task_in_same_session(tmp_path: Path, settings) -> None:
    worker = Worker(tmp_path)
    coder_calls = 0

    def single_task_planner(worker: Worker, prompt: str) -> str:
        document = plan_document()
        document["stages"][0]["tasks"] = [document["stages"][0]["tasks"][0]]
        return json.dumps(document)

    def coder(worker: Worker, prompt: str) -> str:
        nonlocal coder_calls
        coder_calls += 1
        (worker.root / "src").mkdir(exist_ok=True)
        if coder_calls == 1:
            (worker.root / "src" / "greeter.py").write_text("MESSAGE = 'missing greet function'\n")
            return json.dumps({"status": "DONE", "summary": "first output fails acceptance", "files_changed": ["src/greeter.py"]})
        assert "Correction feedback from the previous attempt" in prompt
        assert "Current artifact SHA256" in prompt
        (worker.root / "src" / "greeter.py").write_text(GREETER)
        return json.dumps({"status": "DONE", "summary": "repaired implementation", "files_changed": ["src/greeter.py"]})

    worker.behaviour = {"dridh/planner": single_task_planner, "dridh/code-reacher": coder}
    engine = engine_for(tmp_path, worker, settings)
    workflow_id = engine.create("Automatically retry task repair")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    result = asyncio.run(engine.run_to_end(workflow_id))
    assert result["outcome"] == "COMPLETED"
    assert coder_calls == 2
    assert worker.session_inputs[-2:] == [None, "ses_testsession0001"]
    status = engine.status(workflow_id)
    assert status["plan"]["tasks"][0]["attempts"] == 2
    assert status["plan"]["tasks"][0]["state"] == "DONE"


def test_task_retry_budget_exhaustion_surfaces_human_blocker(tmp_path: Path, settings) -> None:
    worker = Worker(tmp_path)

    def single_attempt_planner(worker: Worker, prompt: str) -> str:
        document = plan_document()
        document["stages"][0]["tasks"] = [document["stages"][0]["tasks"][0]]
        document["stages"][0]["tasks"][0]["max_attempts"] = 1
        return json.dumps(document)

    def failing_coder(worker: Worker, prompt: str) -> str:
        (worker.root / "src").mkdir(exist_ok=True)
        (worker.root / "src" / "greeter.py").write_text("MESSAGE = 'always fails acceptance'\n")
        return json.dumps({"status": "DONE", "summary": "invalid output", "files_changed": ["src/greeter.py"]})

    worker.behaviour = {"dridh/planner": single_attempt_planner, "dridh/code-reacher": failing_coder}
    engine = engine_for(tmp_path, worker, settings)
    workflow_id = engine.create("Escalate after the task repair budget")["workflow_id"]
    asyncio.run(engine.plan(workflow_id))
    engine.approve(workflow_id, reviewer="owner", method="terminal-confirmation")
    outcome = asyncio.run(engine.execute_task(workflow_id, "T-001"))
    assert outcome["outcome"] == "FAILED"
    assert outcome["blocked_for_human"] is True
    assert outcome["escalation"] == "HUMAN_REQUIRED"
    status = engine.status(workflow_id)
    assert status["failure"]["blocked_for_human"] is True
    assert status["plan"]["tasks"][0]["blocked_for_human"] is True
    with StateStore(open_workspace(tmp_path).state_db, open_workspace(tmp_path).workspace_id) as store:
        event = store.read().execute(
            "SELECT payload FROM ledger WHERE workflow_id=? AND event_type='TASK_REPAIR_BLOCKED'",
            (workflow_id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["reason"] == "human intervention required"
