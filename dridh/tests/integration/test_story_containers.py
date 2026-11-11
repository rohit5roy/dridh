"""Story tests in real containers (recovery scenarios 10 and 11). Needs a Docker daemon."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from dridh.executor import containers as c

IMAGE = "python:3.13-slim"
PASSING = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        open('written-by-test.txt', 'w').write('x')\n"
FAILING = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"
# A service each story's tests reach by its Compose name; its reply names the story's own Compose project.
SERVICE_TEST = (
    "import os, unittest, urllib.request\n\n\nclass T(unittest.TestCase):\n    def test_service(self):\n"
    "        body = urllib.request.urlopen('http://svc:8000/marker.txt', timeout=10).read().decode().strip()\n"
    "        self.assertEqual(body, open('expected.txt').read().strip())\n"
)
COMPOSE = """services:
  svc:
    image: python:3.13-slim
    command: ["sh", "-c", "mkdir -p /srv && echo $$COMPOSE_PROJECT > /srv/marker.txt && cd /srv && exec python3 -m http.server 8000"]
    environment:
      COMPOSE_PROJECT: ${COMPOSE_PROJECT_NAME}
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/marker.txt')"]
      interval: 1s
      retries: 30
"""


def _docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [pytest.mark.containers, pytest.mark.skipif(not _docker_ready(), reason="Docker is not available")]


def _project(root: Path, test_source: str, name: str = "test_story.py") -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "tests" / name).write_text(test_source)
    return root


def _plan(workspace: str, run: str, story: str, root: Path, **kwargs) -> c.ContainerPlan:
    return c.ContainerPlan(image=IMAGE, setup=kwargs.pop("setup", []), command=["python3", "-m", "unittest", "discover", "-s", "tests"],
                           compose_file=c.find_compose_file(root, None), names=c.environment_names(workspace, run, story),
                           labels=c.labels(workspace, run, story), **kwargs)


def _leftovers(workspace: str) -> dict[str, list[str]]:
    out = {}
    for kind, args in (("containers", ["ps", "-aq"]), ("networks", ["network", "ls", "-q"]), ("volumes", ["volume", "ls", "-q"])):
        result = subprocess.run(["docker", *args, "--filter", f"label=io.dridh.workspace={workspace}"], capture_output=True, text=True)
        out[kind] = result.stdout.split()
    prefix = c.name_prefix(workspace).replace("-", "_")
    projects = subprocess.run(["docker", "ps", "-a", "--format", '{{.Label "com.docker.compose.project"}}'], capture_output=True, text=True)
    out["compose"] = [p for p in projects.stdout.split() if p.startswith(prefix)]
    return out


def _clean(workspace: str) -> bool:
    return not any(_leftovers(workspace).values())


def test_tests_run_in_a_labelled_container_as_the_caller_and_everything_is_removed(tmp_path):
    workspace = "wsrealrun01"
    root = _project(tmp_path / "w", PASSING)
    result = asyncio.run(c.run_in_container(_plan(workspace, "run1", "ST-001", root), root, timeout=180))
    assert result["passed"] is True and result["exit_code"] == 0, result["output"]
    record = result["containers"]
    assert record["mode"] == "docker" and record["image"] == IMAGE and record["status"] == "PASSED"
    assert record["image_digest"] and record["image_digest"].startswith("python@sha256:")
    assert record["network"].endswith("-net") and record["cleanup"]["networks"] == 1
    written = root / "written-by-test.txt"
    assert written.read_text() == "x" and written.stat().st_uid == os.getuid()
    assert _clean(workspace)


def test_a_failing_test_fails_the_story_and_still_cleans_up(tmp_path):
    workspace = "wsrealfail1"
    root = _project(tmp_path / "w", FAILING)
    result = asyncio.run(c.run_in_container(_plan(workspace, "run1", "ST-002", root), root, timeout=180))
    assert result["passed"] is False and result["exit_code"] == 1 and "test_bad" in result["output"]
    assert not result.get("infrastructure_failure") and _clean(workspace)


def test_a_timeout_kills_the_container(tmp_path):
    workspace = "wsrealtime1"
    root = _project(tmp_path / "w", "import time, unittest\n\n\nclass T(unittest.TestCase):\n    def test_slow(self):\n        time.sleep(120)\n")
    result = asyncio.run(c.run_in_container(_plan(workspace, "run1", "ST-003", root), root, timeout=8))
    assert result["timed_out"] is True and result["passed"] is False and result["exit_code"] is None
    assert _clean(workspace)


def test_scenario_10_a_container_that_cannot_start_is_an_infrastructure_failure(tmp_path):
    workspace = "wsrealstart"
    root = _project(tmp_path / "w", PASSING)
    plan = _plan(workspace, "run1", "ST-004", root, memory="1k")  # below Docker's minimum: the container is never created
    result = asyncio.run(c.run_in_container(plan, root, timeout=120))
    assert result["infrastructure_failure"] is True and result["passed"] is False
    assert result["containers"]["infrastructure_failure"] is True and "did not start" in result["output"]
    assert _clean(workspace)


def test_a_failing_setup_step_is_an_environment_failure_not_a_test_failure(tmp_path):
    """Found in the big-pickle run: pip could not reach PyPI from the container, and Dridh reported the story's tests as
    failing, so the Executor tried to repair correct code."""
    workspace = "wssetupfail"
    root = _project(tmp_path / "w", PASSING)
    plan = _plan(workspace, "run1", "ST-005", root, setup=["echo preparing", "pip install --index-url http://127.0.0.1:9/ nothing-here -q"])
    result = asyncio.run(c.run_in_container(plan, root, timeout=180))
    assert result["infrastructure_failure"] is True and result["passed"] is False
    assert "could not be prepared" in result["output"] and "setup step failed: pip install" in result["output"]
    assert _clean(workspace)


def test_scenario_11_parallel_stories_get_their_own_compose_project_network_and_service(tmp_path):
    workspace = "wsparallel1"
    plans, roots = [], []
    for story in ("ST-010", "ST-011"):
        root = _project(tmp_path / story, SERVICE_TEST)
        (root / "compose.test.yml").write_text(COMPOSE)
        plan = _plan(workspace, "run1", story, root)
        (root / "expected.txt").write_text(plan.names["project"] + "\n")
        plans.append(plan)
        roots.append(root)

    async def both():
        return await asyncio.gather(*(c.run_in_container(p, r, timeout=240) for p, r in zip(plans, roots, strict=True)))

    results = asyncio.run(both())
    for result in results:
        assert result["passed"] is True, json.dumps(result, indent=1)[-2000:]
    records = [r["containers"] for r in results]
    assert records[0]["compose_project"] != records[1]["compose_project"]
    assert records[0]["network"] != records[1]["network"] and all(r["network"].endswith("_default") for r in records)
    assert all(any(e["step"] == "compose down" and e["exit_code"] == 0 for e in r["events"]) for r in records)
    assert _clean(workspace)


def test_stale_resources_of_a_crashed_run_are_removed_by_label_and_project_name(tmp_path):
    workspace, run = "wsstale0001", "deadbeef-0000"
    names = c.environment_names(workspace, run, "ST-020")
    label_args = [x for k, v in c.labels(workspace, run, "ST-020").items() for x in ("--label", f"{k}={v}")]
    subprocess.run(["docker", "network", "create", *label_args, names["network"]], check=True, capture_output=True)
    subprocess.run(["docker", "volume", "create", *label_args, names["id"] + "-vol"], check=True, capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", names["container"], *label_args, IMAGE, "sleep", "300"], check=True, capture_output=True)
    root = tmp_path / "compose"
    root.mkdir()
    (root / "compose.test.yml").write_text("services:\n  idle:\n    image: python:3.13-slim\n    command: [\"sleep\", \"300\"]\n")
    subprocess.run(["docker", "compose", "-p", names["project"], "-f", str(root / "compose.test.yml"), "up", "-d"], check=True, capture_output=True)
    other = c.environment_names(workspace, "cafef00d-1111", "ST-021")
    subprocess.run(["docker", "network", "create", *[x for k, v in c.labels(workspace, "cafef00d-1111", "ST-021").items() for x in ("--label", f"{k}={v}")],
                    other["network"]], check=True, capture_output=True)
    try:
        assert len(_leftovers(workspace)["compose"]) == 1
        removed = asyncio.run(c.cleanup_stale(workspace, run_id=run))
        assert removed == {"containers": 1, "networks": 1, "volumes": 1, "compose_projects": 1}
        left = _leftovers(workspace)
        assert left["containers"] == [] and left["volumes"] == [] and left["compose"] == [] and len(left["networks"]) == 1
    finally:
        asyncio.run(c.cleanup_stale(workspace))
    assert _clean(workspace)


def test_the_executor_runs_story_tests_in_a_container_and_records_the_environment(tmp_path):
    from dridh.config.dridh_yaml import ExecutionConfig
    from dridh.executor.controller import ExecutionController
    from dridh.executor.story_runner import StoryRunner
    from dridh.state.store import StateStore
    from tests.integration import test_executor_controller as h

    info, feature = h.env.__wrapped__(tmp_path)
    config = ExecutionConfig(test_timeout_seconds=180, containers={"mode": "docker", "setup": []},
                             test_command=["python3", "-m", "unittest", "discover", "-s", "tests"])
    planner = h.FakePlanner([h._story("ST-001")])
    controller = ExecutionController(info, planner=planner, story_runner=StoryRunner(info, h.ScriptedAgent(h._good), config), settings=config)
    run = h._run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    assert planner.stories["ST-001"]["status"] == "IN_REVIEW"
    with StateStore(info.state_db, info.workspace_id) as store:
        connection = store.read()
        attempt = connection.execute("SELECT evidence_json FROM story_attempt WHERE run_id=? AND story_id='ST-001'", (run["id"],)).fetchone()
        rows = [dict(r) for r in connection.execute("SELECT * FROM docker_test_environment WHERE workspace_id=?", (info.workspace_id,))]
    tests = json.loads(attempt["evidence_json"])["tests"]
    assert tests["passed"] is True and tests["containers"]["mode"] == "docker" and tests["containers"]["status"] == "PASSED"
    assert len(rows) == 1 and rows[0]["status"] == "CLEANED" and rows[0]["story_id"] == "ST-001" and rows[0]["image_digest"]
    assert json.loads(rows[0]["evidence_json"])["result"] == "PASSED"
    assert _clean(info.workspace_id)
