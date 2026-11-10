"""Jev evaluation orchestration and durable workspace persistence.

The HTTP client is advisory. This service persists a redacted evaluation envelope,
the source digest, typed criteria and provider health. It never stores the raw input
state or credentials and never makes a human approval decision.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dridh.common.redaction import redact
from dridh.jev import JevClient, JevQuestion, JevStatus
from dridh.state.execution import JevRepository
from dridh.state.store import StateStore, utc_now
from dridh.workspace.layout import WorkspaceInfo

READINESS_THRESHOLD = 0.90
MAX_CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class JevEvaluationResult:
    evaluation_id: str
    status: str
    decision: str
    answers: dict[str, Any]
    readiness_probability: float | None
    readiness_score: float | None
    used_fallback: bool
    attempts: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "status": self.status,
            "decision": self.decision,
            "answers": self.answers,
            "readiness_probability": self.readiness_probability,
            "readiness_score": self.readiness_score,
            "used_fallback": self.used_fallback,
            "attempts": self.attempts,
            "detail": self.detail,
        }


def default_evaluation_questions(task_type: str = "agent_turn") -> list[JevQuestion]:
    """Questions use only the three experimentally supported Jev 1.13 types."""
    readiness_guidance = {
        "planning_completeness": (
            "Assess whether every implementation story has a valid originating Use Case, "
            "clear dependencies, complete acceptance criteria, permitted paths and an acyclic dependency graph."
        ),
        "story_readiness": (
            "Assess whether the implementation satisfies the story's originating Use Case, "
            "scope, acceptance checks, tests, no-new-mocks policy and required delivery evidence."
        ),
        "use_case_completeness": (
            "Assess coverage of actors, success flows, alternate flows, exception flows, boundaries "
            "and traceability to the stated objective."
        ),
        "technical_design_completeness": (
            "Assess whether every approved Use Case and flow has a technical destination, "
            "with component responsibilities, contracts, risks and testability addressed."
        ),
    }
    readiness_instructions = readiness_guidance.get(
        task_type,
        "Assess whether this output satisfies its stated objective, constraints, evidence and acceptance criteria.",
    )
    return [
        JevQuestion(
            "readiness",
            "noul",
            readiness_instructions + " Return a readiness probability from 0 to 1.",
        ),
        JevQuestion(
            "next_action",
            "choice",
            "What is the appropriate next action based on the supplied evidence?",
            {
                "ready": "The output is sufficiently complete for the next deterministic or human gate.",
                "repair": "The output has correctable gaps; send precise findings to the same role for another attempt.",
                "human": "The remaining issue requires a human decision or missing information.",
            },
        ),
    ]


class JevEvaluationService:
    def __init__(self, workspace: WorkspaceInfo, *, client: JevClient | None = None) -> None:
        self.workspace = workspace
        self.client = client or JevClient()

    def evaluate(
        self,
        state: str,
        questions: Sequence[JevQuestion],
        *,
        subject_type: str,
        subject_id: str,
        agent_id: str,
        session_id: str | None,
        evaluation_level: str,
        task_type: str,
        metadata: dict[str, Any] | None = None,
        readiness_threshold: float = READINESS_THRESHOLD,
        deterministic_findings: Sequence[str] = (),
    ) -> JevEvaluationResult:
        safe_state = str(redact(state))[:MAX_CONTEXT_CHARS]
        input_sha = hashlib.sha256(safe_state.encode("utf-8")).hexdigest()
        evaluation_id = str(uuid.uuid4())
        started_at = utc_now()
        started = time.monotonic()
        outcome = self.client.evaluate(
            safe_state,
            list(questions),
            lambda question: {"type": question.type, "fallback": True},
        )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))

        answers = outcome.answers
        readiness = None
        reading = answers.get("readiness")
        if isinstance(reading, dict) and reading.get("type") == "noul":
            value = reading.get("noul")
            if isinstance(value, int | float) and not isinstance(value, bool):
                readiness = min(1.0, max(0.0, float(value)))

        chosen_action = None
        action = answers.get("next_action")
        if isinstance(action, dict) and action.get("type") == "choice":
            chosen_action = action.get("choice")
        if deterministic_findings:
            decision = "REPAIR"
        elif outcome.decided_by_jev and (readiness is not None and readiness < readiness_threshold):
            decision = "REPAIR"
        elif outcome.decided_by_jev and chosen_action == "repair":
            decision = "REPAIR"
        elif outcome.decided_by_jev and chosen_action == "human":
            decision = "HUMAN_REQUIRED"
        elif outcome.decided_by_jev and (readiness is None or readiness >= readiness_threshold):
            decision = "READY_FOR_REVIEW"
        else:
            decision = "FALLBACK_DETERMINISTIC"

        status = "SUCCEEDED" if outcome.decided_by_jev else "FALLBACK"
        provider_status = (
            "HEALTHY" if outcome.status is JevStatus.ANSWERED
            else "UNAVAILABLE" if outcome.status in {JevStatus.UNAVAILABLE, JevStatus.TIMEOUT, JevStatus.QUOTA_EXHAUSTED}
            else "DEGRADED"
        )
        detail = str(redact(outcome.detail))[:500]
        rationale = detail
        if deterministic_findings:
            rationale = "Deterministic validation found blocking issues."
        elif readiness is not None:
            rationale = f"Jev readiness probability: {readiness:.4f}."
        output = {
            "workspace_id": self.workspace.workspace_id,
            "feature_id": (metadata or {}).get("feature_id") or subject_id,
            "feature_id_source": "metadata" if (metadata or {}).get("feature_id") else "evaluation_subject",
            "evaluation_level": evaluation_level,
            "task_type": task_type,
            "agent_id": agent_id,
            "session_id": session_id,
            "source_revision_sha": (metadata or {}).get("source_revision_sha") or input_sha,
            "expected_response_type": "typed_answers",
            "criteria": [
                {
                    "key": question.key,
                    "type": question.type,
                    "instructions": question.instructions[:500],
                    "criteria": question.criteria,
                }
                for question in questions
            ],
            "attempts": outcome.attempts,
            "used_fallback": outcome.used_fallback,
            "answers": answers,
            "metadata": metadata or {},
            "deterministic_findings": [str(redact(item))[:500] for item in deterministic_findings[:20]],
        }
        score = readiness * 100.0 if readiness is not None else None

        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as conn:
                repository = JevRepository(conn, self.workspace.workspace_id)
                repository.record_evaluation(
                    evaluation_id=evaluation_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    provider="opencode-system-one",
                    model=self.client.model,
                    input_sha=input_sha,
                    status=status,
                    decision=decision,
                    score=score,
                    rationale=rationale,
                    output=output,
                    started_at=started_at,
                    completed_at=utc_now(),
                )
                for question in questions:
                    answer = answers.get(question.key, {})
                    verdict = decision
                    criterion_score = None
                    if isinstance(answer, dict):
                        if question.type == "noul" and isinstance(answer.get("noul"), int | float):
                            criterion_score = float(answer["noul"]) * 100.0
                        elif question.type == "choice":
                            verdict = str(answer.get("choice", decision)).upper()
                            confidence = answer.get("confidence")
                            criterion_score = float(confidence) * 100.0 if isinstance(confidence, int | float) else None
                        elif question.type == "score":
                            value = answer.get("score")
                            criterion_score = float(value) if isinstance(value, int | float) else None
                    repository.record_criterion(
                        criterion_id=str(uuid.uuid4()),
                        evaluation_id=evaluation_id,
                        criterion=question.key,
                        verdict=verdict if outcome.decided_by_jev else "FALLBACK_DETERMINISTIC",
                        score=criterion_score,
                        rationale=question.instructions[:500],
                        evidence={"answer": answer} if isinstance(answer, dict) else {},
                    )
                repository.provider_health(
                    health_id=str(uuid.uuid4()),
                    provider="opencode-system-one",
                    model=self.client.model,
                    status=provider_status,
                    latency_ms=latency_ms,
                    error_code=None if outcome.decided_by_jev else outcome.status.value,
                    metadata={"evaluation_id": evaluation_id, "evaluation_level": evaluation_level},
                )

        return JevEvaluationResult(
            evaluation_id=evaluation_id,
            status=outcome.status.value,
            decision=decision,
            answers=answers,
            readiness_probability=readiness,
            readiness_score=score,
            used_fallback=outcome.used_fallback,
            attempts=outcome.attempts,
            detail=detail,
        )
