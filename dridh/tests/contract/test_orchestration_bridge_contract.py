"""Bridge contract for workflow operations: caller authorisation, validation, lightweight imports and cancellation."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from dridh.common.protocol import handle_request
from tests.integration.test_cli import SOURCE_ROOT


@pytest.fixture(autouse=True)
def _legacy_workflow(monkeypatch):
    """These tests use the retired generic workflow engine to exercise bridge mechanics (see dridh.orchestration.legacy)."""
    monkeypatch.setenv("DRIDH_LEGACY_WORKFLOW", "1")


def _request(operation: str, workspace: Path, payload: dict | None = None, caller: dict | None = None) -> str:
    body: dict = {
        "protocol_version": 1,
        "request_id": str(uuid.uuid4()),
        "workspace_root": str(workspace),
        "operation": operation,
        "payload": payload or {},
    }
    if caller is not None:
        body["caller"] = caller
    return json.dumps(body)


def _call(operation: str, workspace: Path, payload: dict | None = None, caller: dict | None = None) -> dict:
    return handle_request(_request(operation, workspace, payload, caller)).model_dump(mode="json")


COORDINATOR = {"source": "plugin", "agent": "dridh/coordinator", "session_id": "ses_contract0001"}
CLI = {"source": "cli"}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    created = _call("workspace.init", root, caller=COORDINATOR)
    assert created["ok"] is True
    return root


def test_runtime_operations_do_not_import_orchestration(tmp_path: Path) -> None:
    code = (
        "import json, sys\n"
        "from dridh.common.protocol import handle_request\n"
        f"root = {str(tmp_path)!r}\n"
        "for op in ('runtime.ping', 'runtime.info'):\n"
        "    body = json.dumps({'protocol_version': 1, 'request_id': '3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01',"
        " 'workspace_root': root, 'operation': op, 'payload': {}})\n"
        "    assert handle_request(body).model_dump()['ok'] is True\n"
        "loaded = sorted(name for name in sys.modules if name.startswith(('dridh.orchestration', 'dridh.state', 'dridh.reporting', 'dridh.integrations')))\n"
        "print(json.dumps(loaded))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT / "src")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    "caller",
    [
        None,
        {"source": "plugin", "agent": "build"},
        {"source": "plugin", "agent": "dridh/code-reacher"},
        {"source": "plugin", "agent": "dridh/planner"},
        {"source": "plugin"},
        {"source": "cli"},
    ],
)
def test_workflow_operations_refuse_callers_other_than_the_coordinator(project: Path, caller: dict | None) -> None:
    response = _call("workflow.create", project, {"objective": "anything"}, caller=caller)
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTHORIZATION_DENIED"


def test_creating_or_planning_a_workflow_is_retired_without_the_legacy_switch(project: Path, monkeypatch) -> None:
    monkeypatch.delenv("DRIDH_LEGACY_WORKFLOW")
    for operation, payload in (("workflow.create", {"objective": "Create a greeting"}), ("workflow.plan", {"workflow_id": "wf-0123456789ab"})):
        response = _call(operation, project, payload, caller=COORDINATOR)
        assert response["ok"] is False and response["error"]["code"] == "CONFIGURATION_ERROR", response
        assert "retired" in response["error"]["message"] and "dridh execute start" in response["error"]["recovery"]
    assert _call("workflow.verify_ledger", project, caller=COORDINATOR)["ok"] is True  # existing state stays readable


def test_coordinator_plugin_caller_may_create_a_workflow(project: Path) -> None:
    response = _call("workflow.create", project, {"objective": "Create a greeting"}, caller=COORDINATOR)
    assert response["ok"] is True
    assert response["data"]["state"] == "CREATED"
    assert response["data"]["workflow_id"].startswith("wf-")


def test_cli_caller_is_refused_for_workflow_operations(project: Path) -> None:
    response = _call("workflow.create", project, {"objective": "CLI-driven"}, caller=CLI)
    assert response["ok"] is False
    assert response["error"]["code"] == "AUTHORIZATION_DENIED"


def test_caller_with_unknown_field_is_rejected(project: Path) -> None:
    caller = dict(COORDINATOR, role="owner")
    response = _call("workflow.create", project, {"objective": "x"}, caller=caller)
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_approval_and_rejection_are_not_bridge_operations(project: Path) -> None:
    for operation in ("workflow.approve", "workflow.reject", "workflow.waive", "workflow.set_state"):
        response = _call(operation, project, {"workflow_id": "wf-000000000000"}, caller=COORDINATOR)
        assert response["ok"] is False, operation
        assert response["error"]["code"] == "VALIDATION_FAILED", operation


def test_workflow_identifiers_are_validated_before_use(project: Path) -> None:
    for bad in ("../escape", "wf-XYZ", "", 42):
        response = _call("workflow.status", project, {"workflow_id": bad}, caller=COORDINATOR)
        assert response["ok"] is False
        assert response["error"]["code"] == "VALIDATION_FAILED"


def test_unknown_workflow_is_reported_as_not_found(project: Path) -> None:
    response = _call("workflow.status", project, {"workflow_id": "wf-000000000000"}, caller=COORDINATOR)
    assert response["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_execute_before_approval_is_refused_through_the_bridge(project: Path) -> None:
    created = _call("workflow.create", project, {"objective": "Greeting"}, caller=COORDINATOR)
    workflow_id = created["data"]["workflow_id"]
    refused = _call("workflow.execute", project, {"workflow_id": workflow_id}, caller=COORDINATOR)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "INVALID_TRANSITION"


def test_workspace_without_initialisation_is_refused_for_workflows(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    response = _call("workflow.create", bare, {"objective": "Greeting"}, caller=COORDINATOR)
    assert response["ok"] is False
    assert response["error"]["code"] == "WORKSPACE_NOT_FOUND"


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_sigterm_cancels_the_plan_and_terminates_the_worker_process_group(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _call("workspace.init", project, caller=COORDINATOR)["ok"] is True
    workflow_id = _call("workflow.create", project, {"objective": "Cancel me"}, caller=COORDINATOR)["data"]["workflow_id"]

    fake = tmp_path / "fake-opencode"
    worker_pid = tmp_path / "worker.pid"
    child_pid = tmp_path / "child.pid"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo $$ > "{worker_pid}"\n'
        "sleep 300 &\n"
        f'echo $! > "{child_pid}"\n'
        "wait\n"
    )
    fake.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(SOURCE_ROOT / "src"),
            "DRIDH_OPENCODE_BIN": str(fake),
            "DRIDH_WORKER_TIMEOUT_SECONDS": "120",
            "DRIDH_LEASE_TTL_SECONDS": "120",
        }
    )
    request = _request("workflow.plan", project, {"workflow_id": workflow_id}, caller=COORDINATOR)
    process = subprocess.Popen(
        [sys.executable, "-m", "dridh", "bridge"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(project),
    )
    assert process.stdin is not None
    process.stdin.write(request)
    process.stdin.close()

    assert process.stdout is not None
    try:
        _wait_for(worker_pid)
        _wait_for(child_pid)
        worker = int(worker_pid.read_text().strip())
        child = int(child_pid.read_text().strip())
        assert _alive(worker) and _alive(child)
        process.send_signal(signal.SIGTERM)
        stdout = process.stdout.read()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, stdout
    envelope = json.loads(lines[0])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "CANCELLED"
    assert process.returncode == 0

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (_alive(worker) or _alive(child)):
        time.sleep(0.1)
    assert not _alive(worker), "worker process survived cancellation"
    assert not _alive(child), "worker child process survived cancellation"

    status = _call("workflow.status", project, {"workflow_id": workflow_id}, caller=COORDINATOR)
    assert status["data"]["state"] == "CANCELLED"
    assert _call("workflow.verify_ledger", project, caller=COORDINATOR)["data"]["ok"] is True
