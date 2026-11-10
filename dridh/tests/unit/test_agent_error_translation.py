"""A model-runner failure keeps its meaning at the bridge and the CLI (found live: a Designer worker timeout reached the
agent and the person as "An unexpected internal error occurred")."""

from __future__ import annotations

import pytest

from dridh.common.errors import ErrorCode, from_agent_error, internal_error
from dridh.integrations.opencode import AgentErrorCode, AgentRunError


def test_a_worker_timeout_is_a_retryable_worker_timeout():
    error = internal_error(AgentRunError(AgentErrorCode.TIMEOUT, "Agent invocation timed out after 240.0 seconds.", retryable=True))
    assert error.code is ErrorCode.WORKER_TIMEOUT and error.retryable is True
    assert "timed out after 240.0 seconds" in error.message and "unexpected" not in error.message
    assert "timeout" in error.recovery.lower()


@pytest.mark.parametrize(("agent_code", "expected"), [
    (AgentErrorCode.QUOTA_EXHAUSTED, ErrorCode.DEPENDENCY_UNAVAILABLE),
    (AgentErrorCode.OPENCODE_UNAVAILABLE, ErrorCode.DEPENDENCY_UNAVAILABLE),
    (AgentErrorCode.PROVIDER_FAILURE, ErrorCode.WORKER_FAILED),
    (AgentErrorCode.CANCELLED, ErrorCode.CANCELLED),
])
def test_other_runner_failures_are_typed(agent_code, expected):
    assert from_agent_error(AgentRunError(agent_code, "x")).code is expected


def test_other_exceptions_stay_internal_and_hide_their_text():
    error = internal_error(RuntimeError("secret path /home/x"))
    assert error.code is ErrorCode.INTERNAL_ERROR and "secret" not in error.message
    assert from_agent_error(RuntimeError("x")) is None


def test_the_cli_reports_a_runner_failure_as_a_dridh_error(monkeypatch, capsys):
    import dridh.cli.main as cli

    def boom():
        raise AgentRunError(AgentErrorCode.TIMEOUT, "Agent invocation timed out after 240.0 seconds.", retryable=True)

    monkeypatch.setattr(cli, "app", boom)
    monkeypatch.setattr(cli.sys, "argv", ["dridh", "designer", "generate", "x"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code != 0
    err = capsys.readouterr().err
    assert "WORKER_TIMEOUT" in err and "Traceback" not in err
