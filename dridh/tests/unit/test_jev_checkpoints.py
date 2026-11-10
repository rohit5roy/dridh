"""Mandatory Jev checkpoints: advisory, recorded, and never able to block or approve (owner decision: fall back when unavailable)."""

from __future__ import annotations

import json

import pytest

from dridh.jev import JevClient
from dridh.jev.checkpoints import STAGE_TASK_TYPES, run_checkpoint
from dridh.jev.evaluation import JevEvaluationService
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline


@pytest.fixture()
def info(tmp_path):
    workspace, _ = seed_approved_baseline(tmp_path / "project")
    return workspace


class FakeClient(JevClient):
    """Answers with a fixed readiness and next action, or fails the way a dead endpoint does."""

    def __init__(self, readiness=0.97, action="ready", fail: Exception | None = None) -> None:
        super().__init__(base_url="http://127.0.0.1:9", api_key="k")
        self._readiness, self._action, self._fail = readiness, action, fail

    def evaluate(self, state, questions, fallback):
        from dridh.jev.client import JevOutcome, JevStatus

        if self._fail:
            raise self._fail
        answers = {"readiness": {"type": "noul", "noul": self._readiness}, "next_action": {"type": "choice", "choice": self._action, "confidence": 0.9}}
        return JevOutcome(JevStatus.ANSWERED, answers, False, 1, "ok")


def _run(info, client, stage="planner"):
    return run_checkpoint(info, stage=stage, subject_id="s-1", agent_id="dridh/planner", summary="Plan validated.",
                          service=JevEvaluationService(info, client=client))


def test_every_stage_has_a_task_type():
    assert set(STAGE_TASK_TYPES) == {"ideator", "designer", "planner", "executor"}


def test_a_healthy_jev_is_recorded_and_stays_advisory(info):
    result = _run(info, FakeClient(0.97, "ready"))
    assert result["decision"] == "READY_FOR_REVIEW" and result["flagged"] is False and result["advisory"] is True
    with StateStore(info.state_db, info.workspace_id) as store:
        row = store.read().execute("SELECT status,decision FROM jev_evaluation").fetchone()
    assert (row["status"], row["decision"]) == ("SUCCEEDED", "READY_FOR_REVIEW")


@pytest.mark.parametrize(("readiness", "action", "decision"), [(0.5, "ready", "REPAIR"), (0.99, "repair", "REPAIR"), (0.99, "human", "HUMAN_REQUIRED")])
def test_a_negative_verdict_is_flagged_for_the_reviewer_not_enforced(info, readiness, action, decision):
    result = _run(info, FakeClient(readiness, action))
    assert result["decision"] == decision and result["flagged"] is True and result["advisory"] is True


def test_an_unconfigured_jev_falls_back_to_the_deterministic_path(info, monkeypatch):
    for name in ("DRIDH_JEV_URL", "DRIDH_JEV_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    result = run_checkpoint(info, stage="designer", subject_id="s-2", agent_id="dridh/technical-designer", summary="TDD validated.")
    assert result["decision"] == "FALLBACK_DETERMINISTIC" and result["used_fallback"] is True and result["flagged"] is False
    assert result["readiness"] is None  # a score is never fabricated


def test_a_crashing_jev_or_database_never_stops_the_stage(info):
    result = _run(info, FakeClient(fail=RuntimeError("boom")))
    assert result["decision"] == "FALLBACK_DETERMINISTIC" and result["status"] == "UNAVAILABLE" and result["flagged"] is False


def test_the_checkpoint_stores_a_digest_and_no_summary_text(info):
    secret = "sk" + "-" + "canary" + "-value-1234567890"
    run_checkpoint(info, stage="planner", subject_id="s-3", agent_id="dridh/planner", summary=f"Plan validated. {secret}",
                   service=JevEvaluationService(info, client=FakeClient()))
    with StateStore(info.state_db, info.workspace_id) as store:
        rows = store.read().execute("SELECT * FROM jev_evaluation").fetchall()
    assert secret not in json.dumps([dict(r) for r in rows], default=str)
