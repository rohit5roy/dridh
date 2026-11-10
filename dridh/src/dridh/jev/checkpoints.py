"""Mandatory Jev checkpoints for the stage services (plan 9.3 task level, 9.4 fallback, 9.6 advisory).

Python calls Jev after a stage's deterministic validation has passed. The result is recorded and shown to the human
reviewer, never used to approve, and never allowed to block: when Jev is disabled, unavailable, slow or answers
invalidly the outcome is ``FALLBACK_DETERMINISTIC`` and the deterministic path continues unchanged (owner decision,
2026-11-08). A ``REPAIR`` or ``HUMAN_REQUIRED`` verdict is surfaced as a flag for the reviewer, because Jev returns no
exact location the agent could repair; deterministic validators remain the repair source.
"""

from __future__ import annotations

from typing import Any

from dridh.jev.evaluation import JevEvaluationService, default_evaluation_questions
from dridh.workspace.layout import WorkspaceInfo

STAGE_TASK_TYPES = {
    "ideator": "use_case_completeness",
    "designer": "technical_design_completeness",
    "planner": "planning_completeness",
    "executor": "story_readiness",
}
FLAGGED = {"REPAIR", "HUMAN_REQUIRED"}


def checkpoint_threshold(workspace: WorkspaceInfo, task_type: str) -> float:
    """The readiness threshold for one checkpoint: the project's ``jev.thresholds`` in dridh.yaml, else the default."""
    from dridh.config.dridh_yaml import CONFIG_FILENAME, DEFAULT_JEV_THRESHOLDS, load_config

    try:
        return load_config(workspace.root / CONFIG_FILENAME).jev.thresholds[task_type]
    except (OSError, ValueError, KeyError, AttributeError):
        return DEFAULT_JEV_THRESHOLDS[task_type]


def run_checkpoint(
    workspace: WorkspaceInfo, *, stage: str, subject_id: str, agent_id: str, summary: str,
    session_id: str | None = None, service: JevEvaluationService | None = None, metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one stage output. Never raises and never changes workflow state."""
    task_type = STAGE_TASK_TYPES[stage]
    threshold = checkpoint_threshold(workspace, task_type)
    try:
        result = (service or JevEvaluationService(workspace)).evaluate(
            f"Task type: {task_type}\nStage: {stage}\nDeterministic validation: PASS\n{summary}",
            default_evaluation_questions(task_type),
            subject_type="TASK", subject_id=subject_id, agent_id=agent_id, session_id=session_id,
            evaluation_level="TASK", task_type=task_type, metadata=metadata, readiness_threshold=threshold,
        )
    except Exception as exc:  # noqa: BLE001 - Jev (or its persistence) failing must never stop a deterministic workflow
        return {"evaluation_id": None, "status": "UNAVAILABLE", "decision": "FALLBACK_DETERMINISTIC", "readiness": None,
                "used_fallback": True, "flagged": False, "advisory": True, "threshold": threshold, "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"evaluation_id": result.evaluation_id, "status": result.status, "decision": result.decision,
            "readiness": result.readiness_probability, "used_fallback": result.used_fallback,
            "flagged": result.decision in FLAGGED, "advisory": True, "threshold": threshold, "detail": result.detail[:300]}
