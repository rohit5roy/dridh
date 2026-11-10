from __future__ import annotations

import json
import uuid

import dridh
from tests.integration.test_cli import run_module


def _request(workspace, operation: str = "runtime.ping") -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "request_id": str(uuid.uuid4()),
            "workspace_root": str(workspace),
            "operation": operation,
            "payload": {},
        }
    )


def test_ping_stdout_is_exactly_one_protocol_envelope(tmp_path) -> None:
    request_id = str(uuid.uuid4())
    request = json.dumps(
        {
            "protocol_version": 1,
            "request_id": request_id,
            "workspace_root": str(tmp_path),
            "operation": "runtime.ping",
            "payload": {},
        }
    )
    result = run_module("bridge", stdin=request)
    assert result.returncode == 0
    assert result.stdout.count("\n") == 1
    response = json.loads(result.stdout)
    assert response["protocol_version"] == 1
    assert response["request_id"] == request_id
    assert response["ok"] is True
    assert response["data"]["status"] == "healthy"
    diagnostics = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    assert diagnostics
    assert all(isinstance(item, dict) for item in diagnostics)
    assert diagnostics[-1]["event"] == "bridge.request.succeeded"
    assert diagnostics[-1]["operation"] == "runtime.ping"


def test_info_operation_returns_metadata(tmp_path) -> None:
    result = run_module("bridge", stdin=_request(tmp_path, "runtime.info"))
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["ok"] is True
    assert response["data"]["package_version"] == dridh.__version__
    assert response["data"]["bridge_protocol_version"] == 1
    assert response["data"]["workspace_root"] == str(tmp_path.resolve())


def test_unknown_operation_returns_structured_failure(tmp_path) -> None:
    result = run_module("bridge", stdin=_request(tmp_path, "coordinator.start"))
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"
    assert response["error"]["recovery"]
    assert "coordinator.start" in " ".join(response["error"]["details"])


def test_invalid_workspace_returns_structured_failure(tmp_path) -> None:
    missing = tmp_path / "missing"
    request = json.loads(_request(missing))
    result = run_module("bridge", stdin=json.dumps(request))
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"] == "WORKSPACE_NOT_FOUND"
    assert response["request_id"] == request["request_id"]


def test_malformed_request_returns_protocol_failure_without_stdout_noise() -> None:
    result = run_module("bridge", stdin="{not json")
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"] == "PROTOCOL_ERROR"
    assert response["request_id"] == "unavailable"
    assert result.stdout.count("\n") == 1


def test_diagnostics_do_not_echo_payload_secrets(tmp_path) -> None:
    request = json.dumps(
        {
            "protocol_version": 1,
            "request_id": str(uuid.uuid4()),
            "workspace_root": str(tmp_path),
            "operation": "runtime.ping",
            "payload": {"api_key": "sk-proj-supersecretvalue123"},
        }
    )
    result = run_module("bridge", stdin=request)
    assert result.returncode == 0
    assert "supersecretvalue123" not in result.stderr
    assert "supersecretvalue123" not in result.stdout
