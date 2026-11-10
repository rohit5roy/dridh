"""Deterministic story-to-code mapping from story file ownership and the files each story's passing attempt changed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from dridh.code_reacher.indexer import CodeIndexer
from dridh.code_reacher.mapping import StoryCodeMapper, owns, refresh_mapping, target_type
from dridh.common.errors import DridhError
from dridh.state.lifecycle import LifecycleRepository
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


def _story(story_id: str, owned: list[str], use_cases: list[str], component: str = "CMP-01") -> dict[str, Any]:
    return {"story_id": story_id, "component_id": component, "use_case_ids": use_cases, "file_ownership": owned}


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _attempt(info, run_id: str, story_id: str, *, changed: list[str] | None, commit: str | None) -> None:
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        repo = RunRepository(connection, info.workspace_id)
        attempt = repo.start_attempt(run_id=run_id, story_id=story_id, kind="IMPLEMENT", actor="t")
        evidence = {"changed_files": changed} if changed is not None else {}
        repo.finish_attempt(attempt["id"], status="PASSED", commit_sha=commit, evidence=evidence, actor="t")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    info, _ = seed_approved_baseline(tmp_path / "project")
    root = info.project_root
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".opencode/\n")
    _write(root, "src/orders/cancel.py", "def cancel_order(order_id):\n    return order_id\n")
    _write(root, "src/orders_archive/old.py", "def archived():\n    return 1\n")
    _write(root, "src/billing/refund.py", "def refund(amount):\n    return amount\n")
    _write(root, "src/reports/daily.py", "def daily_report():\n    return []\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _write(root, "src/shipping/ship.py", "class Shipper:\n    def ship(self):\n        return True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "ST-1.3")
    shipping_commit = _git(root, "rev-parse", "HEAD")

    stories = [
        _story("ST-1.1", ["src/orders"], ["UC-01"]),            # ownership only (directory prefix)
        _story("ST-1.2", ["lib/**"], ["UC-01"], "CMP-02"),        # nothing owned: mapped by its changed files only
        _story("ST-1.3", ["src/shipping/*.py"], []),              # glob ownership and commit-file fallback
        _story("ST-1.4", ["docs/"], []),                          # no code at all
    ]
    monkeypatch.setattr(StoryCodeMapper, "_stories", lambda self: (stories, []))
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        feature = LifecycleRepository(connection, info.workspace_id).create(slug="f", name="F", mode="new", actor="t")
        run = RunRepository(connection, info.workspace_id).create_run(
            feature_id=feature["id"], planner_session_id="planner-aaaaaaaaaaaa", plan_digest="a" * 64,
            max_parallel=2, max_repairs=3, confirmed_by="h", actor="human:h")
    _attempt(info, run["id"], "ST-1.2", changed=["./src/billing/refund.py", "tests/test_refund.py"], commit=None)
    _attempt(info, run["id"], "ST-1.3", changed=None, commit=shipping_commit)
    CodeIndexer(info).index(embed=False)
    return info


def test_ownership_matching_is_by_prefix_or_glob_never_by_partial_name():
    assert owns("src/orders", "src/orders/cancel.py") and owns("src/orders/", "src/orders/cancel.py")
    assert not owns("src/orders", "src/orders_archive/old.py")
    assert owns("src/shipping/*.py", "src/shipping/ship.py") and not owns("src/shipping/*.py", "src/shipping/deep/x.ts")
    assert owns("src/**", "src/a/b/c.py") and owns("./src/x.py", "src/x.py") and not owns("", "src/x.py")
    assert [target_type(t) for t in ("UC-01", "CMP-02", "SF-01.1", "ST-1.1")] == ["USE_CASE", "COMPONENT", "FLOW", "STORY"]


def test_rebuild_maps_stories_components_and_use_cases_with_evidence(project):
    mapper = StoryCodeMapper(project)
    result = mapper.rebuild()
    assert result["stories_without_code"] == ["ST-1.4"] and result["mappings"] > 0

    owned = mapper.code_for("ST-1.1")
    assert list(owned["files"]) == ["src/orders/cancel.py"]  # src/orders_archive is not inside src/orders
    assert owned["files"]["src/orders/cancel.py"][0]["mapping"] == "FILE_OWNERSHIP"
    assert owned["files"]["src/orders/cancel.py"][0]["evidence"] == {"owned": "src/orders", "path": "src/orders/cancel.py"}

    changed = mapper.code_for("ST-1.2")
    assert list(changed["files"]) == ["src/billing/refund.py"]
    assert {m["mapping"] for m in changed["files"]["src/billing/refund.py"]} == {"STORY_CHANGE"}

    shipped = mapper.code_for("ST-1.3")
    mappings = {(m["entity"], m["mapping"]) for m in shipped["files"]["src/shipping/ship.py"]}
    assert ("Shipper", "FILE_OWNERSHIP") in {(e.split(".")[-1], m) for e, m in mappings}
    assert any(m == "STORY_CHANGE" for _, m in mappings)  # from the commit, since the evidence had no file list

    use_case = mapper.code_for("UC-01")
    assert set(use_case["files"]) == {"src/orders/cancel.py", "src/billing/refund.py"}
    assert all(m["mapping"] == "VIA_STORY" for items in use_case["files"].values() for m in items)
    assert mapper.code_for("CMP-02")["files"].keys() == {"src/billing/refund.py"}
    assert mapper.code_for("UC-99")["files"] == {}

    targets = mapper.targets_for("src/orders/cancel.py")["targets"]
    assert targets["STORY"] == {"ST-1.1": ["FILE_OWNERSHIP"]} and targets["USE_CASE"] == {"UC-01": ["VIA_STORY"]}
    assert mapper.targets_for("src/reports/daily.py")["targets"] == {}
    with pytest.raises(DridhError):
        mapper.targets_for("../etc/passwd")


def test_rebuild_is_a_projection_of_the_current_index(project):
    mapper = StoryCodeMapper(project)
    first = mapper.rebuild()
    assert mapper.rebuild()["mappings"] == first["mappings"]  # idempotent
    _write(project.project_root, "src/orders/cancel.py", "def cancel_order(order_id, reason):\n    return order_id\n\n\ndef undo():\n    return 0\n")
    CodeIndexer(project).index(embed=False)  # recreates the entities of the changed file
    assert mapper.code_for("ST-1.1")["entities"] == 0  # stale until rebuilt (the cascade removed the old links)
    refreshed = refresh_mapping(project)
    assert refreshed["rebuilt"] is True
    names = {m["entity"].split(".")[-1] for m in mapper.code_for("ST-1.1")["files"]["src/orders/cancel.py"]}
    assert {"cancel_order", "undo"} <= names


def test_refresh_reports_instead_of_failing_without_an_index(tmp_path):
    info, _ = seed_approved_baseline(tmp_path / "empty")
    result = refresh_mapping(info)
    assert result["rebuilt"] is False and "index" in result["error"].lower()
    assert StoryCodeMapper(info).code_for("ST-1.1")["note"].startswith("No mapping")


def test_the_real_planner_session_supplies_stories_and_ownership_before_approval(tmp_path):
    from tests.integration.test_planner_repair import _started

    service, session_id = _started(tmp_path)
    with pytest.raises(DridhError, match="not been generated"):
        service.planned_stories(session_id)
    payload = service._load(session_id)
    keys = payload["indexer"]["order"]
    payload["generated"] = {"story_ids": {key: f"ST-1.{n}" for n, key in enumerate(keys, start=1)}}
    service._save(payload)
    planned = service.planned_stories(session_id)  # no approval needed: the mapping is read-only
    assert [s["story_id"] for s in planned] == [f"ST-1.{n}" for n in range(1, len(keys) + 1)]
    assert all("file_ownership" in s and s["status"] == "NOT_STARTED" for s in planned)
    with pytest.raises(DridhError):
        service.execution_stories(session_id)  # execution still needs the approved plan

    info = service.workspace
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        feature = LifecycleRepository(connection, info.workspace_id).create(slug="f", name="F", mode="new", actor="t")
        RunRepository(connection, info.workspace_id).create_run(feature_id=feature["id"], planner_session_id=session_id, plan_digest="a" * 64,
                                                                max_parallel=1, max_repairs=1, confirmed_by="h", actor="human:h")
    stories, notes = StoryCodeMapper(info)._stories()
    assert [s["story_id"] for s in stories] == [s["story_id"] for s in planned] and notes == []
