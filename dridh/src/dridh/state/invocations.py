"""Append-only record of bridge tool invocations (plan 5.2 ``tool_invocation``; schema v14).

Stores who called which operation, whether it succeeded, how long it took and a SHA-256 of the canonical payload.
The payload itself is never stored, so secrets and document text cannot leak into the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from dridh.state.store import utc_now

INVOCATION_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS tool_invocation (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  caller_source TEXT,
  caller_agent TEXT,
  caller_session TEXT,
  status TEXT NOT NULL CHECK(status IN ('OK','FAILED')),
  error_code TEXT,
  payload_sha256 TEXT NOT NULL,
  payload_keys TEXT NOT NULL DEFAULT '[]',
  duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
  created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS tool_invocation_time ON tool_invocation(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS tool_invocation_operation ON tool_invocation(workspace_id, operation, status)",
    """CREATE TRIGGER IF NOT EXISTS tool_invocation_workspace_guard BEFORE INSERT ON tool_invocation
WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key='workspace_id')
BEGIN SELECT RAISE(ABORT,'tool invocation workspace mismatch'); END""",
    """CREATE TRIGGER IF NOT EXISTS tool_invocation_no_update BEFORE UPDATE ON tool_invocation
BEGIN SELECT RAISE(ABORT,'tool invocations are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS tool_invocation_no_delete BEFORE DELETE ON tool_invocation
BEGIN SELECT RAISE(ABORT,'tool invocations are append-only'); END""",
)


def payload_digest(payload: Any) -> tuple[str, list[str]]:
    body = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    keys = sorted(payload) if isinstance(payload, dict) else []
    return hashlib.sha256(body.encode("utf-8")).hexdigest(), keys[:30]


class InvocationRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection, self.workspace_id = connection, workspace_id

    def record(self, *, request_id: str, operation: str, caller: Any, payload: Any, ok: bool,
               error_code: str | None, started: float) -> str:
        digest, keys = payload_digest(payload)
        invocation_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO tool_invocation(id,workspace_id,request_id,operation,caller_source,caller_agent,caller_session,status,error_code,"
            "payload_sha256,payload_keys,duration_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (invocation_id, self.workspace_id, request_id, operation, getattr(caller, "source", None), getattr(caller, "agent", None),
             getattr(caller, "session_id", None), "OK" if ok else "FAILED", error_code, digest, json.dumps(keys),
             max(0, int((time.monotonic() - started) * 1000)), utc_now()))
        return invocation_id
