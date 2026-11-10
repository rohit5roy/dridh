"""Read-only Coordinator queries: context, state queries and document validation."""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from dridh.common.errors import DridhError
from dridh.coordinator import queries
from dridh.coordinator.lifecycle import FeatureLifecycle
from tests.e2e.planner_test_helpers import seed_approved_baseline


@pytest.fixture()
def info(tmp_path):
    workspace, _ = seed_approved_baseline(tmp_path / "project")
    return workspace


def test_context_without_a_feature_says_what_to_do(info):
    context = queries.get_context(info, None)
    assert context["features"] == [] and "coordinator.create_feature" in context["message"]


def test_context_for_a_feature_bundles_status_and_next_actions(info):
    FeatureLifecycle(info).create_feature("Order Intake", actor="t")
    context = queries.get_context(info, None)  # one feature: no slug needed
    assert context["status"]["feature"]["slug"] == "order-intake" and context["status"]["next_actions"]
    assert context["execution_runs"] == [] and context["workspace"]["mode"] in ("new", "existing")


def test_queries_are_filtered_bounded_and_read_only(info):
    FeatureLifecycle(info).create_feature("Order Intake", actor="t")
    assert queries.query(info, "features")["rows"][0]["slug"] == "order-intake"
    assert queries.query(info, "features", status="BLOCKED")["count"] == 0
    for kind in ("stories", "runs", "lots", "gates"):
        assert queries.query(info, kind)["kind"] == kind
    with pytest.raises(DridhError):
        queries.query(info, "documents; DROP TABLE feature")


def test_documents_are_validated_inside_the_project_only(info, tmp_path):
    good = info.project_root / "001-USE-CASES.md"
    result = queries.validate_document(info, "use-cases", good.name)
    assert result["ok"] is True and result["errors"] == 0
    bad = info.project_root / "broken.md"
    bad.write_text("# not a use case document\n")
    assert queries.validate_document(info, "use-cases", "broken.md")["ok"] is False
    outside = tmp_path / "secret.md"
    outside.write_text("x")
    for path in (str(outside), "../secret.md", "missing.md"):
        with pytest.raises(DridhError):
            queries.validate_document(info, "use-cases", path)
    with pytest.raises(DridhError, match="kind must be one of"):
        queries.validate_document(info, "shell", good.name)


def test_the_bridge_serves_them_and_still_has_no_approval_operations(info):
    import uuid

    from tests.integration.test_cli import run_module

    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": "coordinator.validate",
               "workspace_root": str(info.project_root), "payload": {"kind": "use-cases", "path": "001-USE-CASES.md"},
               "caller": {"source": "plugin", "agent": "dridh/coordinator", "session_id": "ses_coordinator_0001"}}
    reply = json.loads(run_module("bridge", stdin=json.dumps(request)).stdout)
    assert reply["ok"] is True and reply["data"]["ok"] is True, reply
    from dridh.common.protocol import ALLOWED_OPERATIONS

    assert not [op for op in ALLOWED_OPERATIONS if any(w in op for w in ("approve", "release", "complete", "deliver"))]


def test_every_bridge_request_is_audited_without_storing_its_payload(info):
    import sqlite3
    import uuid

    from tests.integration.test_cli import run_module

    secret = "sk" + "-" + "canary" + "-value-1234567890"  # built at runtime: a fake key that no scanner should see in source

    def call(operation, payload, agent="dridh/coordinator"):
        request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": operation, "workspace_root": str(info.project_root),
                   "payload": payload, "caller": {"source": "plugin", "agent": agent, "session_id": "ses_audit_0001"}}
        return json.loads(run_module("bridge", stdin=json.dumps(request)).stdout)

    assert call("coordinator.query", {"kind": "features"})["ok"] is True
    assert call("coordinator.validate", {"kind": "shell", "path": secret})["ok"] is False
    assert call("coordinator.status", {}, agent="dridh/planner")["ok"] is False  # rejected callers are recorded too
    with closing(sqlite3.connect(info.state_db)) as connection:
        rows = connection.execute("SELECT operation,status,error_code,caller_agent,payload_sha256,payload_keys FROM tool_invocation ORDER BY created_at, rowid").fetchall()
        assert [(r[0], r[1]) for r in rows] == [("coordinator.query", "OK"), ("coordinator.validate", "FAILED"), ("coordinator.status", "FAILED")]
        assert rows[2][2] == "AUTHORIZATION_DENIED" and rows[2][3] == "dridh/planner"
        assert rows[0][3] == "dridh/coordinator" and len(rows[0][4]) == 64 and json.loads(rows[0][5]) == ["kind"]
        assert secret not in " ".join(str(value) for row in rows for value in row)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE tool_invocation SET status='OK'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM tool_invocation")


def test_review_checklist_queries_show_outstanding_items_and_change_nothing(info):
    from dridh.state.documents import DocumentRepository
    from dridh.state.reviews import ReviewRepository
    from dridh.state.store import StateStore

    with StateStore(info.state_db, info.workspace_id) as store:
        with store.transaction() as conn:
            documents = DocumentRepository(conn, info.workspace_id)
            key = conn.execute("SELECT relative_path FROM document WHERE workspace_id=? ORDER BY created_at LIMIT 1", (info.workspace_id,)).fetchone()[0]
            metadata, revision = documents.document_metadata(key), documents.current_revision(key)
            reviews = ReviewRepository(conn, info.workspace_id)
            review_id = reviews.create_review(document_id=str(metadata["id"]), revision_id=revision.id, reviewer="Ana", actor="t")
            reviews.set_checklist(review_id, "Flows are complete", True, actor="cli:ana")
            reviews.set_checklist(review_id, "Actors are named", False, actor="cli:ana")
        before = store.read().execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]

    listed = queries.list_reviews(info)
    mine = next(r for r in listed["reviews"] if r["review_id"] == review_id)
    assert mine["items"] == 2 and mine["checked"] == 1 and mine["document"] == key
    assert queries.list_reviews(info, status="APPROVED")["reviews"] == [] or all(r["status"] == "APPROVED" for r in queries.list_reviews(info, status="APPROVED")["reviews"])
    with pytest.raises(DridhError):
        queries.list_reviews(info, status="DONE; DROP TABLE review")

    checklist = queries.review_checklist(info, review_id)
    assert checklist["outstanding"] == ["Actors are named"] and checklist["complete"] is False
    assert f"dridh reviews checklist {review_id}" in checklist["human_action"]
    with pytest.raises(DridhError):
        queries.review_checklist(info, "no-such-review")
    with StateStore(info.state_db, info.workspace_id) as store:
        assert store.read().execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == before  # read-only
