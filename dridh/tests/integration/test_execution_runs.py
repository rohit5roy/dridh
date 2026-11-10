"""Execution run, lot and attempt persistence (schema v12)."""

from __future__ import annotations

import sqlite3

import pytest

from dridh.common.errors import DridhError
from dridh.config.dridh_yaml import ExecutionConfig
from dridh.state.lifecycle import LifecycleRepository
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline


@pytest.fixture
def parts(tmp_path):
    workspace, _session = seed_approved_baseline(tmp_path / "project")
    yield from _parts(workspace)


def _parts(workspace):
    store = StateStore(workspace.state_db, workspace.workspace_id).__enter__()
    try:
        with store.transaction() as connection:
            feature = LifecycleRepository(connection, workspace.workspace_id).create(slug="f", name="F", mode="new", actor="t")
        yield store, workspace, feature
    finally:
        store.__exit__(None, None, None)


def _run(store, workspace, feature, **kwargs):
    with store.transaction() as connection:
        return RunRepository(connection, workspace.workspace_id).create_run(
            feature_id=feature["id"], planner_session_id="planner-aaaaaaaaaaaa", plan_digest="d" * 64,
            max_parallel=kwargs.get("max_parallel", 2), max_repairs=3, confirmed_by="human", actor="human:h")


def test_one_active_run_per_feature_and_status_changes_are_revision_checked(parts):
    store, workspace, feature = parts
    run = _run(store, workspace, feature)
    with pytest.raises(DridhError, match="already has an active"):
        _run(store, workspace, feature)
    with store.transaction() as connection:
        repo = RunRepository(connection, workspace.workspace_id)
        paused = repo.set_run_status(run, "PAUSED", reason="pathfinder review", actor="t")
        assert paused["status"] == "PAUSED" and paused["pause_reason"] == "pathfinder review"
        with pytest.raises(DridhError, match="changed since"):
            repo.set_run_status(run, "RUNNING", reason=None, actor="t")
        done = repo.set_run_status(paused, "STOPPED", reason=None, actor="t")
        with pytest.raises(DridhError, match="already STOPPED"):
            repo.set_run_status(done, "RUNNING", reason=None, actor="t")
    assert _run(store, workspace, feature)["status"] == "RUNNING"


def test_lots_are_decided_as_a_whole_with_an_immutable_history(parts):
    store, workspace, feature = parts
    run = _run(store, workspace, feature)
    with store.transaction() as connection:
        repo = RunRepository(connection, workspace.workspace_id)
        lot = repo.open_lot(run_id=run["id"], kind="PATHFINDER", story_ids=["S-1"], threshold_percent=None, actor="t")
        assert lot["status"] == "AWAITING_REVIEW" and lot["story_ids"] == ["S-1"]
        rework = repo.review_lot(lot["id"], verdict="REWORK_REQUIRED", reviewer="h", notes="fix", actor="human:h")
        assert rework["status"] == "REWORK_REQUIRED"
        with pytest.raises(DridhError, match="not awaiting review"):
            repo.review_lot(lot["id"], verdict="APPROVED", reviewer="h", notes="", actor="human:h")
        repo.reopen_lot(lot["id"], actor="t")
        approved = repo.review_lot(lot["id"], verdict="APPROVED", reviewer="h", notes="", actor="human:h")
        assert [r["verdict"] for r in approved["reviews"]] == ["REWORK_REQUIRED", "APPROVED"]
        second = repo.open_lot(run_id=run["id"], kind="THRESHOLD", story_ids=["S-2", "S-3"], threshold_percent=25, actor="t")
        assert second["ordinal"] == 2
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with store.transaction() as connection:
            connection.execute("UPDATE execution_lot_review SET notes='x'")


def test_attempts_are_numbered_per_story_and_finish_once(parts):
    store, workspace, feature = parts
    run = _run(store, workspace, feature)
    with store.transaction() as connection:
        repo = RunRepository(connection, workspace.workspace_id)
        first = repo.start_attempt(run_id=run["id"], story_id="S-1", kind="IMPLEMENT", actor="t")
        done = repo.finish_attempt(first["id"], status="FAILED", commit_sha=None, evidence={"tests": "red"}, actor="t")
        assert done["evidence"] == {"tests": "red"}
        with pytest.raises(DridhError, match="not running"):
            repo.finish_attempt(first["id"], status="PASSED", commit_sha="abc", evidence={}, actor="t")
        second = repo.start_attempt(run_id=run["id"], story_id="S-1", kind="REPAIR", actor="t")
        assert (first["attempt_no"], second["attempt_no"]) == (1, 2)
        assert len(repo.attempts(run["id"], "S-1")) == 2


def test_execution_settings_are_bounded():
    assert ExecutionConfig().max_parallel_stories == 2 and ExecutionConfig().checkpoint_percentages == [25, 50, 100]
    for bad in ({"max_parallel_stories": 6}, {"max_parallel_stories": 0}, {"max_repair_attempts": 4},
                {"checkpoint_percentages": [50, 25, 100]}, {"checkpoint_percentages": [25, 50]}):
        with pytest.raises(ValueError):
            ExecutionConfig(**bad)


def test_story_status_may_change_but_story_definitions_stay_immutable(tmp_path):
    from dridh.planner.service import PlannerService  # noqa: F401 - the real writer uses the same table
    from tests.e2e.planner_test_helpers import seed_approved_baseline

    info, _ = seed_approved_baseline(tmp_path / "project")
    with StateStore(info.state_db, info.workspace_id) as store:
        triggers = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='story'")}
    assert "story_immutable_update" not in triggers and "story_definition_immutable_update" in triggers
