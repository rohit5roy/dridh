"""Category B: the delivery engine against REAL GitHub (opt-in).

Set DRIDH_LIVE_GITHUB_REPO=owner/throwaway-repo (a disposable repository you own). The test pushes story branches, opens real
PRs, waits for real Actions checks and integrates. It never merges anything, and its `finally` block closes every PR it opened
and deletes every remote branch except the default branch, even when an assertion fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from dridh.config.dridh_yaml import DeliveryConfig, ExecutionConfig
from dridh.delivery.engine import DeliveryEngine
from dridh.executor.controller import ExecutionController
from dridh.executor.story_runner import StoryRunner
from dridh.state.lifecycle import LifecycleRepository
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline
from tests.integration.test_executor_controller import FakePlanner, _story, approve

REPO = os.environ.get("DRIDH_LIVE_GITHUB_REPO", "")
pytestmark = pytest.mark.skipif(
    not REPO, reason="Category B (live GitHub, not enabled): set DRIDH_LIVE_GITHUB_REPO=owner/throwaway-repo")

WORKFLOW = """name: ci
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 -m unittest discover -s tests
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: test ! -f CI_FAIL
"""
PASSING = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"


def gh(*args: str) -> str:
    done = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[:300])
    return done.stdout


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True, timeout=300).stdout.strip()


def cleanup(project: Path) -> list[str]:
    """Close every PR and delete every remote branch except main. Returns what could not be removed (never raises)."""
    problems: list[str] = []
    try:
        for pull in json.loads(gh(f"repos/{REPO}/pulls?state=open&per_page=100") or "[]"):
            gh("-X", "PATCH", f"repos/{REPO}/pulls/{pull['number']}", "-f", "state=closed")
        branches = [b["name"] for b in json.loads(gh(f"repos/{REPO}/branches?per_page=100") or "[]") if b["name"] != "main"]
    except RuntimeError as exc:
        return [f"could not list: {exc}"]
    for branch in branches:
        try:
            gh("-X", "DELETE", f"repos/{REPO}/git/refs/heads/{branch}")
        except RuntimeError:
            pushed = subprocess.run(["git", "push", "origin", "--delete", branch], cwd=project, capture_output=True, text=True, check=False, timeout=120)
            if pushed.returncode != 0:
                problems.append(branch)
    return problems


def agent(fail_story: str):
    class Scripted:
        async def run(self, **kwargs):
            from types import SimpleNamespace

            story_id = kwargs["title"].split()[-1]
            root = Path(kwargs["worktree_path"])
            (root / "src").mkdir(exist_ok=True)
            (root / "tests").mkdir(exist_ok=True)
            (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (root / "src" / f"{story_id.lower()}.py").write_text("VALUE = 1\n")
            (root / "tests" / f"test_{story_id.lower().replace('-', '_')}.py").write_text(PASSING)
            (root / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
            if story_id == fail_story:
                (root / "CI_FAIL").write_text("make the gate job fail\n")
            return SimpleNamespace(text=json.dumps({"status": "DONE", "summary": "ok", "files_changed": []}), session_id=f"ses-{story_id}")
    return Scripted()


@pytest.fixture()
def live(tmp_path):
    """A fresh clone and workspace. Branch names are unique per run (feature slug, run id), so leftovers of an earlier
    run never collide; some sessions' GitHub proxies refuse branch deletion, so leftovers are reported, not fatal."""
    import uuid

    project = tmp_path / "project"
    subprocess.run(["git", "clone", "-q", f"https://github.com/{REPO}", str(project)], check=True, timeout=600)
    info, _ = seed_approved_baseline(project)
    git(project, "config", "user.email", "dridh-live@example.com")
    git(project, "config", "user.name", "dridh-live")
    (project / ".git" / "info" / "exclude").write_text(".opencode/\n001-USE-CASES.md\nfeature_tdd.md\n*.md\n!README.md\n__pycache__/\n")
    slug = f"live-{uuid.uuid4().hex[:6]}"
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        feature = LifecycleRepository(connection, info.workspace_id).create(slug=slug, name="Live", mode="new", actor="t")
    try:
        yield info, project, feature
    finally:
        leftover = cleanup(project)
        if leftover:
            print(f"\nLEFTOVER branches on {REPO} (delete them by hand): {leftover}")


def test_real_github_pr_checks_integration_and_a_failing_check(live):
    info, project, feature = live
    stories = [dict(_story(sid), file_ownership=["src", ".github", "CI_FAIL"]) for sid in ("ST-001", "ST-002")]
    planner = FakePlanner(stories)
    settings = ExecutionConfig(test_timeout_seconds=120, containers={"mode": "off"}, test_command=["python3", "-m", "unittest", "discover", "-s", "tests"])
    engine = DeliveryEngine(info, config=DeliveryConfig(enabled=True, poll_seconds=15, checks_timeout_seconds=900), execution=settings, planner=planner)
    controller = ExecutionController(info, planner=planner, story_runner=StoryRunner(info, agent("ST-002"), settings), settings=settings, delivery=engine)
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        run = RunRepository(connection, info.workspace_id).create_run(
            feature_id=feature["id"], planner_session_id="planner-aaaaaaaaaaaa", plan_digest="a" * 64, max_parallel=2, max_repairs=0,
            confirmed_by="live", actor="human:live")
    asyncio.run(controller.drive(run["id"]))
    for _ in range(6):
        if all(s["status"] == "COMPLETED" for s in planner.stories.values()):
            break
        status = controller.status(run["id"])
        for lot in [lot for lot in status["lots"] if lot["status"] == "AWAITING_REVIEW"]:
            approve(controller, run["id"], lot["ordinal"])
        if controller.status(run["id"])["run"]["status"] == "PAUSED":
            controller.resume(run["id"], actor="t")
            asyncio.run(controller.drive(run["id"]))

    passing = engine.create_pr(run["id"], "ST-001")
    assert passing["created"] and passing["base"] == "main" and passing["url"].startswith(f"https://github.com/{REPO}/pull/")
    start = time.monotonic()
    result = engine.wait_for_checks("ST-001")
    assert result["state"] == "PASS", (result, time.monotonic() - start)
    outcome = engine.integrate(run["id"], "ST-001")
    assert outcome["unlocked"] is True
    remote_tip = gh(f"repos/{REPO}/git/ref/heads/{outcome['branch']}", "--jq", ".object.sha").strip()
    assert remote_tip == outcome["integration_commit"]

    failing = engine.create_pr(run["id"], "ST-002")
    assert failing["created"]
    verdict = engine.wait_for_checks("ST-002")
    assert verdict["state"] == "FAIL", verdict
    with pytest.raises(Exception, match="no passing required checks"):
        engine.integrate(run["id"], "ST-002")
    assert "DEPENDENTS_UNLOCKED" not in engine.gates("ST-002")
    with StateStore(info.state_db, info.workspace_id) as store:
        names = {row["check_name"]: row["conclusion"] for row in store.read().execute("SELECT check_name, conclusion FROM pull_request_check")}
    assert names.get("build") == "success" and names.get("gate") in {"success", "failure"}


def repairing_agent():
    """Implements the story with a CI-only failure; when given the failing check, removes the cause."""
    class Repairing:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run(self, **kwargs):
            from types import SimpleNamespace

            self.prompts.append(kwargs["prompt"])
            root = Path(kwargs["worktree_path"])
            if "failed its required checks on GitHub" in kwargs["prompt"]:
                (root / "CI_FAIL").unlink()
                (root / "src" / "st_repair.py").write_text("VALUE = 2  # fixed after the gate check failed\n")
                return SimpleNamespace(text=json.dumps({"status": "DONE", "summary": "removed the CI blocker", "files_changed": []}), session_id="ses-repair")
            for directory in ("src", "tests", ".github/workflows"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            (root / "src" / "st_repair.py").write_text("VALUE = 1\n")
            (root / "tests" / "test_st_repair.py").write_text(PASSING)
            (root / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
            (root / "CI_FAIL").write_text("make the gate job fail\n")  # local tests pass; only CI fails
            return SimpleNamespace(text=json.dumps({"status": "DONE", "summary": "ok", "files_changed": []}), session_id="ses-impl")
    return Repairing()


def test_real_github_ci_repair_loop_and_an_observed_human_merge(live):
    """Live: a failing required check is repaired by the Executor with a new commit, the new head
    SHA is evaluated, the story integrates, and a merge made by a person on GitHub is observed as REMOTE_MERGED.

    The PR targets a scratch base branch made from main for this test, so the merge never touches the repository's main;
    the fixture's cleanup deletes it with every other branch."""
    info, project, feature = live
    base = f"dridh-live-base-{feature['slug']}"
    main_sha = gh(f"repos/{REPO}/git/ref/heads/main", "--jq", ".object.sha").strip()
    git(project, "push", "-q", "origin", f"{main_sha}:refs/heads/{base}")
    git(project, "fetch", "-q", "origin", base)
    stories = [dict(_story("ST-REPAIR"), file_ownership=["src", "tests", ".github", "CI_FAIL"])]
    planner = FakePlanner(stories)
    settings = ExecutionConfig(test_timeout_seconds=120, containers={"mode": "off"}, test_command=["python3", "-m", "unittest", "discover", "-s", "tests"])
    engine = DeliveryEngine(info, planner=planner, execution=settings, config=DeliveryConfig(
        enabled=True, target_branch=base, required_checks=["build", "gate"], poll_seconds=15, checks_timeout_seconds=900, max_ci_repairs=2))
    agent_ = repairing_agent()
    runner = StoryRunner(info, agent_, settings)
    controller = ExecutionController(info, planner=planner, story_runner=runner, settings=settings, delivery=engine)
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        run = RunRepository(connection, info.workspace_id).create_run(
            feature_id=feature["id"], planner_session_id="planner-aaaaaaaaaaaa", plan_digest="a" * 64, max_parallel=1, max_repairs=0,
            confirmed_by="live", actor="human:live")
    asyncio.run(controller.drive(run["id"]))
    for _ in range(4):
        if planner.stories["ST-REPAIR"]["status"] == "COMPLETED":
            break
        for lot in [lot for lot in controller.status(run["id"])["lots"] if lot["status"] == "AWAITING_REVIEW"]:
            approve(controller, run["id"], lot["ordinal"])
        if controller.status(run["id"])["run"]["status"] == "PAUSED":
            controller.resume(run["id"], actor="t")
            asyncio.run(controller.drive(run["id"]))
    assert planner.stories["ST-REPAIR"]["status"] == "COMPLETED"

    pr = engine.create_pr(run["id"], "ST-REPAIR")
    assert pr["created"] and pr["base"] == base
    first_sha = engine.wait_for_checks("ST-REPAIR")
    assert first_sha["state"] == "FAIL" and any(f["name"] == "gate" for f in first_sha["failures"]), first_sha
    number = int(pr["number"])

    result = engine.repair_until_green(run["id"], "ST-REPAIR", runner=runner)
    assert result["state"] == "PASS" and result["repaired"] is True and result["rounds"] == 1, result
    assert any("Check 'gate' concluded failure" in prompt for prompt in agent_.prompts)
    new_head = gh(f"repos/{REPO}/pulls/{number}", "--jq", ".head.sha").strip()
    assert new_head == result["head_sha"] != first_sha["head_sha"]
    with StateStore(info.state_db, info.workspace_id) as store:
        attempts = RunRepository(store.read(), info.workspace_id).attempts(run["id"], "ST-REPAIR")
    assert [a["evidence"].get("trigger") for a in attempts if a["kind"] == "REPAIR"] == ["CI"]

    outcome = engine.integrate(run["id"], "ST-REPAIR")
    assert outcome["unlocked"] is True
    assert "REMOTE_MERGED" not in engine.gates("ST-REPAIR")  # Dridh never merges and never waits for a merge

    # The person merges on GitHub (Dridh has no merge operation); Dridh observes it on the next refresh.
    try:
        assert json.loads(gh("-X", "PUT", f"repos/{REPO}/pulls/{number}/merge", "-f", "merge_method=merge", "-f", f"sha={new_head}"))["merged"]
        how = "merge button (API)"
    except RuntimeError:  # some session proxies refuse the merge API; a fast-forward push is a merge GitHub records too
        git(project, "push", "-q", "origin", f"{new_head}:refs/heads/{base}")
        how = "fast-forward push of the PR head"
    for _ in range(20):
        if json.loads(gh(f"repos/{REPO}/pulls/{number}")).get("merged"):
            break
        time.sleep(3)
    print(f"\nmerged by {how}")
    engine.refresh_checks("ST-REPAIR")
    gates = engine.gates("ST-REPAIR")
    assert gates["REMOTE_MERGED"]["status"] == "PASS" and gates["REMOTE_MERGED"]["sha"] == new_head
    assert gh(f"repos/{REPO}/git/ref/heads/main", "--jq", ".object.sha").strip() == main_sha  # main untouched
