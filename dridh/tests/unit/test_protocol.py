from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from dridh.common.errors import ErrorCode
from dridh.common.protocol import (
    ALLOWED_OPERATIONS,
    PROTOCOL_VERSION,
    BridgeFailure,
    BridgeRequestError,
    BridgeSuccess,
    decode_request,
    dispatch,
    handle_request,
    serialise_response,
)


def request_payload(workspace: Path, operation: str = "runtime.ping") -> dict:
    return {
        "protocol_version": 1,
        "request_id": "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01",
        "workspace_root": str(workspace),
        "operation": operation,
        "payload": {},
    }


def test_decode_valid_request_normalises_uuid() -> None:
    workspace = Path("/tmp")
    request = decode_request(json.dumps(request_payload(workspace)))
    assert request.request_id == "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01"
    assert request.operation in ALLOWED_OPERATIONS
    assert request.payload == {}


def test_ping_returns_typed_success(workspace: Path) -> None:
    response = handle_request(json.dumps(request_payload(workspace)))
    assert isinstance(response, BridgeSuccess)
    assert response.ok is True
    assert response.protocol_version == PROTOCOL_VERSION
    assert response.request_id == "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01"
    assert response.data["status"] == "healthy"
    assert response.data["workspace_root"] == str(workspace.resolve())


def test_info_returns_runtime_metadata(workspace: Path) -> None:
    response = handle_request(json.dumps(request_payload(workspace, "runtime.info")))
    assert isinstance(response, BridgeSuccess)
    data = response.data
    assert data["package"] == "dridh"
    assert data["package_version"]
    assert data["python_version"]
    assert data["bridge_protocol_version"] == 1
    assert data["operations"] == sorted(ALLOWED_OPERATIONS)
    assert {"runtime.info", "runtime.ping"} <= set(data["operations"])
    assert data["workspace_root"] == str(workspace.resolve())
    # In-process the flag reflects whatever earlier tests imported; the meaningful check is a fresh interpreter (below).
    assert isinstance(data["heavy_dependencies_loaded"], bool)


def test_runtime_info_reports_no_heavy_dependencies_in_a_fresh_process(workspace: Path) -> None:
    import os
    import subprocess
    import sys

    code = (
        "import json, sys; from dridh.common.protocol import handle_request; "
        f"r = handle_request({json.dumps(json.dumps(request_payload(workspace, 'runtime.info')))}); "
        "print(r.data['heavy_dependencies_loaded'])"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert result.stdout.strip() == "False", result.stderr


def test_unknown_operation_is_validation_failure(workspace: Path) -> None:
    payload = request_payload(workspace, "coordinator.start")
    with pytest.raises(BridgeRequestError) as exc_info:
        decode_request(json.dumps(payload))
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
    assert exc_info.value.request_id == payload["request_id"]
    failure = handle_request(json.dumps(payload))
    assert isinstance(failure, BridgeFailure)
    assert failure.error.code == "VALIDATION_FAILED"


def test_wrong_protocol_version_is_protocol_failure(workspace: Path) -> None:
    payload = request_payload(workspace)
    payload["protocol_version"] = 2
    with pytest.raises(BridgeRequestError) as exc_info:
        decode_request(json.dumps(payload))
    assert exc_info.value.code is ErrorCode.PROTOCOL_ERROR
    assert exc_info.value.request_id == payload["request_id"]


def test_request_id_must_be_uuid(workspace: Path) -> None:
    payload = request_payload(workspace)
    payload["request_id"] = "not-a-uuid"
    with pytest.raises(BridgeRequestError) as exc_info:
        decode_request(json.dumps(payload))
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
    assert exc_info.value.request_id == "not-a-uuid"


def test_relative_workspace_is_rejected_before_dispatch() -> None:
    payload = request_payload(Path("/tmp"))
    payload["workspace_root"] = "relative/path"
    with pytest.raises(BridgeRequestError) as exc_info:
        decode_request(json.dumps(payload))
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED


def test_missing_workspace_is_structured_workspace_error(tmp_path: Path) -> None:
    payload = request_payload(tmp_path / "missing")
    response = handle_request(json.dumps(payload))
    assert isinstance(response, BridgeFailure)
    assert response.error.code == "WORKSPACE_NOT_FOUND"
    assert response.request_id == payload["request_id"]
    assert response.error.recovery


def test_payload_must_be_an_object(workspace: Path) -> None:
    payload = request_payload(workspace)
    payload["payload"] = ["not", "an", "object"]
    response = handle_request(json.dumps(payload))
    assert isinstance(response, BridgeFailure)
    assert response.error.code == "VALIDATION_FAILED"


def test_extra_fields_are_rejected(workspace: Path) -> None:
    payload = request_payload(workspace)
    payload["unexpected"] = True
    response = handle_request(json.dumps(payload))
    assert isinstance(response, BridgeFailure)
    assert response.error.code == "VALIDATION_FAILED"


def test_malformed_json_returns_protocol_error() -> None:
    response = handle_request("{not json")
    assert isinstance(response, BridgeFailure)
    assert response.error.code == "PROTOCOL_ERROR"
    assert response.request_id == "unavailable"


def test_non_object_json_returns_protocol_error() -> None:
    response = handle_request("[]")
    assert isinstance(response, BridgeFailure)
    assert response.error.code == "PROTOCOL_ERROR"


def test_serialised_success_is_exactly_one_json_line(workspace: Path) -> None:
    response = dispatch(decode_request(json.dumps(request_payload(workspace))))
    text = serialise_response(response)
    assert text.count("\n") == 0
    parsed = json.loads(text)
    assert parsed["protocol_version"] == 1
    assert parsed["ok"] is True
    assert parsed["request_id"] == "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01"


def test_serialised_failure_is_typed(workspace: Path) -> None:
    payload = request_payload(workspace)
    payload["operation"] = "jev.evaluate"
    response = handle_request(json.dumps(payload))
    parsed = json.loads(serialise_response(response))
    assert parsed["ok"] is False
    assert parsed["protocol_version"] == 1
    assert parsed["error"]["code"] == "VALIDATION_FAILED"
    assert isinstance(parsed["error"]["details"], list)
    assert isinstance(parsed["error"]["retryable"], bool)
    assert parsed["error"]["recovery"]


def test_uuid_validation_accepts_uppercase_and_normalises() -> None:
    workspace = Path("/tmp")
    payload = request_payload(workspace)
    payload["request_id"] = str(uuid.uuid4()).upper()
    request = decode_request(json.dumps(payload))
    assert request.request_id == payload["request_id"].lower()
