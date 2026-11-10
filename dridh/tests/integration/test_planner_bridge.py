"""Coordinator-only authorization coverage for Phase 13 Planner bridge operations."""

from __future__ import annotations

import json
import uuid

from tests.integration.test_cli import run_module


def test_planner_bridge_rejects_non_coordinator_worker(tmp_path) -> None:
    request = {
        "protocol_version": 1,
        "request_id": str(uuid.uuid4()),
        "workspace_root": str(tmp_path),
        "operation": "planner.status",
        "payload": {"session_id": "planner-0123456789ab"},
        "caller": {"source": "plugin", "agent": "dridh/planner", "session_id": "ses_planner_worker_123"},
    }
    result = run_module("bridge", stdin=json.dumps(request))
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTHORIZATION_DENIED"
