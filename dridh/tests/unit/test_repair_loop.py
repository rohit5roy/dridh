"""Bounded contract-repair loop (plan rule 9): at most three repairs, then a human."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.integrations.opencode import AgentErrorCode, AgentRunError
from dridh.orchestration.repair import MAX_REPAIRS, call_with_repair

GOOD = json.dumps({"component_id": "CMP-01"})


class ScriptedRunner:
    """Returns scripted replies in order and records every call exactly as the helper made it."""

    def __init__(self, replies, *, session_id="ses_abc12345", fail_resume=False):
        self.replies, self.session_id, self.fail_resume = list(replies), session_id, fail_resume
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if kwargs.get("session_id") and self.fail_resume:
            raise AgentRunError(AgentErrorCode.SESSION_RESUME_FAILED, "cannot resume")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=reply, session_id=self.session_id)


def _validate(parsed):
    if parsed.get("component_id") != "CMP-01":
        raise DridhError(ErrorCode.WORKER_FAILED, "Story references an unknown component.", details=("valid values: CMP-01",))
    return {"normalised": parsed["component_id"]}


def test_valid_first_reply_is_accepted_without_repair_and_without_session_kwarg():
    runner = ScriptedRunner([GOOD])
    outcome = call_with_repair(runner, "PROMPT", label="Indexer", validate=_validate)
    assert outcome.value == {"normalised": "CMP-01"} and not outcome.repaired
    assert runner.calls == [{"prompt": "PROMPT"}]  # first call matches the pre-existing runner contract


def test_invalid_twice_then_valid_succeeds_on_the_third_call_continuing_the_session():
    bad = json.dumps({"component_id": None})
    runner = ScriptedRunner([bad, bad, GOOD])
    outcome = call_with_repair(runner, "PROMPT", label="Indexer", validate=_validate)
    assert outcome.value == {"normalised": "CMP-01"} and len(outcome.attempts) == 2
    assert len(runner.calls) == 3
    second = runner.calls[1]
    assert second["session_id"] == "ses_abc12345"
    assert "Story references an unknown component." in second["prompt"] and "valid values: CMP-01" in second["prompt"]
    assert "PROMPT" not in second["prompt"]  # the session already holds the original prompt
    assert all(item.session_continued for item in outcome.attempts[1:])


def test_always_invalid_stops_after_exactly_four_calls_and_asks_for_a_human():
    runner = ScriptedRunner([json.dumps({"component_id": "bad"})] * 10)
    with pytest.raises(DridhError) as exc_info:
        call_with_repair(runner, "PROMPT", label="Central Plan Indexer", validate=_validate)
    assert len(runner.calls) == MAX_REPAIRS + 1 == 4
    error = exc_info.value
    assert error.code is ErrorCode.WORKER_FAILED and error.retryable is False
    assert "after 3 repair attempts" in error.message and "Central Plan Indexer" in error.message
    assert len(error.details) == 4 and "Human intervention" in error.recovery


def test_a_reply_that_is_not_json_is_repaired():
    runner = ScriptedRunner(["Sure! Here is the plan.", GOOD])
    outcome = call_with_repair(runner, "PROMPT", label="W", validate=_validate)
    assert outcome.repaired and "not a single valid JSON object" in runner.calls[1]["prompt"]


@pytest.mark.parametrize("code", [AgentErrorCode.QUOTA_EXHAUSTED, AgentErrorCode.PROVIDER_FAILURE, AgentErrorCode.TIMEOUT])
def test_runner_failures_are_never_retried(code):
    runner = ScriptedRunner([AgentRunError(code, "boom"), GOOD])
    with pytest.raises(AgentRunError):
        call_with_repair(runner, "PROMPT", label="W", validate=_validate)
    assert len(runner.calls) == 1


def test_non_content_validator_errors_propagate_immediately():
    def validate(_parsed):
        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "target is not a regular file")

    runner = ScriptedRunner([GOOD, GOOD])
    with pytest.raises(DridhError) as exc_info:
        call_with_repair(runner, "PROMPT", label="W", validate=validate)
    assert exc_info.value.code is ErrorCode.CONFIGURATION_CONFLICT and len(runner.calls) == 1


def test_without_a_session_id_the_full_prompt_is_resent_with_the_feedback():
    runner = ScriptedRunner([json.dumps({"component_id": "x"}), GOOD], session_id="")
    call_with_repair(runner, "ORIGINAL PROMPT", label="W", validate=_validate)
    assert "session_id" not in runner.calls[1]
    assert runner.calls[1]["prompt"].startswith("ORIGINAL PROMPT") and "rejected" in runner.calls[1]["prompt"]


def test_a_failed_session_resume_falls_back_to_a_fresh_full_prompt_call():
    runner = ScriptedRunner([json.dumps({"component_id": "x"}), GOOD], fail_resume=True)
    outcome = call_with_repair(runner, "ORIGINAL PROMPT", label="W", validate=_validate)
    assert outcome.value == {"normalised": "CMP-01"}
    assert len(runner.calls) == 3  # initial, failed resume, fresh full-prompt retry (same repair attempt)
    assert runner.calls[1].get("session_id") and "session_id" not in runner.calls[2]
    assert runner.calls[2]["prompt"].startswith("ORIGINAL PROMPT")


def test_validator_may_return_none_to_keep_the_parsed_object():
    runner = ScriptedRunner([GOOD])
    outcome = call_with_repair(runner, "P", label="W", validate=lambda parsed: None)
    assert outcome.value == {"component_id": "CMP-01"}


def test_no_validator_still_requires_a_json_object():
    runner = ScriptedRunner(["no json here", GOOD])
    assert call_with_repair(runner, "P", label="W").value == {"component_id": "CMP-01"}


def test_feedback_is_bounded_and_redacts_secrets():
    secret = "sk-or-v1-" + "a" * 40

    def validate(_parsed):
        raise DridhError(ErrorCode.WORKER_FAILED, "bad " + "x" * 5000, details=(f"token {secret}",) * 20)

    runner = ScriptedRunner([GOOD, GOOD, GOOD, GOOD])
    with pytest.raises(DridhError):
        call_with_repair(runner, "P", label="W", validate=validate)
    feedback = runner.calls[1]["prompt"]
    assert secret not in feedback and len(feedback) < 3500
