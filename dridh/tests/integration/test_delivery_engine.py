"""Delivery against a real local bare remote and a fake GitHub adapter."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dridh.common.errors import DridhError
from dridh.config.dridh_yaml import DeliveryConfig, ExecutionConfig
from dridh.delivery.engine import DeliveryEngine
from dridh.executor.controller import ExecutionController
from dridh.executor.story_runner import StoryRunner, story_branch
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from tests.integration.test_executor_controller import (  # noqa: F401, F811 - env is a pytest fixture
    FakePlanner,
    ScriptedAgent,
    _good,
    _run,
    _story,
    approve,
    env,
)


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


class FakeGitHub:
    def __init__(self, root: Path) -> None:
        self.root, self.prs, self.checks, self.heads = root, {}, [{"name": "ci", "status": "completed", "conclusion": "success"}], {}

    def create_pr(self, *, title, body, head, base, draft=False):
        number = len(self.prs) + 1
        sha = git(self.root, "rev-parse", head)
        self.prs[number] = {"title": title, "body": body, "head": head, "base": base}
        self.heads[number] = sha
        return {"number": number, "url": f"https://github.com/o/r/pull/{number}", "head_sha": sha, "base_sha": "b"}

    def pr_view(self, number):
        return {"state": "OPEN", "head_sha": self.heads[number], "base_ref": self.prs[number]["base"], "merged": False}

    def check_runs(self, sha):
        return self.checks


class StubIndexer:
    """Records which commit was indexed; the engine then reads the index revision from SQLite."""

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def index(self, *, root: Path) -> dict[str, Any]:
        from dridh.code_reacher.indexer import CodeIndexer

        return CodeIndexer(self.workspace).index(root=root, embed=False)


@pytest.fixture()
def world(env, tmp_path):  # noqa: F811 - `env` is the pytest fixture imported above
    info, feature = env
    root = info.project_root
    git(root, "branch", "-M", "main")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "origin", "main")
    planner = FakePlanner([_story("ST-001"), _story("ST-002"), _story("ST-003", deps=("ST-001",)), _story("ST-004", deps=("ST-001", "ST-002"))])
    settings = ExecutionConfig(test_timeout_seconds=120, containers={"mode": "off"}, test_command=["python3", "-m", "unittest", "discover", "-s", "tests"])
    github = FakeGitHub(root)
    engine = DeliveryEngine(info, config=DeliveryConfig(enabled=True, poll_seconds=0.01, checks_timeout_seconds=0.05), execution=settings,
                            planner=planner, adapter=github, indexer=StubIndexer(info))
    controller = ExecutionController(info, planner=planner, story_runner=StoryRunner(info, ScriptedAgent(_good), settings),
                                     settings=settings, delivery=engine)
    run = _run(info, feature)
    return info, root, remote, planner, github, engine, controller, run


def _reviewed(controller, run_id, planner, wanted):
    """Drive and approve lots until every story in ``wanted`` is COMPLETED (delivery is not run here)."""
    for _ in range(12):
        status = controller.status(run_id)
        if all(planner.stories[s]["status"] == "COMPLETED" for s in wanted):
            return
        if status["run"]["status"] == "RUNNING":
            asyncio.run(controller.drive(run_id))
            continue
        starving = [lot for lot in status["lots"] if lot["kind"] == "STARVATION" and lot["status"] == "AWAITING_REVIEW"]
        if starving:  # a story was blocked: say why, instead of failing later with an unrelated message
            with StateStore(controller.workspace.state_db, controller.workspace.workspace_id) as store:
                attempts = RunRepository(store.read(), controller.workspace.workspace_id).attempts(run_id)
            reasons = {a["story_id"]: a["evidence"].get("findings") or a["evidence"].get("worktree_error") or a["evidence"].get("agent_error") or a["evidence"] for a in attempts if a["status"] == "FAILED"}
            raise AssertionError(f"stories were blocked: {reasons}")
        for lot in [lot for lot in status["lots"] if lot["status"] == "AWAITING_REVIEW"]:
            approve(controller, run_id, lot["ordinal"])
        if controller.status(run_id)["run"]["status"] == "PAUSED":
            controller.resume(run_id, actor="t")
    raise AssertionError("stories were not reviewed")


def _deliver(engine, run_id, story_id):
    pr = engine.create_pr(run_id, story_id)
    assert engine.wait_for_checks(story_id, sleep=lambda s: None)["state"] == "PASS"
    return pr, engine.integrate(run_id, story_id)


def test_full_delivery_stacks_prs_and_unlocks_dependents_in_order(world):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    assert planner.stories["ST-003"]["status"] == "NOT_STARTED"
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    # roots open against the target branch; the first delivery creates the integration branch
    pr1, out1 = _deliver(engine, rid, "ST-001")
    pr2, out2 = _deliver(engine, rid, "ST-002")
    assert (pr1["base"], pr2["base"]) == ("main", "main") and out1["unlocked"] and out2["unlocked"]
    assert git(remote, "branch", "--list", "dridh/integration/f").strip().endswith("dridh/integration/f")
    assert engine.gates("ST-001")["DEPENDENTS_UNLOCKED"]["status"] == "PASS"
    # a dependent now starts from the integrated baseline and is opened on the integration branch (join of two)
    _reviewed(controller, rid, planner, ["ST-003", "ST-004"])
    assert planner.stories["ST-003"]["status"] == "COMPLETED"
    pr3, out3 = _deliver(engine, rid, "ST-003")
    pr4, out4 = _deliver(engine, rid, "ST-004")
    assert pr3["base"] == story_branch(rid, "ST-001")       # one dependency whose branch holds the full ancestry
    assert pr4["base"] == "dridh/integration/f"             # a diamond joins on the shared integration branch
    assert out3["unlocked"] and out4["unlocked"]
    tree = git(remote, "ls-tree", "-r", "--name-only", "dridh/integration/f").split()
    assert {"src/st-001.py", "src/st-002.py", "src/st-003.py", "src/st-004.py"} <= set(tree)
    tip = git(remote, "rev-parse", "dridh/integration/f")
    assert out4["integration_commit"] == tip and engine._index_revision() == tip  # noqa: SLF001 - index matches the integration commit
    assert git(remote, "rev-parse", "main") == git(root, "rev-parse", "main")  # nothing was merged into the target branch
    assert "Acceptance criteria" in github.prs[1]["body"] and "ST-001" in github.prs[3]["body"]


def test_pending_or_failed_checks_block_integration(world):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    engine.create_pr(rid, "ST-001")
    github.checks = [{"name": "ci", "status": "in_progress", "conclusion": None}]
    assert engine.refresh_checks("ST-001")["state"] == "PENDING"
    with pytest.raises(DridhError, match="no passing required checks"):
        engine.integrate(rid, "ST-001")
    github.checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
    assert engine.wait_for_checks("ST-001", sleep=lambda s: None)["state"] == "FAIL"
    with pytest.raises(DridhError):
        engine.integrate(rid, "ST-001")
    assert "DEPENDENTS_UNLOCKED" not in engine.gates("ST-001")


def test_a_moved_pr_head_withdraws_eligibility(world):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    pr = engine.create_pr(rid, "ST-001")
    assert engine.refresh_checks("ST-001")["state"] == "PASS"
    github.heads[pr["number"]] = "f" * 40  # someone pushed another commit to the PR branch
    stale = engine.refresh_checks("ST-001")
    assert stale["state"] == "STALE" and engine.gates("ST-001")["GHA_PASSED"]["status"] == "FAIL"
    with pytest.raises(DridhError):
        engine.integrate(rid, "ST-001")


def test_integration_needs_a_reviewed_story_and_a_diverged_remote_is_never_forced(world, tmp_path):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    with pytest.raises(DridhError, match="not been approved in a reviewed lot|not passed"):
        engine.integrate(rid, "ST-001")
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    _deliver(engine, rid, "ST-001")
    # a third party advances the remote integration branch
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "x@example.com")
    git(other, "config", "user.name", "x")
    git(other, "checkout", "-q", "dridh/integration/f")
    (other / "OTHER.md").write_text("remote-only change\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "remote change")
    git(other, "push", "-q", "origin", "dridh/integration/f")
    remote_tip = git(remote, "rev-parse", "dridh/integration/f")
    pr, out = _deliver(engine, rid, "ST-002")
    assert out["unlocked"] is True  # a fast-forward of the local branch from the remote, then a normal push
    assert git(remote, "merge-base", "--is-ancestor", remote_tip, "dridh/integration/f") == ""  # the third party's commit survived
    assert "OTHER.md" in git(remote, "ls-tree", "-r", "--name-only", "dridh/integration/f")


def test_a_merge_conflict_blocks_without_unlocking(world):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]

    def conflicting(story_id, call, worktree, kwargs):
        reply = _good(story_id, call, worktree, kwargs)
        (worktree / "src" / "shared.py").write_text(f"VALUE = '{story_id}'\n")
        return reply

    controller._story_runner.runner = ScriptedAgent(conflicting)  # noqa: SLF001
    asyncio.run(controller.drive(rid))
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    _pr, first = _deliver(engine, rid, "ST-001")
    assert first["unlocked"] is True
    engine.create_pr(rid, "ST-002")
    engine.wait_for_checks("ST-002", sleep=lambda s: None)
    blocked = engine.integrate(rid, "ST-002")
    assert blocked["integrated"] is False and "conflicts" in blocked["blocked"]
    assert engine.gates("ST-002")["LOCALLY_INTEGRATED"]["status"] == "BLOCKED" and "DEPENDENTS_UNLOCKED" not in engine.gates("ST-002")
    assert git(root, "branch", "--list", story_branch(rid, "ST-002"))  # the story branch is preserved


def test_true_divergence_is_reported_and_never_forced(world, tmp_path):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    _deliver(engine, rid, "ST-001")
    local = info.project_root / ".opencode" / "dridh" / "worktrees" / "integration-f"
    (local / "LOCAL.md").write_text("local only\n")
    git(local, "add", "-A")
    git(local, "-c", "user.email=l@example.com", "-c", "user.name=l", "commit", "-q", "-m", "local only")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "checkout", "-q", "dridh/integration/f")
    (other / "REMOTE.md").write_text("remote only\n")
    git(other, "add", "-A")
    git(other, "-c", "user.email=r@example.com", "-c", "user.name=r", "commit", "-q", "-m", "remote only")
    git(other, "push", "-q", "origin", "dridh/integration/f")
    before = git(remote, "rev-parse", "dridh/integration/f")
    engine.create_pr(rid, "ST-002")
    engine.wait_for_checks("ST-002", sleep=lambda s: None)
    with pytest.raises(DridhError, match="diverged"):
        engine.integrate(rid, "ST-002")
    assert git(remote, "rev-parse", "dridh/integration/f") == before


# ---------------------------------------------------------------- CI failure repair (plan 16.7)


class TreeGitHub(FakeGitHub):
    """Checks fail for any commit that still contains CI_FAIL, and succeed otherwise (head moves follow pushes)."""

    def __init__(self, root) -> None:
        super().__init__(root)
        self.seen: list[str] = []

    def pr_view(self, number):
        sha = git(self.root, "rev-parse", self.prs[number]["head"])
        self.heads[number] = sha
        return {"state": "OPEN", "head_sha": sha, "base_ref": self.prs[number]["base"], "merged": False}

    def check_runs(self, sha):
        self.seen.append(sha)
        broken = subprocess.run(["git", "cat-file", "-e", f"{sha}:CI_FAIL"], cwd=self.root, capture_output=True).returncode == 0
        run = {"name": "gate", "status": "completed", "conclusion": "failure" if broken else "success", "url": "https://github.com/o/r/runs/1",
               "summary": "CI_FAIL marker is present" if broken else ""}
        return [{"name": "build", "status": "completed", "conclusion": "success"}, run, dict(run)]


def _broken_story_world(world, fixer):
    """ST-001 ships a CI_FAIL marker (owned by the story) that the failing check complains about; ``fixer`` is the repair agent."""
    info, root, remote, planner, github, engine, controller, run = world
    planner.stories["ST-001"]["file_ownership"] = ["src", "CI_FAIL"]
    planner.stories["ST-002"]["file_ownership"] = ["src", "CI_FAIL"]

    def breaking(story_id, call, worktree, kwargs):
        reply = _good(story_id, call, worktree, kwargs)
        if story_id == "ST-001":
            (worktree / "CI_FAIL").write_text("broken\n")
        return reply

    controller._story_runner.runner = ScriptedAgent(breaking)  # noqa: SLF001
    tree = TreeGitHub(root)
    engine._adapter = tree  # noqa: SLF001
    asyncio.run(controller.drive(run["id"]))
    _reviewed(controller, run["id"], planner, ["ST-001", "ST-002"])
    repair_runner = StoryRunner(info, ScriptedAgent(fixer), controller.settings)
    return tree, repair_runner


def _remove_marker(story_id, call, worktree, kwargs):
    assert "failed its required checks" in kwargs["prompt"] and "gate" in kwargs["prompt"] and "CI_FAIL marker is present" in kwargs["prompt"]
    (worktree / "CI_FAIL").unlink()
    (worktree / "src" / "ci-fix.py").write_text("FIXED = True\n")
    return {"status": "DONE", "summary": "removed the marker", "files_changed": ["CI_FAIL", "src/ci-fix.py"]}


def test_a_failed_check_is_repaired_pushed_rechecked_and_then_integrated(world):
    info, root, remote, planner, github, engine, controller, run = world
    tree, repair_runner = _broken_story_world(world, _remove_marker)
    rid = run["id"]
    pr = engine.create_pr(rid, "ST-001")
    old_head = git(root, "rev-parse", story_branch(rid, "ST-001"))
    result = engine.repair_until_green(rid, "ST-001", runner=repair_runner, sleep=lambda s: None)
    assert result["state"] == "PASS" and result["repaired"] is True and result["rounds"] == 1
    new_head = git(root, "rev-parse", story_branch(rid, "ST-001"))
    assert new_head != old_head and git(remote, "rev-parse", story_branch(rid, "ST-001")) == new_head  # pushed as a fast-forward
    assert old_head in tree.seen and new_head in tree.seen  # checks were evaluated for each exact SHA
    with StateStore(info.state_db, info.workspace_id) as store:
        record = store.read().execute("SELECT head_sha, number FROM pull_request WHERE story_id='ST-001'").fetchone()
        attempts = RunRepository(store.read(), info.workspace_id).attempts(rid, "ST-001")
    assert record["head_sha"] == new_head and record["number"] == pr["number"]
    assert [a["kind"] for a in attempts if a["evidence"].get("trigger") == "CI"] == ["REPAIR"]
    assert engine.integrate(rid, "ST-001")["unlocked"] is True  # only now


def test_the_ci_repair_budget_is_bounded_and_nothing_unlocks(world):
    info, root, remote, planner, github, engine, controller, run = world

    def useless(story_id, call, worktree, kwargs):
        (worktree / "src" / "noise.py").write_text(f"X = {call}\n")  # a new commit, but the marker stays
        return {"status": "DONE", "summary": "tried", "files_changed": ["src/noise.py"]}

    tree, repair_runner = _broken_story_world(world, useless)
    rid = run["id"]
    engine.create_pr(rid, "ST-001")
    result = engine.repair_until_green(rid, "ST-001", runner=repair_runner, sleep=lambda s: None)
    assert result["state"] == "FAIL" and result["exhausted"] is True and result["rounds"] == engine.config.max_ci_repairs == 2
    again = engine.repair_failed_checks(rid, "ST-001", runner=repair_runner)  # a restart cannot grant more rounds
    assert again["exhausted"] is True and engine._ci_rounds(rid, "ST-001") == 2  # noqa: SLF001
    assert "DEPENDENTS_UNLOCKED" not in engine.gates("ST-001")
    with pytest.raises(DridhError, match="no passing required checks"):
        engine.integrate(rid, "ST-001")


def test_a_repair_that_changes_nothing_does_not_pretend_to_fix_the_pr(world):
    info, root, remote, planner, github, engine, controller, run = world

    def nothing(story_id, call, worktree, kwargs):
        return {"status": "DONE", "summary": "looked at it", "files_changed": []}

    tree, repair_runner = _broken_story_world(world, nothing)
    rid = run["id"]
    engine.create_pr(rid, "ST-001")
    head = git(root, "rev-parse", story_branch(rid, "ST-001"))
    step = engine.repair_failed_checks(rid, "ST-001", runner=repair_runner)
    assert step["repaired"] is False and any("no new commit" in f for f in step["findings"])
    assert git(root, "rev-parse", story_branch(rid, "ST-001")) == head and git(remote, "rev-parse", story_branch(rid, "ST-001")) == head


def test_failing_check_details_reach_the_gate_evidence(world):
    info, root, remote, planner, github, engine, controller, run = world
    tree, _ = _broken_story_world(world, _remove_marker)
    rid = run["id"]
    engine.create_pr(rid, "ST-001")
    assert engine.refresh_checks("ST-001")["state"] == "FAIL"
    with StateStore(info.state_db, info.workspace_id) as store:
        row = store.read().execute("SELECT evidence_json FROM story_gate WHERE story_id='ST-001' AND gate='GHA_PASSED'").fetchone()
    evidence = json.loads(row["evidence_json"])
    assert evidence["failures"][0]["name"] == "gate" and "CI_FAIL marker" in evidence["failures"][0]["summary"]


def test_deliver_story_repairs_a_failing_check_then_integrates_in_one_call(world):
    info, root, remote, planner, github, engine, controller, run = world
    tree, repair_runner = _broken_story_world(world, _remove_marker)
    result = engine.deliver_story(run["id"], "ST-001", runner=repair_runner, sleep=lambda s: None)
    assert result["checks"]["state"] == "PASS" and result["checks"]["rounds"] == 1 and result["integration"]["unlocked"] is True


def test_deliver_story_without_a_runner_or_repair_budget_stops_at_the_failing_check(world):
    info, root, remote, planner, github, engine, controller, run = world
    tree, repair_runner = _broken_story_world(world, _remove_marker)
    for runner in (None, repair_runner):
        engine.config.max_ci_repairs = 0 if runner else 2
        result = engine.deliver_story(run["id"], "ST-001", runner=runner, sleep=lambda s: None)
        assert result["checks"]["state"] == "FAIL" and result["integration"] is None
    assert "DEPENDENTS_UNLOCKED" not in engine.gates("ST-001")


def test_an_index_failure_after_integration_is_recorded_blocks_the_unlock_and_can_be_retried(world):
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    engine.create_pr(rid, "ST-001")
    engine.wait_for_checks("ST-001", sleep=lambda s: None)

    class Broken:
        def index(self, *, root):
            raise RuntimeError("tree-sitter grammar failed to load /tmp/secret-path")

    good_indexer, engine._indexer = engine._indexer, Broken()  # noqa: SLF001
    blocked = engine.integrate(rid, "ST-001")
    assert blocked["integrated"] is True and "failed to index" in blocked["blocked"]
    assert engine.gates("ST-001")["INDEXED"]["status"] == "FAIL" and "DEPENDENTS_UNLOCKED" not in engine.gates("ST-001")
    assert git(remote, "rev-parse", "dridh/integration/f") == blocked["integration_commit"]  # the push stands
    engine._indexer = good_indexer  # noqa: SLF001
    again = engine.integrate(rid, "ST-001")  # idempotent: merging an already merged story is a no-op
    assert again["unlocked"] is True and engine.gates("ST-001")["INDEXED"]["status"] == "PASS"


def test_github_lagging_behind_a_push_is_pending_not_a_moved_head(world):
    """Found live: GitHub reports the old PR head for a few seconds after Dridh pushes a CI repair. That is not a moved
    head (it is an ancestor of the pushed commit), so the checks wait instead of withdrawing eligibility."""
    info, root, remote, planner, github, engine, controller, run = world
    rid = run["id"]
    asyncio.run(controller.drive(rid))
    _reviewed(controller, rid, planner, ["ST-001", "ST-002"])
    pr = engine.create_pr(rid, "ST-001")
    pushed = github.heads[pr["number"]]
    github.heads[pr["number"]] = git(root, "rev-parse", f"{pushed}^").strip()  # GitHub still shows the parent
    lagging = engine.refresh_checks("ST-001")
    assert lagging["state"] == "PENDING" and "waiting for it to show" in lagging["reasons"][0]
    assert engine.gates("ST-001").get("GHA_PASSED", {}).get("status") != "FAIL"
    github.heads[pr["number"]] = pushed  # GitHub caught up
    assert engine.refresh_checks("ST-001")["state"] == "PASS"
