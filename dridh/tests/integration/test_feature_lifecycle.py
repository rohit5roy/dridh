"""The feature lifecycle backbone: single steps, evidence from the real stage services, stale-approval reopen.

Ideator and Technical Designer stages use the real services over a real seeded workspace (approved Use Case
and TDD). The Planner/Executor-side stages use a small evidence double, because producing a generated plan
needs a model; the real Planner path is covered by the stub-provider E2E.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.coordinator.lifecycle import FeatureLifecycle, Readiness, StageEvidence, successor
from dridh.state.lifecycle import STAGES, LifecycleRepository
from dridh.state.store import StateStore
from tests.contract.test_orchestration_bridge_contract import CLI, COORDINATOR, _call
from tests.e2e.planner_test_helpers import DESIGNER_SESSION, IDEATOR_SESSION, seed_approved_baseline
from tests.integration.test_cli import run_module

ACTOR = "test:lifecycle"
PLANNER_SESSION = "planner-000000000013"


class PlannerDouble(StageEvidence):
    """Real Ideator/Designer evidence; scripted Planner and story evidence."""

    def __init__(self, workspace: Any) -> None:
        super().__init__(workspace)
        self.planner_status: str | None = None
        self.planner_unlocked = False
        self.stories_done = False

    def discover_downstream(self, feature: dict[str, Any]) -> dict[str, str]:
        found = super().discover_downstream(feature)
        if self.planner_status and feature.get("designer_session_id") and not feature.get("planner_session_id"):
            found["planner_session_id"] = PLANNER_SESSION
        return found

    def can_leave(self, feature: dict[str, Any], stage: str) -> Readiness:
        if stage == "PLANNING":
            ok = self.planner_status == "GENERATED"
            return Readiness(ok, [] if ok else ["The plan is not generated yet."])
        if stage == "PLAN_VALIDATION":
            return Readiness(self.planner_unlocked, [] if self.planner_unlocked else ["Plan gate closed."])
        if stage == "EXECUTION":
            return Readiness(self.stories_done, [] if self.stories_done else ["Stories are not complete."])
        return super().can_leave(feature, stage)

    def violated_invariant(self, feature: dict[str, Any]) -> tuple[str, str] | None:
        if feature["stage"] in ("EXECUTION", "DELIVERY_REVIEW") and not self.planner_unlocked:
            return "PLAN_VALIDATION", "The plan validation is no longer valid."
        features = dict(feature, planner_session_id=None)  # the real check only covers Ideator and Designer here
        return super().violated_invariant(features)


@pytest.fixture()
def workspace(tmp_path: Path):
    info, _designer_session = seed_approved_baseline(tmp_path / "project")
    return info


def _lifecycle(info: Any, double: bool = False) -> FeatureLifecycle:
    return FeatureLifecycle(info, evidence=PlannerDouble(info) if double else None)


def _stages(moves: list[dict[str, Any]]) -> list[str]:
    return [move["to"] for move in moves]


# ------------------------------------------------------------------ schema


def test_fresh_database_has_lifecycle_tables_and_they_are_append_only(workspace):
    lifecycle = _lifecycle(workspace)
    feature = lifecycle.create_feature("Order intake", actor=ACTOR)
    with closing(sqlite3.connect(workspace.state_db)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"feature", "workflow_transition"} <= tables
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE workflow_transition SET reason='x'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM workflow_transition")
        with pytest.raises(sqlite3.DatabaseError, match="retained"):
            connection.execute("DELETE FROM feature WHERE id=?", (feature["id"],))
    assert feature["stage"] == "INITIALISED" and feature["slug"] == "order-intake"


def test_a_v10_database_migrates_to_v11_keeping_its_data(workspace):
    from dridh.state.migration import migrate_to_current, schema_version

    connection = sqlite3.connect(workspace.state_db)
    for statement in ("DROP TABLE ledger_import", "DROP TABLE ledger_entry", "DROP TABLE tool_invocation", "DROP TABLE story_attempt", "DROP TABLE execution_lot_review", "DROP TABLE execution_lot_story", "DROP TABLE execution_lot",
                      "DROP TABLE execution_run", "DROP TABLE workflow_transition", "DROP TABLE feature"):
        connection.execute(statement)
    connection.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    connection.commit()
    connection.close()
    result = migrate_to_current(workspace.state_db)
    assert result["to_version"] == 15 and [step["to_version"] for step in result["steps"]] == [11, 12, 13, 14, 15]
    assert result["steps"][0]["backup_path"] and Path(result["steps"][0]["backup_path"]).exists()
    assert schema_version(workspace.state_db) == 15
    assert _lifecycle(workspace).create_feature("After migration", actor=ACTOR)["stage"] == "INITIALISED"


# ---------------------------------------------------------------- creation


def test_create_defaults_to_the_workspace_mode_rejects_duplicates_and_records_history(workspace):
    lifecycle = _lifecycle(workspace)
    feature = lifecycle.create_feature("Order Intake!", actor=ACTOR)
    assert feature["slug"] == "order-intake" and feature["mode"] == workspace.mode == "new"
    with pytest.raises(DridhError) as exc_info:
        lifecycle.create_feature("order intake", actor=ACTOR)
    assert exc_info.value.code is ErrorCode.CONFIGURATION_CONFLICT
    with pytest.raises(DridhError):
        lifecycle.create_feature("!!!", actor=ACTOR)
    with pytest.raises(DridhError):
        lifecycle.create_feature("x", actor=ACTOR, mode="maybe")
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        history = LifecycleRepository(store.read(), workspace.workspace_id).transitions(feature["id"])
    assert [(item["kind"], item["to_stage"]) for item in history] == [("CREATE", "INITIALISED")]


def test_successor_skips_indexing_only_for_new_codebases():
    assert successor("new", "INITIALISED") == "IDEATION"
    assert successor("existing", "INITIALISED") == "CODEBASE_INDEXING"
    assert successor("existing", "CODEBASE_INDEXING") == "IDEATION"
    assert successor("new", "COMPLETED") is None and STAGES[0] == "INITIALISED" and STAGES[-1] == "COMPLETED"


# ----------------------------------------------- real services: new codebase


def test_sync_walks_a_new_feature_through_the_approved_stages_using_real_evidence(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor=ACTOR)
    result = lifecycle.sync("order-intake", actor=ACTOR)
    assert _stages(result["moves"]) == ["IDEATION", "USE_CASE_REVIEW", "TECHNICAL_DISCOVERY", "TDD_REVIEW", "PLANNING"]
    status = result["status"]
    assert status["feature"]["stage"] == "PLANNING" and status["sessions"]["designer_session_id"] == DESIGNER_SESSION  # discovered
    assert status["ready_to_advance"] is False and "no session bound" in status["blockers"][0]
    assert "planner.start" in status["next_actions"]
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        history = LifecycleRepository(store.read(), workspace.workspace_id).transitions(status["feature"]["id"])
    assert [item["to_stage"] for item in history][-1] == "PLANNING" and history[3]["to_stage"] == "TECHNICAL_DISCOVERY" and history[3]["evidence"]["gate"] == "unlocked"
    assert lifecycle.sync("order-intake", actor=ACTOR)["moves"] == []  # idempotent


def test_a_stage_cannot_be_skipped_or_left_without_its_evidence(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.advance("order-intake", actor=ACTOR)  # INITIALISED -> IDEATION needs nothing but creation
    with pytest.raises(DridhError) as exc_info:
        lifecycle.advance("order-intake", actor=ACTOR)  # no ideator session bound
    assert exc_info.value.code is ErrorCode.INVALID_TRANSITION and "Cannot leave IDEATION" in exc_info.value.message
    assert lifecycle.status("order-intake")["feature"]["stage"] == "IDEATION"


def test_changing_an_approved_document_reopens_the_review_and_recovers_when_restored(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor=ACTOR)
    assert lifecycle.sync("order-intake", actor=ACTOR)["status"]["feature"]["stage"] == "PLANNING"

    document = workspace.project_root / "001-USE-CASES.md"
    original = document.read_text(encoding="utf-8")
    document.write_text(original + "\nAn unreviewed edit after approval.\n", encoding="utf-8")
    result = lifecycle.sync("order-intake", actor=ACTOR)
    assert result["moves"][0]["kind"] == "REOPEN" and result["moves"][0]["to"] == "USE_CASE_REVIEW"
    assert "no longer valid" in result["moves"][0]["reason"]
    status = result["status"]
    assert status["feature"]["stage"] == "USE_CASE_REVIEW" and status["ready_to_advance"] is False
    with pytest.raises(DridhError):
        lifecycle.advance("order-intake", actor=ACTOR)

    document.write_text(original, encoding="utf-8")
    assert lifecycle.sync("order-intake", actor=ACTOR)["status"]["feature"]["stage"] == "PLANNING"


# ------------------------------------------------------ existing codebase


def test_an_existing_codebase_waits_for_a_successful_code_index(tmp_path):
    info, _ = seed_approved_baseline(tmp_path / "project")
    lifecycle = _lifecycle(info)
    lifecycle.create_feature("Order intake", actor=ACTOR, mode="existing")
    result = lifecycle.sync("order-intake", actor=ACTOR)
    assert _stages(result["moves"]) == ["CODEBASE_INDEXING"]
    assert "Code Reacher index" in result["status"]["blockers"][0]

    connection = sqlite3.connect(info.state_db)
    connection.execute(
        "INSERT INTO code_index_run(id,workspace_id,repository_path,source_sha,status,started_at) VALUES(?,?,?,?,?,?)",
        (str(uuid.uuid4()), info.workspace_id, ".", "a" * 40, "FAILED", "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()
    assert lifecycle.sync("order-intake", actor=ACTOR)["moves"] == []  # a failed index is not evidence
    connection.execute(
        "INSERT INTO code_index_run(id,workspace_id,repository_path,source_sha,status,started_at) VALUES(?,?,?,?,?,?)",
        (str(uuid.uuid4()), info.workspace_id, ".", "b" * 40, "SUCCEEDED", "2026-01-02T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    assert _stages(lifecycle.sync("order-intake", actor=ACTOR)["moves"]) == ["IDEATION"]


# --------------------------------------------- planner-side stages (double)


def test_the_full_path_to_completion_is_human_gated_at_the_end(workspace):
    lifecycle = _lifecycle(workspace, double=True)
    double: PlannerDouble = lifecycle.evidence  # type: ignore[assignment]
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor=ACTOR)
    assert lifecycle.sync("order-intake", actor=ACTOR)["status"]["feature"]["stage"] == "PLANNING"

    double.planner_status, double.planner_unlocked, double.stories_done = "GENERATED", True, True
    result = lifecycle.sync("order-intake", actor=ACTOR)
    assert _stages(result["moves"]) == ["PLAN_VALIDATION", "EXECUTION", "DELIVERY_REVIEW"]
    assert result["status"]["feature"]["stage"] == "DELIVERY_REVIEW"

    with pytest.raises(DridhError) as exc_info:
        lifecycle.advance("order-intake", actor=ACTOR)
    assert exc_info.value.code is ErrorCode.APPROVAL_REQUIRED
    with pytest.raises(DridhError):
        lifecycle.complete("order-intake", reviewer="Rohit", human_confirmed=False)
    with pytest.raises(DridhError):
        lifecycle.complete("order-intake", reviewer="  ", human_confirmed=True)
    done = lifecycle.complete("order-intake", reviewer="Rohit", human_confirmed=True)["feature"]
    assert done["stage"] == "COMPLETED"
    with pytest.raises(DridhError):
        lifecycle.advance("order-intake", actor=ACTOR)


def test_complete_is_refused_before_delivery_review(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    with pytest.raises(DridhError, match="DELIVERY_REVIEW"):
        lifecycle.complete("order-intake", reviewer="Rohit", human_confirmed=True)


# ------------------------------------------------------ safety properties


def test_block_prevents_movement_and_unblock_restores_it(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    blocked = lifecycle.block("order-intake", reason="Waiting for the product owner.", actor=ACTOR)
    assert blocked["status"] == "BLOCKED"
    with pytest.raises(DridhError, match="blocked"):
        lifecycle.advance("order-intake", actor=ACTOR)
    assert lifecycle.sync("order-intake", actor=ACTOR)["moves"] == []
    assert "blocked" in lifecycle.status("order-intake")["blockers"][0].lower()
    assert lifecycle.unblock("order-intake", reason="Answered.", actor=ACTOR)["status"] == "ACTIVE"
    assert lifecycle.advance("order-intake", actor=ACTOR)["feature"]["stage"] == "IDEATION"
    with pytest.raises(DridhError):
        lifecycle.unblock("order-intake", reason="again", actor=ACTOR)
    with pytest.raises(DridhError):
        lifecycle.block("order-intake", reason=" ", actor=ACTOR)


def test_a_stale_writer_cannot_overwrite_newer_state(workspace):
    lifecycle = _lifecycle(workspace)
    created = lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.advance("order-intake", actor=ACTOR)  # the feature is now at a newer revision than `created`
    with StateStore(workspace.state_db, workspace.workspace_id) as store, store.transaction() as connection:
        with pytest.raises(DridhError) as exc_info:
            LifecycleRepository(connection, workspace.workspace_id).record_transition(
                created, to_stage="IDEATION", kind="ADVANCE", reason="stale", evidence={}, actor=ACTOR
            )
    assert exc_info.value.code is ErrorCode.INVALID_TRANSITION and "changed since it was read" in exc_info.value.message
    assert lifecycle.status("order-intake")["feature"]["stage"] == "IDEATION"


def test_advance_with_the_same_idempotency_key_is_replayed_not_repeated(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    first = lifecycle.advance("order-intake", actor=ACTOR, idempotency_key="req-1")
    again = lifecycle.advance("order-intake", actor=ACTOR, idempotency_key="req-1")
    assert first["replayed"] is False and again["replayed"] is True
    assert lifecycle.status("order-intake")["feature"]["stage"] == "IDEATION"
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        count = len(LifecycleRepository(store.read(), workspace.workspace_id).transitions(first["feature"]["id"]))
    assert count == 2  # CREATE + one ADVANCE


def test_reopen_only_goes_backwards_and_needs_a_reason(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor=ACTOR)
    lifecycle.sync("order-intake", actor=ACTOR)
    for target in ("PLANNING", "EXECUTION", "NOPE", "CODEBASE_INDEXING"):
        with pytest.raises(DridhError):
            lifecycle.reopen("order-intake", to_stage=target, reason="why", actor=ACTOR)
    with pytest.raises(DridhError):
        lifecycle.reopen("order-intake", to_stage="IDEATION", reason=" ", actor=ACTOR)
    assert lifecycle.reopen("order-intake", to_stage="IDEATION", reason="Scope changed.", actor=ACTOR)["stage"] == "IDEATION"


def test_a_session_cannot_be_rebound_to_a_different_one(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor=ACTOR)
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor=ACTOR)  # same id: no-op
    with pytest.raises(DridhError) as exc_info:
        lifecycle.bind_session("order-intake", "ideator_session_id", "inquiry-ffffffffffff", actor=ACTOR)
    assert exc_info.value.code is ErrorCode.CONFIGURATION_CONFLICT
    with pytest.raises(DridhError):
        lifecycle.bind_session("order-intake", "evil_column", "x", actor=ACTOR)


def test_transitions_are_written_to_the_hash_chained_audit_log(workspace):
    lifecycle = _lifecycle(workspace)
    lifecycle.create_feature("Order intake", actor=ACTOR)
    lifecycle.advance("order-intake", actor=ACTOR)
    with closing(sqlite3.connect(workspace.state_db)) as connection:
        actions = [row[0] for row in connection.execute("SELECT action FROM audit_event ORDER BY seq")]
    assert "feature.create" in actions and "feature.advance" in actions


# ----------------------------------------------------------------- bridge


def test_bridge_operations_drive_the_lifecycle_for_the_coordinator_only(workspace):
    root = workspace.project_root
    created = _call("coordinator.create_feature", root, {"name": "Order intake"}, caller=COORDINATOR)
    assert created["ok"] and created["data"]["feature"]["stage"] == "INITIALISED"
    status = _call("coordinator.status", root, caller=COORDINATOR)  # the only feature is implied
    assert status["ok"] and status["data"]["feature"]["slug"] == "order-intake" and status["data"]["next_stage"] == "IDEATION"
    advanced = _call("coordinator.transition", root, {"feature": "order-intake", "idempotency_key": "k1"}, caller=COORDINATOR)
    assert advanced["ok"] and advanced["data"]["feature"]["stage"] == "IDEATION"
    refused = _call("coordinator.transition", root, {"feature": "order-intake"}, caller=COORDINATOR)
    assert refused["ok"] is False and refused["error"]["code"] == "INVALID_TRANSITION"
    synced = _call("coordinator.sync", root, {"feature": "order-intake"}, caller=COORDINATOR)
    assert synced["ok"] and synced["data"]["moves"] == []

    assert _call("coordinator.status", root, caller=CLI)["ok"] is False
    assert _call("coordinator.status", root, caller={"source": "plugin", "agent": "dridh/ideator", "session_id": "ses_x0000001"})["ok"] is False
    for operation in ("coordinator.complete", "coordinator.approve", "feature.complete"):
        assert _call(operation, root, {"feature": "order-intake"}, caller=COORDINATOR)["ok"] is False


def test_bridge_validates_its_inputs(workspace):
    root = workspace.project_root
    assert _call("coordinator.create_feature", root, {"name": ""}, caller=COORDINATOR)["ok"] is False
    assert _call("coordinator.status", root, {"feature": 7}, caller=COORDINATOR)["ok"] is False
    assert _call("coordinator.status", root, {"feature": "missing"}, caller=COORDINATOR)["ok"] is False
    _call("coordinator.create_feature", root, {"name": "one"}, caller=COORDINATOR)
    _call("coordinator.create_feature", root, {"name": "two"}, caller=COORDINATOR)
    ambiguous = _call("coordinator.status", root, caller=COORDINATOR)  # two features and none named
    assert ambiguous["ok"] is False
    assert _call("coordinator.transition", root, {"feature": "one", "idempotency_key": ""}, caller=COORDINATOR)["ok"] is False


# -------------------------------------------------------------------- CLI


def test_cli_commands_and_the_human_only_completion(workspace):
    root = str(workspace.project_root)
    assert run_module("feature", "create", "Order intake", "--project", root).returncode == 0
    listed = json.loads(run_module("feature", "list", "--project", root).stdout)["features"]
    assert [item["slug"] for item in listed] == ["order-intake"]
    assert run_module("feature", "bind", "--kind", "ideator", "--session", IDEATOR_SESSION, "--project", root).returncode == 0
    synced = run_module("feature", "sync", "--project", root)
    assert synced.returncode == 0, synced.stderr
    assert json.loads(synced.stdout)["status"]["feature"]["stage"] == "PLANNING"
    status = json.loads(run_module("feature", "status", "--project", root).stdout)
    assert status["next_stage"] == "PLAN_VALIDATION" and status["ready_to_advance"] is False

    bad_kind = run_module("feature", "bind", "--kind", "executor", "--session", "x", "--project", root)
    assert bad_kind.returncode == 2
    refused = run_module("feature", "complete", "--reviewer", "Rohit", "--project", root, stdin="COMPLETE order-intake\n")
    assert refused.returncode != 0 and "interactive terminal" in refused.stderr  # scripts and agents cannot complete
    reopened = run_module("feature", "reopen", "--to", "ideation", "--reason", "Scope changed.", "--project", root)
    assert reopened.returncode == 0 and json.loads(reopened.stdout)["feature"]["stage"] == "IDEATION"
