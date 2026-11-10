"""Coordinator sessions through OpenCode: start, resume and a safe fresh fallback.

Every session that Dridh starts or resumes is registered in the workspace state
and recorded in the ledger. A resume is refused unless the stored session belongs
to the Coordinator in this workspace. A fresh one-shot session is started only
when the caller asks for it, and that fallback is recorded. Timeouts and
cancellations never fall back, because a fresh run would hide the interruption.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from dridh.common.errors import DridhError, ErrorCode
from dridh.integrations.opencode import AgentErrorCode, AgentRunError, AgentRunResult, OpenCodeRunner
from dridh.orchestration.engine import _map_runner_error
from dridh.orchestration.roster import COORDINATOR
from dridh.state.store import StateStore, append_ledger, utc_now
from dridh.workspace.layout import WorkspaceInfo

DEFAULT_SESSION_TIMEOUT = 1800.0
SESSION_TITLE = "Dridh Coordinator session"


class SessionRunner(Protocol):
    async def run(
        self,
        *,
        agent: str,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        title: str | None = None,
    ) -> AgentRunResult: ...


class SessionService:
    def __init__(
        self,
        workspace: WorkspaceInfo,
        *,
        actor: str,
        runner: SessionRunner | None = None,
        timeout: float | None = None,
    ) -> None:
        self.workspace = workspace
        self.actor = actor
        self._runner = runner
        self.timeout = timeout or float(os.environ.get("DRIDH_SESSION_TIMEOUT_SECONDS", DEFAULT_SESSION_TIMEOUT))

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    def _runner_or_default(self) -> SessionRunner:
        if self._runner is None:
            self._runner = OpenCodeRunner(workspace_root=self.workspace.project_root, timeout=self.timeout)
        return self._runner

    async def _run(self, message: str, session_id: str | None) -> AgentRunResult:
        return await self._runner_or_default().run(
            agent=COORDINATOR,
            prompt=message,
            session_id=session_id,
            title=SESSION_TITLE,
        )

    def _lookup(self, session_id: str) -> dict[str, Any] | None:
        with self._store() as store:
            row = store.read().execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

    def _record(self, conn: Any, session_id: str, event: str, payload: dict[str, Any], workflow_id: str | None = None) -> None:
        now = utc_now()
        conn.execute(
            """INSERT INTO sessions(session_id, agent, workflow_id, purpose, status, created_at, last_used_at)
               VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET last_used_at = excluded.last_used_at""",
            (session_id, COORDINATOR, workflow_id, event, now, now),
        )
        append_ledger(conn, workflow_id, event, self.actor, {"session_id": session_id, **payload})

    def _refuse(self, session_id: str, reason: str) -> None:
        with self._store() as store:
            with store.transaction() as conn:
                append_ledger(conn, None, "SESSION_REFUSED", self.actor, {"session_id": session_id, "reason": reason})
        raise DridhError(
            ErrorCode.SESSION_UNSAFE,
            "The session cannot safely be resumed in this workspace.",
            details=(f"session: {session_id}", f"reason: {reason}"),
        )

    def _summary(self, result: AgentRunResult, *, mode: str, requested: str | None = None) -> dict[str, Any]:
        return {
            "mode": mode,
            "session_id": result.session_id,
            "requested_session_id": requested,
            "agent": COORDINATOR,
            "resumed": result.resumed,
            "exit_code": result.exit_code,
            "duration_seconds": round(result.duration_seconds, 3),
            "text": result.text,
        }

    async def start(self, message: str) -> dict[str, Any]:
        result = await self._guarded(self._run(message, session_id=None))
        with self._store() as store:
            with store.transaction() as conn:
                self._record(conn, result.session_id, "SESSION_STARTED", {"agent": COORDINATOR})
        return self._summary(result, mode="started")

    async def resume(self, session_id: str, message: str, *, fresh_if_unsafe: bool = False) -> dict[str, Any]:
        row = self._lookup(session_id)
        reason: str | None = None
        if row is None:
            reason = "NOT_REGISTERED"
        elif row["agent"] != COORDINATOR:
            reason = "AGENT_MISMATCH"
        elif row["status"] != "ACTIVE":
            reason = "NOT_ACTIVE"
        if reason is not None:
            if fresh_if_unsafe:
                return await self._fresh_fallback(session_id, reason, message)
            self._refuse(session_id, reason)

        try:
            result = await self._run(message, session_id=session_id)
        except AgentRunError as exc:
            if exc.code is AgentErrorCode.SESSION_RESUME_FAILED:
                if fresh_if_unsafe:
                    return await self._fresh_fallback(session_id, "RESUME_NOT_PRESERVED", message)
                self._refuse(session_id, "RESUME_NOT_PRESERVED")
            raise _map_runner_error(exc) from None
        with self._store() as store:
            with store.transaction() as conn:
                self._record(conn, result.session_id, "SESSION_RESUMED", {"requested": session_id})
        return self._summary(result, mode="resumed", requested=session_id)

    async def _guarded(self, pending: Any) -> AgentRunResult:
        try:
            return await pending
        except AgentRunError as exc:
            raise _map_runner_error(exc) from None

    async def _fresh_fallback(self, requested: str, reason: str, message: str) -> dict[str, Any]:
        result = await self._guarded(self._run(message, session_id=None))
        with self._store() as store:
            with store.transaction() as conn:
                self._record(
                    conn,
                    result.session_id,
                    "SESSION_FALLBACK_FRESH",
                    {"requested_session_id": requested, "reason": reason},
                )
        return self._summary(result, mode="fresh_fallback", requested=requested)
