"""Story runner and run controller, with a scripted agent against real Git worktrees."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.config.dridh_yaml import ExecutionConfig
from dridh.executor.controller import ExecutionController
from dridh.executor.story_runner import StoryRunner
from dridh.integrations.opencode import AgentErrorCode, AgentRunError
from dridh.state.lifecycle import LifecycleRepository
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline

PASSING_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


class FakePlanner:
    """In-memory stand-in for the approved plan (the real one is covered by the packaged E2E)."""

    def __init__(self, stories: list[dict[str, Any]]) -> None:
        self.stories = {s["story_id"]: dict(s, status="NOT_STARTED") for s in stories}
        self.history: list[dict[str, str]] = []

    def gate(self, session_id: str) -> dict[str, Any]:
        return {"unlocked": True, "plan_digest": "a" * 64}

    def execution_stories(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(s) for s in self.stories.values()]

    def update_story_statuses(self, session_id: str, statuses: dict[str, str], *, actor: str) -> None:
        self.history.append(dict(statuses))
        for story_id, status in statuses.items():
            self.stories[story_id]["status"] = status


def _story(story_id: str, deps: tuple[str, ...] = (), owns: str = "src") -> dict[str, Any]:
    return {"story_id": story_id, "title": f"Story {story_id}", "description": "", "component_id": "CMP-01", "use_case_ids": [],
            "todos": ["do it"], "acceptance_criteria": ["works"], "definition_of_done": ["tests pass"],
            "file_ownership": [owns], "infrastructure": False, "dependencies": list(deps), "key": story_id.lower()}


class ScriptedAgent:
    """Writes the files a script says, in the worktree it is given, and replies like the Executor."""

    def __init__(self, script) -> None:
        self.script, self.calls = script, []

    async def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        story_id = kwargs["title"].split()[-1]
        reply = self.script(story_id, len(self.calls), Path(kwargs["worktree_path"]), kwargs)
        return SimpleNamespace(text=json.dumps(reply), session_id=f"ses_{story_id}")


def _good(story_id: str, call: int, root: Path, kwargs: dict) -> dict:
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / f"{story_id.lower()}.py").write_text("VALUE = 1\n")
    (root / "tests" / f"test_{story_id.lower().replace('-', '_')}.py").write_text(PASSING_TEST)
    return {"status": "DONE", "summary": "implemented", "files_changed": [f"src/{story_id.lower()}.py"]}


@pytest.fixture()
def env(tmp_path):
    info, _session = seed_approved_baseline(tmp_path / "project")
    _git(info.project_root, "init", "-q")
    _git(info.project_root, "config", "user.email", "t@example.com")
    _git(info.project_root, "config", "user.name", "t")
    (info.project_root / ".gitignore").write_text(".opencode/\n__pycache__/\n.pytest_cache/\n")
    (info.project_root / "pytest.ini").write_text("[pytest]\n")
    _git(info.project_root, "add", "-A")
    _git(info.project_root, "commit", "-q", "-m", "base")
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        feature = LifecycleRepository(connection, info.workspace_id).create(slug="f", name="F", mode="new", actor="t")
    return info, feature


def approve(controller, run_id, ordinal, verdict="approved", notes=""):
    challenge = controller.review_challenge(run_id, ordinal, verdict)
    return controller.review_lot(run_id, ordinal, verdict=verdict, reviewer="h", notes=notes, confirmation=challenge["confirmation"])


def finish(controller, run_id, limit=20):
    """Drive, approve every waiting lot and resume until the run ends."""
    for _ in range(limit):
        result = controller.status(run_id)
        state = result["run"]["status"]
        if state == "RUNNING":
            result = asyncio.run(controller.drive(run_id))
        elif state == "PAUSED":
            waiting = [lot for lot in result["lots"] if lot["status"] == "AWAITING_REVIEW" and lot["kind"] != "STARVATION"]
            for lot in waiting:
                approve(controller, run_id, lot["ordinal"])
            if not waiting and any(lot["status"] == "AWAITING_REVIEW" for lot in result["lots"]):
                return result
            if controller.status(run_id)["run"]["status"] == "PAUSED":
                controller.resume(run_id, actor="t")
        else:
            return result
    return controller.status(run_id)


def _controller(info, planner, agent, **settings):
    config = ExecutionConfig(test_timeout_seconds=120, containers={"mode": "off"}, test_command=["python3", "-m", "unittest", "discover", "-s", "tests"], **settings)
    runner = StoryRunner(info, agent, config)
    return ExecutionController(info, planner=planner, story_runner=runner, settings=config)


def _run(info, feature, **kwargs):
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        return RunRepository(connection, info.workspace_id).create_run(
            feature_id=feature["id"], planner_session_id="planner-aaaaaaaaaaaa", plan_digest="a" * 64,
            max_parallel=kwargs.get("max_parallel", 2), max_repairs=kwargs.get("max_repairs", 3), confirmed_by="h", actor="human:h")


def test_independent_stories_pass_and_wait_for_review(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001"), _story("ST-002", owns="src"), _story("ST-003", deps=("ST-001",))])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    result = asyncio.run(controller.drive(run["id"]))
    assert {s: planner.stories[s]["status"] for s in planner.stories} == {"ST-001": "IN_REVIEW", "ST-002": "IN_REVIEW", "ST-003": "NOT_STARTED"}
    assert result["lots"][0]["kind"] == "PATHFINDER" and result["lots"][0]["status"] == "AWAITING_REVIEW"
    assert result["run"]["status"] == "PAUSED" and "Checkpoint lot 1" in result["run"]["pause_reason"]
    branches = _git(info.project_root, "branch", "--list", "dridh/story/*")
    assert "st-001" in branches and "st-002" in branches
    assert "src/st-001.py" in _git(info.project_root, "show", "--stat", "--format=", f"dridh/story/st-001-{run['id'][:8]}")
    assert _git(info.project_root, "status", "--porcelain") == ""  # the main checkout was never touched


def test_a_dependent_starts_only_after_its_prerequisite_is_released_and_sees_its_commit(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001"), _story("ST-003", deps=("ST-001",))])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    assert planner.stories["ST-003"]["status"] == "NOT_STARTED"
    approve(controller, run["id"], 1)
    controller.resume(run["id"], actor="t")
    seen = {}

    def check_prerequisite(story_id, call, root, kwargs):
        seen[story_id] = (root / "src" / "st-001.py").is_file()
        return _good(story_id, call, root, kwargs)

    controller._story_runner.runner = ScriptedAgent(check_prerequisite)  # noqa: SLF001
    result = asyncio.run(controller.drive(run["id"]))
    assert seen == {"ST-003": True}
    assert planner.stories["ST-003"]["status"] == "IN_REVIEW" and result["run"]["status"] == "PAUSED"


def test_failing_tests_are_repaired_within_budget_then_pass(env):
    info, feature = env

    def flaky(story_id, call, root, kwargs):
        reply = _good(story_id, call, root, kwargs)
        if call == 1:
            (root / "tests" / "test_st_001.py").write_text(FAILING_TEST)
        else:
            assert "previous attempt failed" in kwargs["prompt"] and kwargs["session_id"] == "ses_ST-001"
        return reply

    planner = FakePlanner([_story("ST-001")])
    run = _run(info, feature)
    asyncio.run(_controller(info, planner, ScriptedAgent(flaky)).drive(run["id"]))
    assert planner.stories["ST-001"]["status"] == "IN_REVIEW"
    with StateStore(info.state_db, info.workspace_id) as store:
        attempts = RunRepository(store.read(), info.workspace_id).attempts(run["id"], "ST-001")
    assert [(a["kind"], a["status"]) for a in attempts] == [("IMPLEMENT", "FAILED"), ("REPAIR", "PASSED")]
    assert attempts[1]["commit_sha"]


def test_repair_budget_ends_blocked_with_evidence(env):
    info, feature = env

    def always_failing(story_id, call, root, kwargs):
        reply = _good(story_id, call, root, kwargs)
        (root / "tests" / "test_st_001.py").write_text(FAILING_TEST)
        return reply

    planner = FakePlanner([_story("ST-001"), _story("ST-002", deps=("ST-001",))])
    agent = ScriptedAgent(always_failing)
    run = _run(info, feature, max_repairs=1)
    result = asyncio.run(_controller(info, planner, agent, max_repair_attempts=1).drive(run["id"]))
    assert len(agent.calls) == 2  # one attempt plus one repair, never more
    assert planner.stories["ST-001"]["status"] == "BLOCKED" and planner.stories["ST-002"]["status"] == "NOT_STARTED"
    assert result["run"]["status"] == "PAUSED" and "starving" in result["run"]["pause_reason"]


def test_new_mocks_and_out_of_scope_edits_fail_the_attempt(env):
    info, feature = env

    def mocking(story_id, call, root, kwargs):
        reply = _good(story_id, call, root, kwargs)
        (root / "tests" / "test_mock.py").write_text("from unittest.mock import MagicMock\n")
        (root / "docs.md").write_text("outside ownership\n")
        return reply

    planner = FakePlanner([_story("ST-001")])
    agent = ScriptedAgent(mocking)
    run = _run(info, feature, max_repairs=0)
    asyncio.run(_controller(info, planner, agent, max_repair_attempts=0).drive(run["id"]))
    with StateStore(info.state_db, info.workspace_id) as store:
        evidence = RunRepository(store.read(), info.workspace_id).attempts(run["id"])[0]["evidence"]
    joined = " ".join(evidence["findings"])
    assert planner.stories["ST-001"]["status"] == "BLOCKED"
    assert "New mocks are not allowed" in joined and "outside the story's ownership" in joined


def test_provider_failures_are_not_repaired(env):
    info, feature = env

    class Down:
        calls = 0

        async def run(self, **kwargs):
            Down.calls += 1
            raise AgentRunError(AgentErrorCode.PROVIDER_FAILURE if hasattr(AgentErrorCode, "PROVIDER_FAILURE") else list(AgentErrorCode)[0], "quota exceeded")

    planner = FakePlanner([_story("ST-001")])
    run = _run(info, feature)
    asyncio.run(_controller(info, planner, Down()).drive(run["id"]))
    assert Down.calls == 1 and planner.stories["ST-001"]["status"] == "BLOCKED"


def test_a_completed_plan_completes_the_run_and_parallelism_is_bounded(env):
    info, feature = env
    active = {"now": 0, "max": 0}

    class Slow:
        async def run(self, **kwargs):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.2)
            active["now"] -= 1
            story_id = kwargs["title"].split()[-1]
            return SimpleNamespace(text=json.dumps(_good(story_id, 1, Path(kwargs["worktree_path"]), kwargs)), session_id="s")

    planner = FakePlanner([_story(f"ST-00{n}") for n in range(1, 5)])
    run = _run(info, feature, max_parallel=2)
    asyncio.run(_controller(info, planner, Slow(), max_parallel_stories=2).drive(run["id"]))
    assert active["max"] == 2
    controller = _controller(info, planner, ScriptedAgent(_good))
    assert finish(controller, run["id"])["run"]["status"] == "COMPLETED"
    assert {s["status"] for s in planner.stories.values()} == {"COMPLETED"}


def test_start_needs_the_exact_typed_confirmation_and_an_approved_plan(env, monkeypatch):
    info, feature = env
    planner = FakePlanner([_story("ST-001")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    monkeypatch.setattr(controller, "_context", lambda ref: ({"id": feature["id"], "slug": "f"}, "planner-aaaaaaaaaaaa"))
    challenge = controller.challenge("f")
    assert challenge["confirmation"] == f"EXECUTE f {'a' * 12}"
    with pytest.raises(DridhError, match="did not match"):
        controller.start("f", confirmed_by="h", confirmation="EXECUTE")
    assert controller.start("f", confirmed_by="h", confirmation=challenge["confirmation"])["status"] == "RUNNING"
    with pytest.raises(DridhError, match="already has an active"):
        controller.start("f", confirmed_by="h", confirmation=challenge["confirmation"])
    planner.gate = lambda session_id: {"unlocked": False, "reason": "not approved"}  # type: ignore[method-assign]
    with pytest.raises(DridhError, match="not approved"):
        controller.challenge("f")


def test_rework_sends_the_whole_lot_back_with_the_reviewers_notes_and_returns_to_the_same_checkpoint(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001"), _story("ST-002", deps=("ST-001",))])
    agent = ScriptedAgent(_good)
    controller = _controller(info, planner, agent)
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    assert planner.stories["ST-001"]["status"] == "IN_REVIEW"
    with pytest.raises(DridhError, match="Say what must change"):
        approve(controller, run["id"], 1, "rework")
    approve(controller, run["id"], 1, "rework", notes="Handle the empty input case.")
    assert planner.stories["ST-001"]["status"] == "NOT_STARTED"
    controller.resume(run["id"], actor="t")
    result = asyncio.run(controller.drive(run["id"]))
    assert "Handle the empty input case." in agent.calls[-1]["prompt"] and "human reviewer" in agent.calls[-1]["prompt"]
    lot = result["lots"][0]
    assert lot["ordinal"] == 1 and lot["status"] == "AWAITING_REVIEW" and [r["verdict"] for r in lot["reviews"]] == ["REWORK_REQUIRED"]
    assert len(result["lots"]) == 1  # the same checkpoint is reviewed again, not a new one


def test_a_review_needs_the_exact_confirmation_and_cannot_be_decided_twice(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    with pytest.raises(DridhError, match="did not match"):
        controller.review_lot(run["id"], 1, verdict="approved", reviewer="h", notes="", confirmation="APPROVED")
    approve(controller, run["id"], 1)
    with pytest.raises(DridhError, match="not awaiting review"):
        controller.review_challenge(run["id"], 1, "approved")
    assert controller.status(run["id"])["run"]["status"] == "COMPLETED"  # the only story is complete


def test_thresholds_open_checkpoint_lots_by_stories_done_over_planned(env):
    info, feature = env
    planner = FakePlanner([_story(f"ST-00{n}") for n in range(1, 5)])
    controller = _controller(info, planner, ScriptedAgent(_good), max_parallel_stories=1)
    run = _run(info, feature, max_parallel=1)
    result = finish(controller, run["id"], limit=20)
    assert result["run"]["status"] == "COMPLETED"
    kinds = [(lot["kind"], lot["threshold_percent"], len(lot["story_ids"])) for lot in result["lots"]]
    assert kinds[0] == ("PATHFINDER", None, 1)           # after story 1
    assert ("THRESHOLD", 50, 1) in kinds                  # 2 of 4 done
    assert kinds[-1][0] == "FINAL" and kinds[-1][1] == 100  # 4 of 4 done
    assert all(lot["status"] == "APPROVED" for lot in result["lots"])


def test_blocked_stories_open_a_starvation_lot_that_cannot_be_approved(env):
    info, feature = env

    def flaky(story_id, call, root, kwargs):
        if story_id == "ST-001" and "human reviewer" not in kwargs["prompt"] and call <= 2:
            return {"status": "BLOCKED", "summary": "cannot", "files_changed": []}
        return _good(story_id, call, root, kwargs)

    planner = FakePlanner([_story("ST-001"), _story("ST-002", deps=("ST-001",))])
    controller = _controller(info, planner, ScriptedAgent(flaky), max_repair_attempts=0)
    run = _run(info, feature, max_repairs=0)
    result = asyncio.run(controller.drive(run["id"]))
    lot = result["lots"][-1]
    assert lot["kind"] == "STARVATION" and lot["story_ids"] == ["ST-001"] and "starving" in result["run"]["pause_reason"]
    with pytest.raises(DridhError, match="cannot be approved"):
        controller.review_challenge(run["id"], lot["ordinal"], "approved")
    approve(controller, run["id"], lot["ordinal"], "rework", notes="Try again.")
    assert planner.stories["ST-001"]["status"] == "NOT_STARTED"
    assert finish(controller, run["id"])["run"]["status"] == "COMPLETED"


def test_discard_removes_one_stories_worktree_and_branch_and_restarts_it(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001"), _story("ST-002")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    with pytest.raises(DridhError, match="Pause or stop"):
        controller.resume(run["id"], actor="t") and controller.discard_story(run["id"], "ST-001", reviewer="h")
    controller.stop(run["id"], actor="t")
    controller.discard_story(run["id"], "ST-001", reviewer="h")
    branches = _git(info.project_root, "branch", "--list", "dridh/story/*")
    assert "st-001" not in branches and "st-002" in branches
    assert planner.stories["ST-001"]["status"] == "NOT_STARTED" and planner.stories["ST-002"]["status"] == "IN_REVIEW"
    assert not (info.root / "worktrees" / f"exec-{run['id'][:8]}-st-001").exists()


def test_a_plan_that_stops_being_approved_pauses_the_run(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001"), _story("ST-002")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    original = planner.execution_stories

    def gone(session_id):
        raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The plan is not approved for execution.", details=("The plan definitions changed after human approval.",))

    planner.execution_stories = gone  # type: ignore[method-assign]
    result_status = None
    try:
        asyncio.run(controller.drive(run["id"]))
    except Exception:  # noqa: BLE001 - status() also reads the plan
        pass
    with StateStore(info.state_db, info.workspace_id) as store:
        result_status = RunRepository(store.read(), info.workspace_id).get_run(run["id"])
    assert result_status["status"] == "PAUSED" and "no longer valid" in result_status["pause_reason"]
    planner.execution_stories = original  # type: ignore[method-assign]


def test_every_passing_story_gets_an_advisory_jev_checkpoint_that_never_blocks(env, monkeypatch):
    info, feature = env
    for name in ("DRIDH_JEV_URL", "DRIDH_JEV_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    planner = FakePlanner([_story("ST-001"), _story("ST-002")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    result = asyncio.run(controller.drive(run["id"]))
    with StateStore(info.state_db, info.workspace_id) as store:
        evidence = [a["evidence"] for a in RunRepository(store.read(), info.workspace_id).attempts(run["id"]) if a["status"] == "PASSED"]
    assert evidence and all(item["jev"]["decision"] == "FALLBACK_DETERMINISTIC" and item["jev"]["advisory"] for item in evidence)
    assert result["jev_flagged_stories"] == [] and {s["status"] for s in planner.stories.values()} == {"IN_REVIEW"}


def test_a_flagged_jev_verdict_is_shown_to_the_reviewer_but_does_not_block_or_approve(env, monkeypatch):
    info, feature = env
    flagged = {"evaluation_id": "e", "status": "ANSWERED", "decision": "REPAIR", "readiness": 0.4, "used_fallback": False,
               "flagged": True, "advisory": True, "detail": "low readiness"}
    monkeypatch.setattr("dridh.jev.checkpoints.run_checkpoint", lambda *a, **k: flagged)
    planner = FakePlanner([_story("ST-001")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    result = asyncio.run(controller.drive(run["id"]))
    assert result["jev_flagged_stories"] == ["ST-001"]
    assert planner.stories["ST-001"]["status"] == "IN_REVIEW"  # not BLOCKED, and certainly not COMPLETED
    assert result["lots"][0]["status"] == "AWAITING_REVIEW"     # only a human verdict moves it on


def test_discarding_a_story_keeps_its_uncommitted_work_and_its_branch_tip(env):
    info, feature = env
    planner = FakePlanner([_story("ST-001")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    worktree = info.root / "worktrees" / f"exec-{run['id'][:8]}-st-001"
    (worktree / "src" / "st-001.py").write_text("VALUE = 'a human edit that was never committed'\n")
    (worktree / "notes.txt").write_text("scratch\n")
    committed = _git(info.project_root, "rev-parse", f"dridh/story/st-001-{run['id'][:8]}")
    controller.stop(run["id"], actor="t")
    result = controller.discard_story(run["id"], "ST-001", reviewer="h")
    kept = result["recovery"]
    assert not worktree.exists() and _git(info.project_root, "branch", "--list", "dridh/story/*") == ""
    assert _git(info.project_root, "show", f"{kept['snapshot']['ref']}:src/st-001.py") == "VALUE = 'a human edit that was never committed'"
    assert _git(info.project_root, "show", f"{kept['snapshot']['ref']}:notes.txt") == "scratch"
    assert kept["branch_tip"]["commit"] == committed and _git(info.project_root, "rev-parse", kept["branch_tip"]["ref"]) == committed
    with StateStore(info.state_db, info.workspace_id) as store:
        event = store.read().execute("SELECT payload FROM audit_event WHERE action='execution.discard'").fetchone()
        attempts = RunRepository(store.read(), info.workspace_id).attempts(run["id"], "ST-001")
    assert kept["snapshot"]["ref"] in event["payload"] and attempts  # the audit trail and the attempts remain
    assert planner.stories["ST-001"]["status"] == "NOT_STARTED"


# ------------------------------------------------------------ crash recovery (plan 17.4)


def _crashed_run(env):
    """A run left RUNNING with a story IN_PROGRESS and half-written files in its worktree, as after a kill -9 of the driver."""
    info, feature = env
    planner = FakePlanner([_story("ST-001"), _story("ST-002")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    planner.stories["ST-001"]["status"] = "IN_PROGRESS"
    return info, planner, controller, run


def test_a_run_whose_driver_died_is_recovered_and_finishes(env):
    info, planner, controller, run = _crashed_run(env)
    assert controller.resume(run["id"], actor="t")["status"] == "RUNNING"  # nothing holds the lock: the driver is gone
    asyncio.run(controller.drive(run["id"]))
    assert {s["status"] for s in planner.stories.values()} == {"IN_REVIEW"}
    with StateStore(info.state_db, info.workspace_id) as store:
        recovered = store.read().execute("SELECT payload FROM audit_event WHERE action='execution.recover'").fetchone()
    assert recovered is not None  # the recovery is on the audit trail


def test_a_run_with_a_live_driver_is_not_taken_over(env):
    import fcntl

    info, planner, controller, run = _crashed_run(env)
    lock_path = info.root / "locks"
    lock_path.mkdir(exist_ok=True)
    with (lock_path / f"run-{run['id']}.lock").open("a") as live:
        fcntl.flock(live, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DridhError, match="Another process is driving"):
            controller.resume(run["id"], actor="t")
        with pytest.raises(DridhError, match="already driving"):
            asyncio.run(controller.drive(run["id"]))


def test_killing_the_driving_process_releases_the_run(env):
    import signal
    import subprocess
    import sys
    import time

    info, planner, controller, run = _crashed_run(env)
    script = (
        "import sys, time\n"
        "from dridh.executor.controller import ExecutionController\n"
        "from dridh.workspace.layout import open_workspace\n"
        "controller = ExecutionController(open_workspace(sys.argv[1]))\n"
        "with controller._driver_lock(sys.argv[2]) as free:\n"
        "    print('LOCKED' if free else 'BUSY', flush=True)\n"
        "    time.sleep(300)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(info.project_root), run["id"]], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(DridhError, match="Another process is driving"):
            controller.resume(run["id"], actor="t")
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        deadline = time.monotonic() + 5
        while True:  # the operating system drops the lock with the process
            try:
                assert controller.resume(run["id"], actor="t")["status"] == "RUNNING"
                break
            except DridhError:
                assert time.monotonic() < deadline
                time.sleep(0.1)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


def _without_git_identity(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("user.name", "user.email"):
        subprocess.run(["git", "config", "--unset", key], cwd=root, check=False)


def test_a_commit_that_fails_closes_the_attempt_with_the_reason(env, monkeypatch):
    """Found in a real-model run on a cloned repository without a Git identity: the commit failed outside the story's error
    handling, the attempt stayed RUNNING forever and the user only saw 'blocked stories are starving the rest'."""
    info, feature = env
    planner = FakePlanner([_story("ST-001")])
    controller = _controller(info, planner, ScriptedAgent(_good))
    run = _run(info, feature)
    _without_git_identity(monkeypatch, info.project_root)
    asyncio.run(controller.drive(run["id"]))
    attempts = controller.status(run["id"])["attempts"]
    assert attempts and all(a["status"] != "RUNNING" for a in attempts)
    with StateStore(info.state_db, info.workspace_id) as store:
        evidence = RunRepository(store.connection, info.workspace_id).attempts(run["id"], "ST-001")[-1]["evidence"]
    assert any("could not verify or commit" in item for item in evidence["environment_error"])
    assert planner.stories["ST-001"]["status"] == "BLOCKED"


def test_execution_refuses_to_start_without_a_git_identity(env, monkeypatch):
    info, _feature = env
    controller = _controller(info, FakePlanner([_story("ST-001")]), ScriptedAgent(_good))
    _without_git_identity(monkeypatch, info.project_root)
    with pytest.raises(DridhError) as refused:
        controller.challenge("f")
    assert refused.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE and "user.name" in refused.value.message and "git config" in refused.value.recovery


def test_test_byproducts_are_not_a_storys_changes(env):
    """Found in the big-pickle run: a failed attempt's tests left __pycache__/*.pyc in the worktree; the next repair
    staged them and the ownership check rejected every repair."""
    info, feature = env

    def script(story_id, call, root, kwargs):
        reply = _good(story_id, call, root, kwargs)
        # The project's own .gitignore covers neither of these (it lists __pycache__/ and .pytest_cache/ only).
        (root / "src" / "leftover.cpython-313.pyc").write_bytes(b"\x00bytecode")
        (root / ".mypy_cache").mkdir(exist_ok=True)
        (root / ".mypy_cache" / "CACHEDIR.TAG").write_text("cache\n")
        return reply

    planner = FakePlanner([_story("ST-001", owns="src/st-001.py")])
    planner.stories["ST-001"]["file_ownership"] = ["src/st-001.py", "tests"]
    controller = _controller(info, planner, ScriptedAgent(script))
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    assert planner.stories["ST-001"]["status"] == "IN_REVIEW"
    with StateStore(info.state_db, info.workspace_id) as store:
        attempt = RunRepository(store.read(), info.workspace_id).attempts(run["id"], "ST-001")[0]
    assert not any(f.endswith(".pyc") or ".mypy_cache" in f for f in attempt["evidence"]["changed_files"])


def test_byproducts_staged_by_an_earlier_attempt_are_unstaged(tmp_path):
    from dridh.workspace.worktree import WorktreeManager, is_test_byproduct

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "kept.pyc").write_bytes(b"tracked on purpose")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"x")
    (root / "pkg" / "m.py").write_text("X = 1\n")
    (root / "kept.pyc").write_bytes(b"changed")
    _git(root, "add", "-A")  # an earlier attempt staged everything
    removed = WorktreeManager(root).unstage_test_byproducts(root)
    assert removed == ["pkg/__pycache__/m.cpython-313.pyc"]
    staged = _git(root, "diff", "--cached", "--name-only", "HEAD").split()
    assert sorted(staged) == ["kept.pyc", "pkg/m.py"]  # a tracked file is never touched
    assert is_test_byproduct("a/.pytest_cache/v/x") and not is_test_byproduct("src/cache.py")


def test_a_skipped_test_is_shown_to_the_reviewer(env):
    info, feature = env

    def script(story_id, call, root, kwargs):
        reply = _good(story_id, call, root, kwargs)
        (root / "tests" / "test_skips.py").write_text(
            "import unittest\n\n\nclass S(unittest.TestCase):\n    @unittest.skip('no network')\n    def test_install(self):\n        pass\n")
        return reply

    planner = FakePlanner([_story("ST-001")])
    planner.stories["ST-001"]["file_ownership"] = ["src", "tests"]
    controller = _controller(info, planner, ScriptedAgent(script))
    run = _run(info, feature)
    status = asyncio.run(controller.drive(run["id"]))
    assert planner.stories["ST-001"]["status"] == "IN_REVIEW"  # a skip never fails a story
    assert status["review_warnings"] == [{"story_id": "ST-001", "warning":
        "1 test(s) were skipped. Check that none of them is this story's acceptance test before approving."}]


def test_a_story_that_passes_after_the_thresholds_is_still_reviewed(env):
    """Found live: 25% and 50% lots were used, a story passed at 80%, and the last story depended on it. No checkpoint
    could open, so the run waited for a review that had no lot."""
    info, feature = env
    stories = [_story("ST-001", owns="src/st-001.py"), _story("ST-002", deps=("ST-001",), owns="src/st-002.py"),
               _story("ST-003", deps=("ST-002",), owns="src/st-003.py"), _story("ST-004", deps=("ST-003",), owns="src/st-004.py"),
               _story("ST-005", deps=("ST-004",), owns="src/st-005.py")]
    for story in stories:
        story["file_ownership"] = [story["file_ownership"][0], "tests"]
    planner = FakePlanner(stories)
    controller = _controller(info, planner, ScriptedAgent(_good), pathfinder_checkpoint=False, checkpoint_percentages=[25, 50, 100])
    run = _run(info, feature)
    result = finish(controller, run["id"])
    assert result["run"]["status"] == "COMPLETED", result["run"]
    assert all(s["status"] == "COMPLETED" for s in planner.stories.values())
