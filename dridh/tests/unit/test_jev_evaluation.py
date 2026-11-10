"""Durable Jev evaluation decisions and deterministic fallback tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dridh.jev import JevOutcome, JevStatus
from dridh.jev.evaluation import JevEvaluationService, default_evaluation_questions
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace


class AnsweredClient:
    model = "jev-1.13-free"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers

    def evaluate(self, state, questions, fallback):
        return JevOutcome(JevStatus.ANSWERED, self.answers, False, 1, "Jev answered.")


def make_workspace(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return info


def test_readiness_decision_and_criteria_are_persisted_without_raw_input(tmp_path: Path) -> None:
    info = make_workspace(tmp_path)
    raw_state = "Private work context must not be persisted verbatim."
    service = JevEvaluationService(
        info,
        client=AnsweredClient({
            "readiness": {"type": "noul", "noul": 0.96},
            "next_action": {"type": "choice", "choice": "ready", "confidence": 0.98, "probabilities": {"ready": 0.98, "repair": 0.02, "human": 0.0}},
        }),
    )
    result = service.evaluate(
        raw_state,
        default_evaluation_questions(),
        subject_type="TASK",
        subject_id="wf-123:T-001",
        agent_id="dridh/code-reacher",
        session_id="ses_evaluation_test",
        evaluation_level="TASK",
        task_type="story_readiness",
        metadata={"attempt": 1},
    )
    assert result.decision == "READY_FOR_REVIEW"
    assert result.readiness_score == 96.0
    assert result.used_fallback is False

    with StateStore(info.state_db, info.workspace_id) as store:
        conn = store.read()
        row = conn.execute("SELECT * FROM jev_evaluation WHERE id=?", (result.evaluation_id,)).fetchone()
        assert row is not None
        assert row["status"] == "SUCCEEDED"
        assert row["decision"] == "READY_FOR_REVIEW"
        assert row["score"] == 96.0
        assert row["input_sha"] != raw_state
        assert len(row["input_sha"]) == 64
        assert raw_state not in row["output_json"]
        output = json.loads(row["output_json"])
        assert output["agent_id"] == "dridh/code-reacher"
        assert output["session_id"] == "ses_evaluation_test"
        assert output["workspace_id"] == info.workspace_id
        assert output["feature_id"] == "wf-123:T-001"
        assert output["feature_id_source"] == "evaluation_subject"
        assert output["source_revision_sha"] == row["input_sha"]
        assert output["expected_response_type"] == "typed_answers"
        assert {item["key"] for item in output["criteria"]} == {"readiness", "next_action"}
        criteria = conn.execute(
            "SELECT criterion,verdict,score FROM jev_evaluation_criterion WHERE evaluation_id=? ORDER BY criterion",
            (result.evaluation_id,),
        ).fetchall()
        assert len(criteria) == 2
        health = conn.execute(
            "SELECT status FROM jev_provider_health WHERE workspace_id=? ORDER BY checked_at DESC LIMIT 1",
            (info.workspace_id,),
        ).fetchone()
        assert health["status"] == "HEALTHY"


def test_low_readiness_requests_repair_but_never_approves(tmp_path: Path) -> None:
    info = make_workspace(tmp_path)
    service = JevEvaluationService(
        info,
        client=AnsweredClient({
            "readiness": {"type": "noul", "noul": 0.72},
            "next_action": {"type": "choice", "choice": "repair", "confidence": 0.91, "probabilities": {"ready": 0.09, "repair": 0.91, "human": 0.0}},
        }),
    )
    result = service.evaluate(
        "The plan has incomplete acceptance criteria.",
        default_evaluation_questions(),
        subject_type="PLAN",
        subject_id="wf-low-readiness",
        agent_id="dridh/planner",
        session_id="ses_low_readiness",
        evaluation_level="TASK",
        task_type="planning_completeness",
    )
    assert result.decision == "REPAIR"
    assert result.readiness_score == 72.0
    assert result.decision not in {"APPROVED", "HUMAN_APPROVED"}


def test_unavailable_jev_records_fallback_and_does_not_fabricate_score(tmp_path: Path, monkeypatch) -> None:
    info = make_workspace(tmp_path)
    for name in ("DRIDH_JEV_URL", "DRIDH_JEV_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    from dridh.jev import JevClient

    service = JevEvaluationService(info, client=JevClient())
    result = service.evaluate(
        "Deterministic validation passed.",
        default_evaluation_questions(),
        subject_type="TURN",
        subject_id="ses-fallback",
        agent_id="dridh/planner",
        session_id="ses-fallback",
        evaluation_level="TURN",
        task_type="plan",
    )
    assert result.status == JevStatus.DISABLED.value
    assert result.used_fallback is True
    assert result.decision == "FALLBACK_DETERMINISTIC"
    assert result.readiness_score is None
    with StateStore(info.state_db, info.workspace_id) as store:
        row = store.read().execute(
            "SELECT status,decision,score FROM jev_evaluation WHERE id=?",
            (result.evaluation_id,),
        ).fetchone()
        assert tuple(row) == ("FALLBACK", "FALLBACK_DETERMINISTIC", None)
