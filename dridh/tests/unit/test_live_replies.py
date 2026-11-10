"""The live-test diagnostics must show the model's raw reply (and never leak secrets)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.integration.live_replies import ReplyRecorder, failure_message


class _FakeRunner:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text, self.error = text, error

    def run_sync(self, **kwargs):
        if self.error:
            raise self.error
        return SimpleNamespace(exit_code=0, text=self.text)


def test_recorder_keeps_full_reply_and_prints_it_in_failure_message() -> None:
    recorder = ReplyRecorder(_FakeRunner('I cannot comply. {"component_id": "Order Service"}'))
    recorder.run_sync(agent="dridh/workers/planner-indexer", model="m")
    message = failure_message("planner.start failed", {"status": "BLOCKED"}, recorder)
    assert 'I cannot comply. {"component_id": "Order Service"}' in message
    assert "planner-indexer" in message and "BLOCKED" in message


def test_recorder_records_runner_errors_with_details_and_reraises() -> None:
    boom = RuntimeError("provider down")
    boom.details = ("HTTP 429",)  # type: ignore[attr-defined]
    recorder = ReplyRecorder(_FakeRunner(error=boom))
    with pytest.raises(RuntimeError):
        recorder.run_sync(agent="a", model="m")
    dumped = recorder.dump()
    assert "provider down" in dumped and "HTTP 429" in dumped


def test_secrets_are_redacted_and_long_replies_are_bounded(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-value")
    recorder = ReplyRecorder(_FakeRunner("key=sk-or-secret-value " + "x" * 60000))
    recorder.run_sync(agent="a", model="m")
    dumped = recorder.dump()
    assert "sk-or-secret-value" not in dumped and "[REDACTED]" in dumped
    assert len(dumped) < 41000 and "more chars" in dumped


def test_empty_recorder_says_so() -> None:
    assert "no model replies" in ReplyRecorder(_FakeRunner()).dump()
