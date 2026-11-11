"""Bridge operations behind the plugin's OpenCode hooks."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.protocol import decode_request, dispatch
from dridh.coordinator.lifecycle import FeatureLifecycle
from dridh.hooks import context_text
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace


def _call(root: Path, operation: str, payload: dict) -> dict:
    raw = json.dumps({"protocol_version": 1, "request_id": str(uuid.uuid4()), "workspace_root": str(root), "operation": operation,
                      "payload": payload, "caller": {"source": "plugin", "agent": payload.get("agent"), "session_id": payload.get("session_id")}})
    return dispatch(decode_request(raw)).data


def _rows(root: Path) -> list[dict]:
    info, _ = init_workspace(root)
    with StateStore(info.state_db, info.workspace_id) as store:
        return [dict(r) for r in store.read().execute("SELECT * FROM tool_invocation ORDER BY created_at, rowid")]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    info, _ = init_workspace(root, mode="new")
    FeatureLifecycle(info).create_feature("Add farewell", actor="test")
    return root


def test_context_for_a_dridh_agent_summarises_feature_stage_and_rules(project: Path) -> None:
    text = _call(project, "hook.context", {"agent": "dridh/coordinator", "session_id": "ses_1"})["text"]
    assert "Feature 'add-farewell'" in text and "Approvals, checklist ticks" in text and len(text) <= 900


def test_context_is_empty_for_ordinary_agents_and_for_projects_without_dridh(project: Path, tmp_path: Path) -> None:
    assert _call(project, "hook.context", {"agent": "build", "session_id": "ses_1"})["text"] == ""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _call(plain, "hook.context", {"agent": "dridh/coordinator", "session_id": "ses_1"})["text"] == ""
    assert not (plain / ".opencode").exists()


def test_tool_call_metadata_is_recorded_without_the_input(project: Path) -> None:
    before = len(_rows(project))
    result = _call(project, "hook.tool_call", {"agent": "dridh/workers/planner-indexer", "session_id": "ses_2", "tool": "read",
                                               "status": "completed", "duration_ms": 42, "input_keys": ["filePath"], "input_sha256": "a" * 64})
    assert result == {"recorded": True}
    rows = _rows(project)
    assert len(rows) == before + 1  # the hook request itself is not audited twice
    row = rows[-1]
    assert row["operation"] == "opencode.tool:read" and row["caller_source"] == "hook"
    assert row["caller_agent"] == "dridh/workers/planner-indexer" and row["caller_session"] == "ses_2" and row["status"] == "OK"
    assert json.loads(row["payload_keys"]) == ["input_keys", "input_sha256"]


def test_ordinary_agents_tool_calls_are_not_recorded(project: Path) -> None:
    before = len(_rows(project))
    assert _call(project, "hook.tool_call", {"agent": "build", "session_id": "s", "tool": "bash", "status": "completed"})["recorded"] is False
    assert len(_rows(project)) == before


def test_session_events_are_recorded_and_failures_marked(project: Path) -> None:
    assert _call(project, "hook.session_event", {"agent": "dridh/coordinator", "session_id": "ses_3", "type": "session.execution.failed"}) == {"recorded": True}
    row = _rows(project)[-1]
    assert row["operation"] == "opencode.event:session.execution.failed" and row["status"] == "FAILED" and row["error_code"] == "SESSION_FAILED"


@pytest.mark.parametrize(("operation", "payload"), [
    ("hook.tool_call", {"agent": "dridh/coordinator", "tool": "read", "status": "maybe"}),
    ("hook.tool_call", {"agent": "dridh/coordinator", "tool": "read; rm -rf /", "status": "completed"}),
    ("hook.tool_call", {"agent": "dridh/coordinator", "tool": "read", "status": "completed", "input_sha256": "not-a-digest"}),
    ("hook.session_event", {"agent": "dridh/coordinator", "type": "file.edited"}),
])
def test_malformed_hook_payloads_are_refused(project: Path, operation: str, payload: dict) -> None:
    with pytest.raises(DridhError) as raised:
        _call(project, operation, payload)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_context_text_without_a_feature_points_to_feature_creation() -> None:
    assert "coordinator.create_feature" in context_text({"feature": None})
