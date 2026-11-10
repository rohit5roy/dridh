"""Phase 1.5 agent runner tests using a deterministic fake OpenCode executable."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dridh.integrations.opencode import (
    AgentErrorCode,
    AgentRunError,
    OpenCodeRunner,
)

FAKE_SOURCE = '''#!/usr/bin/env python3
import json, os, sys, time

mode = os.environ.get("FAKE_MODE", "success")
args = sys.argv[1:]
session = "ses_fake1234567890"

if mode == "success":
    print(json.dumps({"type": "text", "sessionID": session, "part": {"text": "FAKE_OK"}}))
elif mode == "echo-stdin":
    print(json.dumps({"type": "text", "sessionID": session, "part": {"text": sys.stdin.read()}}))
    print(json.dumps({"argv": args}), file=sys.stderr)
elif mode == "resume":
    sid = args[args.index("--session") + 1] if "--session" in args else session
    print(json.dumps({"type": "text", "sessionID": sid, "part": {"text": "RESUMED_OK"}}))
elif mode == "wrong-session":
    print(json.dumps({"type": "text", "sessionID": "ses_different123456", "part": {"text": "WRONG"}}))
elif mode == "no-session":
    print(json.dumps({"type": "text", "part": {"text": "NO_SESSION"}}))
elif mode == "no-text":
    print(json.dumps({"type": "step_finish", "sessionID": session, "part": {}}))
elif mode == "malformed":
    print("not json")
elif mode == "fail":
    print("provider failure: upstream unavailable", file=sys.stderr)
    raise SystemExit(3)
elif mode == "quota":
    print(json.dumps({"type": "text", "sessionID": session, "part": {"text": "Free usage limit reached"}}))
elif mode == "quota-error":
    secret = os.environ.get("OPENROUTER_API_KEY", "")
    print(json.dumps({"type": "error", "message": f"Free usage limit reached; opaque=supersecretvalue openrouter={secret}"}))
    raise SystemExit(1)
elif mode == "timeout":
    time.sleep(10)
else:
    print("unknown mode", file=sys.stderr)
    raise SystemExit(2)
'''


@pytest.fixture()
def fake_opencode(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-opencode"
    executable.write_text(FAKE_SOURCE)
    executable.chmod(0o755)
    return executable


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def runner(fake_opencode: Path, workspace: Path, *, timeout: float = 5) -> OpenCodeRunner:
    return OpenCodeRunner(
        executable=str(fake_opencode),
        workspace_root=workspace,
        timeout=timeout,
    )


def test_invocation_success(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "success")
    result = asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt="hello"))
    assert result.text == "FAKE_OK"
    assert result.session_id == "ses_fake1234567890"
    assert result.exit_code == 0
    assert result.resumed is False
    assert result.to_dict()["event_count"] == 1


def test_prompt_is_delivered_verbatim_on_stdin_not_argv(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    """Regression: OpenCode re-quotes positional messages, which broke structured (JSON) prompts."""
    monkeypatch.setenv("FAKE_MODE", "echo-stdin")
    prompt = 'Instructions with spaces.\n\nAPPROVED INPUT (canonical JSON):\n{"a": "b \\"c\\"", "d": [1, 2]}'
    result = asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt=prompt))
    assert result.text == prompt


def test_session_resume_success(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "resume")
    result = asyncio.run(
        runner(fake_opencode, workspace).run(
            agent="dridh/test-worker",
            prompt="continue",
            session_id="ses_fake1234567890",
        )
    )
    assert result.text == "RESUMED_OK"
    assert result.session_id == "ses_fake1234567890"
    assert result.resumed is True


def test_invalid_session_id_rejected(fake_opencode: Path, workspace: Path) -> None:
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(
            runner(fake_opencode, workspace).run(
                agent="dridh/test-worker", prompt="x", session_id="not-a-session"
            )
        )
    assert exc_info.value.code is AgentErrorCode.INVALID_INVOCATION


def test_empty_agent_rejected(fake_opencode: Path, workspace: Path) -> None:
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(runner(fake_opencode, workspace).run(agent=" ", prompt="x"))
    assert exc_info.value.code is AgentErrorCode.INVALID_INVOCATION


def test_missing_session_id_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "no-session")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt="x"))
    assert exc_info.value.code is AgentErrorCode.SESSION_EXTRACTION_FAILED


def test_resume_session_mismatch_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "wrong-session")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(
            runner(fake_opencode, workspace).run(
                agent="dridh/test-worker", prompt="x", session_id="ses_fake1234567890"
            )
        )
    assert exc_info.value.code is AgentErrorCode.SESSION_RESUME_FAILED


def test_malformed_output_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "malformed")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt="x"))
    assert exc_info.value.code is AgentErrorCode.MALFORMED_OUTPUT


def test_missing_text_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "no-text")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt="x"))
    assert exc_info.value.code is AgentErrorCode.MALFORMED_OUTPUT


def test_provider_failure_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt="x"))
    assert exc_info.value.code is AgentErrorCode.PROVIDER_FAILURE


def test_quota_exhaustion_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "quota")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(runner(fake_opencode, workspace).run(agent="dridh/test-worker", prompt="x"))
    assert exc_info.value.code is AgentErrorCode.QUOTA_EXHAUSTED


def test_provider_error_diagnostic_is_captured_and_secret_is_redacted(
    fake_opencode: Path, workspace: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "quota-error")
    configured = OpenCodeRunner(
        executable=str(fake_opencode),
        workspace_root=workspace,
        env={"OPENCODE_API_KEY": "supersecretvalue"},
    )
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(configured.run(agent="dridh/test-worker", prompt="x"))

    assert exc_info.value.code is AgentErrorCode.QUOTA_EXHAUSTED
    diagnostics = "\\n".join(exc_info.value.details)
    assert "Free usage limit reached" in diagnostics
    assert "supersecretvalue" not in diagnostics
    assert "[REDACTED]" in diagnostics


def test_openrouter_api_key_is_redacted_from_provider_diagnostics(
    fake_opencode: Path, workspace: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "quota-error")
    secret = "".join(("sk", "-", "or", "-", "v1", "-", "openrouter-secret-value"))
    configured = OpenCodeRunner(
        executable=str(fake_opencode),
        workspace_root=workspace,
        env={"OPENROUTER_API_KEY": secret},
    )
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(configured.run(agent="dridh/test-worker", prompt="x"))
    diagnostics = "\\n".join(exc_info.value.details)
    assert exc_info.value.code is AgentErrorCode.QUOTA_EXHAUSTED
    assert secret not in diagnostics
    assert "[REDACTED]" in diagnostics


def test_failure_classification_ignores_tool_output_and_assistant_text() -> None:
    """Regression (live run): a worker read a test file containing 'too many requests' and was labelled QUOTA_EXHAUSTED."""
    import json

    from dridh.integrations.opencode import _classify_known_failure, _provider_diagnostic

    tool_event = json.dumps({"type": "tool_use", "part": {"state": {"output": "assert 'Too many requests' and 'quota exhausted' and provider"}}})
    text_event = json.dumps({"type": "text", "part": {"text": "The provider returned: free usage limit"}})
    stdout = tool_event + "\n" + text_event
    assert _classify_known_failure(stdout, "") is None
    assert _provider_diagnostic(stdout, "") == ""


def test_failure_classification_reads_nested_error_events_and_stderr() -> None:
    import json

    from dridh.integrations.opencode import _classify_known_failure

    transport = json.dumps({"type": "error", "error": {"type": "provider.transport", "message": "upstream refused"}})
    assert _classify_known_failure(transport, "") is AgentErrorCode.PROVIDER_FAILURE
    limited = json.dumps({"type": "error", "error": {"name": "APIError", "message": "Too many requests"}})
    assert _classify_known_failure(limited, "") is AgentErrorCode.QUOTA_EXHAUSTED
    assert _classify_known_failure("", "Free usage limit reached") is AgentErrorCode.QUOTA_EXHAUSTED


def test_timeout_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "timeout")
    with pytest.raises(AgentRunError) as exc_info:
        asyncio.run(
            runner(fake_opencode, workspace, timeout=0.2).run(agent="dridh/test-worker", prompt="x")
        )
    assert exc_info.value.code is AgentErrorCode.TIMEOUT
    assert exc_info.value.retryable is True


def test_cancellation_is_classified(fake_opencode: Path, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "timeout")

    async def scenario() -> None:
        task = asyncio.create_task(
            runner(fake_opencode, workspace, timeout=30).run(agent="dridh/test-worker", prompt="x")
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(AgentRunError) as exc_info:
            await task
        assert exc_info.value.code is AgentErrorCode.CANCELLED

    asyncio.run(scenario())


def test_missing_executable_is_classified(tmp_path: Path) -> None:
    with pytest.raises(AgentRunError) as exc_info:
        OpenCodeRunner(executable=str(tmp_path / "missing"), workspace_root=tmp_path)
    assert exc_info.value.code is AgentErrorCode.OPENCODE_UNAVAILABLE


def test_invalid_workspace_is_classified(fake_opencode: Path, tmp_path: Path) -> None:
    with pytest.raises(AgentRunError) as exc_info:
        OpenCodeRunner(executable=str(fake_opencode), workspace_root=tmp_path / "missing")
    assert exc_info.value.code is AgentErrorCode.INVALID_INVOCATION


def test_invocation_selects_agent_explicitly_and_never_continues(fake_opencode: Path, workspace: Path) -> None:
    args = runner(fake_opencode, workspace)._build_args(
        agent="dridh/coordinator",
        prompt="hello",
        session_id=None,
        model=None,
        title=None,
    )
    assert args[args.index("--agent") + 1] == "dridh/coordinator"
    assert args.count("--agent") == 1
    assert "--continue" not in args
    assert "-c" not in args
    assert not any("default_agent" in argument for argument in args)
    # The prompt travels on stdin, never in argv (OpenCode re-quotes positional messages).
    assert "hello" not in args


def test_child_environment_sets_config_dir_only_when_configured(
    fake_opencode: Path, workspace: Path, tmp_path: Path
) -> None:
    assert "OPENCODE_CONFIG_DIR" not in runner(fake_opencode, workspace)._child_env()
    config = tmp_path / "config"
    config.mkdir()
    configured = OpenCodeRunner(
        executable=str(fake_opencode), workspace_root=workspace, config_dir=config
    )._child_env()
    assert configured["OPENCODE_CONFIG_DIR"] == str(config.resolve())
