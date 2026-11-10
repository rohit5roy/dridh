from __future__ import annotations

import json
import uuid

from tests.integration.test_cli import run_module


def _wire(workspace, operation="runtime.ping", **overrides) -> str:
    payload = {
        "protocol_version": 1,
        "request_id": str(uuid.uuid4()),
        "workspace_root": str(workspace),
        "operation": operation,
        "payload": {},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _invoke(request: str) -> tuple[dict, str]:
    result = run_module("bridge", stdin=request)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    return json.loads(result.stdout), result.stderr


def test_success_envelope_shape(tmp_path) -> None:
    request_id = str(uuid.uuid4())
    response, _ = _invoke(_wire(tmp_path, request_id=request_id))
    assert set(response) == {"protocol_version", "request_id", "ok", "data"}
    assert response["protocol_version"] == 1
    assert response["request_id"] == request_id
    assert response["ok"] is True
    assert isinstance(response["data"], dict)


def test_failure_envelope_shape(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path, operation="unknown.operation"))
    assert set(response) == {"protocol_version", "request_id", "ok", "error"}
    assert response["protocol_version"] == 1
    assert response["ok"] is False
    assert set(response["error"]) == {"code", "message", "details", "retryable", "recovery"}
    assert isinstance(response["error"]["details"], list)
    assert isinstance(response["error"]["retryable"], bool)
    assert isinstance(response["error"]["recovery"], str)


def test_request_id_is_echoed_on_success_and_failure(tmp_path) -> None:
    request_id = str(uuid.uuid4())
    success, _ = _invoke(_wire(tmp_path, request_id=request_id))
    assert success["request_id"] == request_id
    failure, _ = _invoke(_wire(tmp_path, request_id=request_id, operation="not.allowed"))
    assert failure["request_id"] == request_id


def test_unknown_operation_is_rejected(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path, operation="jev.evaluate"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_invalid_payload_is_rejected(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path, payload=["not", "a", "dict"]))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_invalid_workspace_path_is_rejected(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path / "does-not-exist"))
    assert response["ok"] is False
    assert response["error"]["code"] == "WORKSPACE_NOT_FOUND"


def test_protocol_version_mismatch_is_protocol_error(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path, protocol_version=99))
    assert response["ok"] is False
    assert response["error"]["code"] == "PROTOCOL_ERROR"


def test_stdout_contains_only_protocol_json(tmp_path) -> None:
    result = run_module("bridge", stdin=_wire(tmp_path))
    assert result.returncode == 0
    assert result.stdout.startswith("{")
    assert result.stdout.endswith("\n")
    # stderr may contain JSON diagnostics, but stdout must be one JSON object.
    json.loads(result.stdout)
    assert result.stderr


def test_missing_request_id_is_validation_failure(tmp_path) -> None:
    request = json.loads(_wire(tmp_path))
    request.pop("request_id")
    response, _ = _invoke(json.dumps(request))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"
    assert response["request_id"] == "unavailable"


def test_missing_operation_is_validation_failure(tmp_path) -> None:
    request = json.loads(_wire(tmp_path))
    request.pop("operation")
    response, _ = _invoke(json.dumps(request))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_missing_workspace_is_validation_failure(tmp_path) -> None:
    request = json.loads(_wire(tmp_path))
    request.pop("workspace_root")
    response, _ = _invoke(json.dumps(request))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_null_payload_is_rejected(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path, payload=None))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_extra_envelope_field_is_rejected(tmp_path) -> None:
    response, _ = _invoke(_wire(tmp_path, unexpected="field"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_structured_error_uses_exit_code_zero(tmp_path) -> None:
    result = run_module("bridge", stdin=_wire(tmp_path, operation="runtime.unknown"))
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"
    assert response["error"]["recovery"]


def test_error_code_and_request_id_are_propagated(tmp_path) -> None:
    request_id = str(uuid.uuid4())
    response, _ = _invoke(_wire(tmp_path, request_id=request_id, operation="runtime.unknown"))
    assert response["request_id"] == request_id
    assert response["error"]["code"] == "VALIDATION_FAILED"
    assert "runtime.unknown" in " ".join(response["error"]["details"])


def test_jev_decide_requires_a_dridh_agent_caller(tmp_path) -> None:
    request = _wire(
        tmp_path,
        operation="jev.decide",
        payload={
            "state": "A short task summary.",
            "questions": [{"key": "ready", "type": "noul", "instructions": "Is this ready?"}],
        },
    )
    response, _ = _invoke(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTHORIZATION_DENIED"


def test_jev_decide_persists_deterministic_fallback_for_dridh_agents(tmp_path, monkeypatch) -> None:
    from dridh.state.store import StateStore
    from dridh.workspace.layout import init_workspace

    for name in ("DRIDH_JEV_URL", "DRIDH_JEV_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DRIDH_JEV_AGENT_ACCESS", "all")
    project = tmp_path / "jev-project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    request = _wire(
        project,
        operation="jev.decide",
        payload={
            "state": "The release candidate passes deterministic checks.",
            "evaluation_level": "TASK",
            "task_type": "test_policy",
            "subject_id": "release-candidate-1",
            "questions": [{"key": "ready", "type": "noul", "instructions": "Is the release ready?"}],
        },
        caller={"source": "plugin", "agent": "dridh/planner", "session_id": "ses_jev_test"},
    )
    response, _ = _invoke(request)
    assert response["ok"] is True
    assert response["data"]["used_fallback"] is True
    assert response["data"]["decision"] == "FALLBACK_DETERMINISTIC"
    with StateStore(info.state_db, info.workspace_id) as store:
        row = store.read().execute(
            "SELECT subject_type,subject_id,status,decision,input_sha,output_json FROM jev_evaluation WHERE workspace_id=?",
            (info.workspace_id,),
        ).fetchone()
        assert row is not None
        assert row["subject_type"] == "TASK"
        assert row["subject_id"] == "release-candidate-1"
        assert row["status"] == "FALLBACK"
        assert row["decision"] == "FALLBACK_DETERMINISTIC"
        assert len(row["input_sha"]) == 64
        assert "release candidate passes" not in row["output_json"]


def test_jev_decide_defaults_to_coordinator_only(tmp_path, monkeypatch) -> None:
    from dridh.state.store import StateStore
    from dridh.workspace.layout import init_workspace

    monkeypatch.delenv("DRIDH_JEV_AGENT_ACCESS", raising=False)
    monkeypatch.delenv("DRIDH_JEV_URL", raising=False)
    monkeypatch.delenv("DRIDH_JEV_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_PROVIDER_CREDENTIAL", raising=False)
    project = tmp_path / "jev-default-access"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    request = _wire(
        project,
        operation="jev.decide",
        payload={
            "state": "A short task summary.",
            "questions": [{"key": "ready", "type": "noul", "instructions": "Is this ready?"}],
        },
        caller={"source": "plugin", "agent": "dridh/planner", "session_id": "ses_policy_test"},
    )
    response, _ = _invoke(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTHORIZATION_DENIED"


def test_jev_decide_allowlist_can_grant_selected_agent_access(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DRIDH_JEV_AGENT_ACCESS", "allowlist")
    monkeypatch.setenv("DRIDH_JEV_ALLOWED_AGENTS", "dridh/planner")
    request = _wire(
        tmp_path,
        operation="jev.decide",
        payload={
            "state": "A short task summary.",
            "questions": [{"key": "ready", "type": "noul", "instructions": "Is this ready?"}],
        },
        caller={"source": "plugin", "agent": "dridh/ideator", "session_id": "ses_allowlist_test"},
    )
    response, _ = _invoke(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTHORIZATION_DENIED"
