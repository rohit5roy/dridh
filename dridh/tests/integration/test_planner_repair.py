"""The Planner repairs contract violations from its workers (plan rule 9), but only a bounded number of times."""

from __future__ import annotations

import json
from types import SimpleNamespace

from dridh.integrations.opencode import AgentErrorCode, AgentRunError
from dridh.planner.service import PlannerService
from tests.e2e.planner_test_helpers import seed_approved_baseline

MARKER = "APPROVED INPUT (canonical JSON):\n"


def _valid_indexer_reply(prompt: str) -> dict:
    """One story per (approved flow, owning component), built from the baseline in the prompt."""
    baseline = json.loads(prompt.rsplit(MARKER, 1)[1])
    owners: dict[str, set[str]] = {}
    for item in baseline["tdd_traceability"]:
        if item.get("flow_id") and item.get("component_id"):
            owners.setdefault(item["flow_id"], set()).add(item["component_id"])
    stories = []
    for flow in baseline["flows"]:
        for component_id in sorted(owners.get(flow["id"], ())):
            key = f"{flow['id']}-{component_id}".lower().replace(".", "-")
            stories.append({
                "key": key, "component_id": component_id, "title": f"{flow['subject']} ({component_id})",
                "description": f"Implement {flow['action']}.", "use_case_ids": [flow["use_case_id"]], "flow_ids": [flow["id"]],
                "todos": ["Implement."], "acceptance_criteria": ["Verified."], "definition_of_done": ["Tests pass."],
                "dependencies": [], "dependency_rationale": {}, "file_ownership": [f"src/{key}.py"],
                "resource_ownership": [], "infrastructure": False, "infrastructure_rationale": "",
            })
    return {"summary": "Plan.", "blockers": [], "stories": stories, "project_structure": {"summary": "s", "recommendations": []}}


class ScriptedPlannerRunner:
    """Answers the Indexer: ``bad_replies`` contract-violating replies first, then valid ones."""

    def __init__(self, bad_replies: int, error: Exception | None = None) -> None:
        self.bad_replies, self.error, self.calls, self.first_prompt = bad_replies, error, [], None

    def run_sync(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        self.first_prompt = self.first_prompt or kwargs["prompt"]
        reply = _valid_indexer_reply(self.first_prompt)
        if len(self.calls) <= self.bad_replies:
            reply["stories"][0]["component_id"] = None  # the contract violation seen in the live run
        return SimpleNamespace(text=json.dumps(reply), session_id="ses_planner0001")


def _service(tmp_path, runner):
    info, designer_session = seed_approved_baseline(tmp_path / "project")
    return PlannerService(info, runner=runner), designer_session


def test_a_contract_violation_is_repaired_and_recorded(tmp_path):
    runner = ScriptedPlannerRunner(bad_replies=2)
    service, designer_session = _service(tmp_path, runner)
    started = service.start(designer_session, model="m")
    assert started["status"] == "INQUIRY" and started["stories"] > 0
    assert len(runner.calls) == 3
    assert "session_id" not in runner.calls[0] and runner.calls[1]["session_id"] == "ses_planner0001"
    assert "component not defined in the approved TDD" in runner.calls[1]["prompt"]
    assert "valid component_id values: CMP-01, CMP-02" in runner.calls[1]["prompt"]
    history = service._load(started["session_id"])["repair_history"]
    assert [len(entry["attempts"]) for entry in history] == [2]
    assert history[0]["agent"] == "dridh/workers/planner-indexer"


def test_the_repair_budget_is_bounded_and_ends_blocked_for_a_human(tmp_path):
    runner = ScriptedPlannerRunner(bad_replies=99)
    service, designer_session = _service(tmp_path, runner)
    started = service.start(designer_session, model="m")
    assert started["status"] == "BLOCKED" and len(runner.calls) == 4  # first call + three repairs
    error = started["worker_error"]
    assert error["code"] == "WORKER_FAILED" and error["retryable"] is False
    assert "after 3 repair attempts" in error["message"] and "Human intervention" in error["recovery"]
    assert len(error["details"]) == 4


def test_provider_failures_are_not_repaired(tmp_path):
    runner = ScriptedPlannerRunner(bad_replies=0, error=AgentRunError(AgentErrorCode.QUOTA_EXHAUSTED, "daily limit"))
    service, designer_session = _service(tmp_path, runner)
    started = service.start(designer_session, model="m")
    assert started["status"] == "BLOCKED" and len(runner.calls) == 1
    assert started["worker_error"]["code"] == "QUOTA_EXHAUSTED"


def _started(tmp_path):
    service, designer_session = _service(tmp_path, ScriptedPlannerRunner(bad_replies=0))
    return service, service.start(designer_session, model="m")["session_id"]


def test_approval_needs_a_generated_plan(tmp_path):
    import pytest

    from dridh.common.errors import DridhError

    service, session_id = _started(tmp_path)
    with pytest.raises(DridhError, match="not generated"):
        service.approval_challenge(session_id)
    assert service.gate(session_id)["unlocked"] is False


def test_a_wrong_confirmation_is_refused_and_the_digest_tracks_plan_definitions(tmp_path):
    import pytest

    from dridh.common.errors import DridhError

    service, session_id = _started(tmp_path)
    payload = service._load(session_id)
    payload["status"] = "GENERATED"
    payload["generated"] = {"central_plan": "C.md", "story_count": 1}
    service._save(payload)
    with pytest.raises(DridhError, match="Open the plan review first"):
        service.approval_challenge(session_id)
    payload = service._load(session_id)
    payload["review"] = {"review_id": "review-1", "revision_id": "rev-1"}
    service._save(payload)
    challenge = service.approval_challenge(session_id)
    assert challenge["confirmation"] == f"APPROVE PLAN {session_id} {challenge['plan_digest'][:12]}"
    with pytest.raises(DridhError, match="did not match"):
        service.approve(session_id, reviewer="r", confirmation="APPROVE")
    first = service.plan_definition_digest(session_id)
    assert service.plan_definition_digest(session_id) == first
    payload = service._load(session_id)
    payload["indexer"]["stories"][0]["title"] = "A different story"
    service._save(payload)
    assert service.plan_definition_digest(session_id) != first
