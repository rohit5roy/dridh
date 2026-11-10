"""Phase 11 bridge operation allow-list and Coordinator-only discovery contract."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from dridh.common.protocol import handle_request

COORDINATOR = {"source": "plugin", "agent": "dridh/coordinator", "session_id": "ses_ideator0001"}


def _call(operation: str, workspace: Path, payload: dict | None = None, caller: dict | None = None) -> dict:
    request = {
        "protocol_version": 1,
        "request_id": str(uuid.uuid4()),
        "workspace_root": str(workspace),
        "operation": operation,
        "payload": payload or {},
    }
    if caller is not None:
        request["caller"] = caller
    return handle_request(json.dumps(request)).model_dump(mode="json")


def test_ideator_bridge_is_coordinator_only_and_persists_resume_key(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialised = _call("workspace.init", project, caller=COORDINATOR)
    assert initialised["ok"] is True

    denied = _call("ideator.start", project, {"objective": "A product idea"}, caller={"source": "cli"})
    assert denied["ok"] is False
    assert denied["error"]["code"] == "AUTHORIZATION_DENIED"

    started = _call(
        "ideator.start",
        project,
        {"objective": "Let customers submit orders", "existing_context": "Customers currently email orders."},
        caller=COORDINATOR,
    )
    assert started["ok"] is True
    session_id = started["data"]["session_id"]
    assert session_id.startswith("inquiry-")

    answered = _call(
        "ideator.answer",
        project,
        {
            "session_id": session_id,
            "topic": "problem",
            "question": "What is the problem?",
            "answer": "Order intake is manual.",
            "source": "user",
        },
        caller=COORDINATOR,
    )
    assert answered["ok"] is True
    assert answered["data"]["questions_recorded"] == 1
    assert answered["data"]["parity"]["ok"] is True

    resumed = _call("ideator.status", project, {"session_id": session_id}, caller=COORDINATOR)
    assert resumed["ok"] is True
    assert resumed["data"]["session_id"] == session_id
    assert "problem" not in resumed["data"]["topics_missing"]


def test_ideator_approval_is_not_a_bridge_operation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _call("workspace.init", project, caller=COORDINATOR)
    response = _call("ideator.approve", project, {"session_id": "inquiry-0123456789ab"}, caller=COORDINATOR)
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_FAILED"


def test_ideator_start_binds_the_feature_and_refuses_a_second_session_for_it(tmp_path: Path) -> None:
    """Found in a Coordinator-driven real-model run: no operation could bind the Ideator session, the lifecycle never left
    IDEATION, and the agent piled up 22 throwaway sessions trying to make it advance."""
    project = tmp_path / "project"
    project.mkdir()
    assert _call("workspace.init", project, {"mode": "new"}, caller=COORDINATOR)["ok"] is True

    unbound = _call("ideator.start", project, {"objective": "An idea"}, caller=COORDINATOR)
    assert unbound["ok"] is True and "coordinator.create_feature" in unbound["data"]["warning"]

    assert _call("coordinator.create_feature", project, {"name": "Greeter"}, caller=COORDINATOR)["ok"] is True
    started = _call("ideator.start", project, {"objective": "Greet people"}, caller=COORDINATOR)  # the only open feature
    assert started["ok"] is True and started["data"]["feature"] == "greeter"
    status = _call("coordinator.status", project, {"feature": "greeter"}, caller=COORDINATOR)["data"]
    assert status["sessions"]["ideator_session_id"] == started["data"]["session_id"]

    again = _call("ideator.start", project, {"objective": "Probe", "feature": "greeter"}, caller=COORDINATOR)
    assert again["ok"] is False and again["error"]["code"] == "CONFIGURATION_CONFLICT"
    assert started["data"]["session_id"] in again["error"]["message"]

    assert _call("coordinator.create_feature", project, {"name": "Farewell"}, caller=COORDINATOR)["ok"] is True
    assert _call("coordinator.create_feature", project, {"name": "Welcome"}, caller=COORDINATOR)["ok"] is True
    ambiguous = _call("ideator.start", project, {"objective": "Which one?"}, caller=COORDINATOR)
    assert ambiguous["ok"] is False and "needs `feature`" in ambiguous["error"]["message"]
    unknown = _call("ideator.start", project, {"objective": "x", "feature": "nope"}, caller=COORDINATOR)
    assert unknown["ok"] is False and unknown["error"]["code"] == "VALIDATION_FAILED"
    named = _call("ideator.start", project, {"objective": "Say goodbye", "feature": "farewell"}, caller=COORDINATOR)
    assert named["ok"] is True and named["data"]["feature"] == "farewell"
