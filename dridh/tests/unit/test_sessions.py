"""Coordinator session registry: refusal, mismatch, fallback and cancellation rules (fake runner)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.integrations.opencode import AgentErrorCode, AgentRunError, AgentRunResult
from dridh.orchestration.sessions import SessionService
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace, open_workspace


class FakeRunner:
    def __init__(self, *, returns: str = "ses_fake00000000000000000001", error: AgentRunError | None = None) -> None:
        self.returns = returns
        self.error = error
        self.calls: list[dict] = []

    async def run(self, *, agent, prompt, session_id=None, model=None, title=None):
        self.calls.append({"agent": agent, "session_id": session_id, "prompt": prompt})
        if self.error is not None:
            raise self.error
        return AgentRunResult(
            agent=agent,
            prompt=prompt,
            session_id=self.returns if session_id is None else session_id,
            text="ok",
            exit_code=0,
            duration_seconds=0.01,
            resumed=session_id is not None,
        )


@pytest.fixture
def workspace(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    init_workspace(project)
    return open_workspace(project)


def register(workspace, session_id: str, agent: str = "dridh/coordinator", status: str = "ACTIVE") -> None:
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(session_id, agent, workflow_id, purpose, status, created_at, last_used_at) VALUES (?, ?, NULL, 'test', ?, '2026-01-01', '2026-01-01')",
                (session_id, agent, status),
            )


def events(workspace) -> list[str]:
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        return [row[0] for row in store.read().execute("SELECT event_type FROM ledger ORDER BY seq")]


def test_start_registers_the_coordinator_session_and_records_it(workspace) -> None:
    runner = FakeRunner()
    result = asyncio.run(SessionService(workspace, actor="cli:test", runner=runner).start("Objective: x"))
    assert result["session_id"] == "ses_fake00000000000000000001"
    assert runner.calls[0]["agent"] == "dridh/coordinator"
    assert "SESSION_STARTED" in events(workspace)


def test_unregistered_session_is_refused_without_calling_opencode(workspace) -> None:
    runner = FakeRunner()
    with pytest.raises(DridhError) as refused:
        asyncio.run(SessionService(workspace, actor="cli:test", runner=runner).resume("ses_unknown0000000000000001", "x"))
    assert refused.value.code is ErrorCode.SESSION_UNSAFE
    assert runner.calls == []
    assert "SESSION_REFUSED" in events(workspace)


def test_session_registered_to_another_agent_is_a_mismatch(workspace) -> None:
    register(workspace, "ses_worker00000000000001", agent="dridh/code-reacher")
    runner = FakeRunner()
    with pytest.raises(DridhError) as mismatch:
        asyncio.run(SessionService(workspace, actor="cli:test", runner=runner).resume("ses_worker00000000000001", "x"))
    assert mismatch.value.code is ErrorCode.SESSION_UNSAFE
    assert runner.calls == []


def test_inactive_session_is_refused(workspace) -> None:
    register(workspace, "ses_closed000000000000001", status="CLOSED")
    with pytest.raises(DridhError):
        asyncio.run(SessionService(workspace, actor="cli:test", runner=FakeRunner()).resume("ses_closed000000000000001", "x"))


def test_fresh_fallback_is_only_used_when_requested_and_is_recorded(workspace) -> None:
    runner = FakeRunner()
    result = asyncio.run(SessionService(workspace, actor="cli:test", runner=runner).resume(
        "ses_unknown0000000000000002", "PROBE_RUNTIME", fresh_if_unsafe=True
    ))
    assert result["mode"] == "fresh_fallback"
    assert runner.calls[0]["session_id"] is None
    assert "SESSION_FALLBACK_FRESH" in events(workspace)


def test_resume_that_does_not_preserve_the_id_is_refused_or_falls_back(workspace) -> None:
    register(workspace, "ses_known0000000000000001")
    failure = AgentRunError(AgentErrorCode.SESSION_RESUME_FAILED, "not preserved", session_id="ses_other")
    with pytest.raises(DridhError) as refused:
        asyncio.run(SessionService(workspace, actor="cli:test", runner=FakeRunner(error=failure)).resume("ses_known0000000000000001", "x"))
    assert refused.value.code is ErrorCode.SESSION_UNSAFE
    class ResumeOnlyFails(FakeRunner):
        async def run(self, *, agent, prompt, session_id=None, model=None, title=None):
            if session_id is not None:
                raise failure
            return await super().run(agent=agent, prompt=prompt, session_id=None, model=model, title=title)

    fallback_runner = ResumeOnlyFails()
    result = asyncio.run(SessionService(workspace, actor="cli:test", runner=fallback_runner).resume(
        "ses_known0000000000000001", "x", fresh_if_unsafe=True
    ))
    assert result["mode"] == "fresh_fallback"
    assert fallback_runner.calls[-1]["session_id"] is None


@pytest.mark.parametrize("code", [AgentErrorCode.TIMEOUT, AgentErrorCode.CANCELLED])
def test_timeouts_and_cancellations_never_fall_back_to_a_fresh_session(workspace, code) -> None:
    register(workspace, "ses_known0000000000000002")
    runner = FakeRunner(error=AgentRunError(code, "interrupted"))
    with pytest.raises(DridhError) as interrupted:
        asyncio.run(SessionService(workspace, actor="cli:test", runner=runner).resume(
            "ses_known0000000000000002", "x", fresh_if_unsafe=True
        ))
    assert interrupted.value.code in (ErrorCode.WORKER_TIMEOUT, ErrorCode.CANCELLED)
    assert len(runner.calls) == 1


def test_a_provider_refusal_keeps_its_diagnostic_and_says_what_to_do() -> None:
    from dridh.integrations.opencode import AgentErrorCode, AgentRunError
    from dridh.orchestration.engine import _map_runner_error

    error = _map_runner_error(AgentRunError(AgentErrorCode.QUOTA_EXHAUSTED, "Agent process reported a provider/quota failure.",
                                            details=("exit_code: 1", "provider_diagnostic: provider.free-limit\nFree usage limit reached.")))
    assert error.details == ("QUOTA_EXHAUSTED", "provider_diagnostic: provider.free-limit\nFree usage limit reached.")
    assert "new session" in error.recovery
