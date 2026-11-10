"""OpenCode bridge operation for the shared Jev decision capability."""
from __future__ import annotations

import os
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.protocol import BridgeRequest
from dridh.jev import JevQuestion
from dridh.jev.evaluation import JevEvaluationService
from dridh.workspace.layout import open_workspace
from dridh.workspace.paths import validate_workspace_root

MAX_QUESTION_TEXT = 1000
MAX_SUBJECT_ID = 160


def dispatch_jev(request: BridgeRequest) -> dict[str, Any]:
    caller = request.caller
    if caller is None or caller.source != "plugin" or not (caller.agent or "").startswith("dridh/"):
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "The Jev decision tool is available only to Dridh OpenCode agents.",
            recovery="Call jev_decide from a Dridh agent session.",
        )
    # O-01 remains configurable; default access is restricted to the Coordinator.
    # Operators may opt in all Dridh agents or a named allowlist explicitly.
    access = os.environ.get("DRIDH_JEV_AGENT_ACCESS", "coordinator").strip().lower()
    allowed_agents = {
        item.strip() for item in os.environ.get("DRIDH_JEV_ALLOWED_AGENTS", "").split(",")
        if item.strip()
    }
    if access in {"all", "all_dridh_agents"}:
        permitted = True
    elif access == "allowlist":
        permitted = caller.agent in allowed_agents
    elif access in {"coordinator", "coordinator_only"}:
        permitted = caller.agent == "dridh/coordinator"
    else:
        raise DridhError(
            ErrorCode.CONFIGURATION_ERROR,
            "DRIDH_JEV_AGENT_ACCESS must be all, coordinator or allowlist.",
        )
    if not permitted:
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "Jev access for this agent is disabled by DRIDH_JEV_AGENT_ACCESS.",
            details=(f"agent: {caller.agent}", f"access_mode: {access}"),
            recovery="Use the Coordinator evaluation path or explicitly configure the agent access policy.",
        )

    payload = request.payload
    state = payload.get("state")
    if not isinstance(state, str) or not state.strip():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "state must be a non-empty string.")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 8:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "questions must contain between one and eight typed questions.")

    questions: list[JevQuestion] = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"questions[{index}] must be an object.")
        key, kind, instructions = raw.get("key"), raw.get("type"), raw.get("instructions")
        if not isinstance(key, str) or not key or len(key) > 80:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"questions[{index}].key must be 1–80 characters.")
        if not isinstance(kind, str) or kind not in {"noul", "choice", "score"}:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"questions[{index}].type must be noul, choice or score.")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > MAX_QUESTION_TEXT:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"questions[{index}].instructions must be 1–{MAX_QUESTION_TEXT} characters.")
        criteria = raw.get("criteria")
        if criteria is not None and not isinstance(criteria, dict | list):
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"questions[{index}].criteria must be an object or array.")
        questions.append(JevQuestion(key=key, type=kind, instructions=instructions, criteria=criteria))

    evaluation_level = payload.get("evaluation_level", "TURN")
    if evaluation_level not in {"TURN", "TASK", "ARTIFACT"}:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "evaluation_level must be TURN, TASK or ARTIFACT.")
    task_type = payload.get("task_type", "agent_turn")
    if not isinstance(task_type, str) or not task_type.strip() or len(task_type) > 128:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "task_type must be a non-empty string of at most 128 characters.")
    subject_id = payload.get("subject_id") or caller.session_id or request.request_id
    if not isinstance(subject_id, str) or not subject_id or len(subject_id) > MAX_SUBJECT_ID:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"subject_id must be 1–{MAX_SUBJECT_ID} characters.")

    root = validate_workspace_root(request.workspace_root)
    workspace = open_workspace(root)
    evaluation = JevEvaluationService(workspace).evaluate(
        state,
        questions,
        subject_type=evaluation_level,
        subject_id=subject_id,
        agent_id=caller.agent or "dridh/unknown",
        session_id=caller.session_id,
        evaluation_level=evaluation_level,
        task_type=task_type,
        metadata={"request_id": request.request_id},
    )
    return evaluation.to_dict()
