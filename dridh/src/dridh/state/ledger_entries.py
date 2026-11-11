"""Ledger entries as queryable rows (schema v15).

``ledger_entry`` holds one row per Table 14 entry of the imported LEDGER.md revision. Rows are replaced by import, never edited
in place, and every row carries the SHA-256 of its Markdown (row plus definition block). ``ledger_import`` records which document
revision the rows came from and which approved Use Case revision existed at the time, so drift and tampering are detectable.
Nothing here writes to a Use Case table: a ledger entry can never change the approved original requirement.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import utc_now

LEDGER_STATUSES = ("DONE", "DEVIATED", "NOT_DONE", "WONT_DO")

LEDGER_ENTRY_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS ledger_entry (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  ledger_id TEXT NOT NULL CHECK (ledger_id GLOB 'LDG-[0-9][0-9]*'),
  title TEXT NOT NULL,
  origin_id TEXT NOT NULL,
  deviation_status TEXT NOT NULL CHECK (deviation_status IN ({",".join(repr(s) for s in LEDGER_STATUSES)})),
  linked_story_id TEXT,
  original_requirement TEXT NOT NULL,
  actual_behavior TEXT NOT NULL,
  justification TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  entry_sha256 TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 1),
  imported_at TEXT NOT NULL,
  UNIQUE (workspace_id, ledger_id)
)""",
    """CREATE TABLE IF NOT EXISTS ledger_import (
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  document_sha256 TEXT NOT NULL,
  use_case_revision_id TEXT,
  entry_count INTEGER NOT NULL,
  imported_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, document_id)
)""",
    "CREATE INDEX IF NOT EXISTS ledger_entry_origin ON ledger_entry(workspace_id, origin_id)",
    """CREATE TRIGGER IF NOT EXISTS ledger_entry_workspace_guard BEFORE INSERT ON ledger_entry
WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key='workspace_id')
BEGIN SELECT RAISE(ABORT,'ledger entry workspace mismatch'); END""",
    """CREATE TRIGGER IF NOT EXISTS ledger_entry_immutable_update BEFORE UPDATE ON ledger_entry
BEGIN SELECT RAISE(ABORT,'ledger entries are replaced by import, not updated'); END""",
)


class LedgerEntryRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection, self.workspace_id = connection, workspace_id

    def replace(self, *, document_id: str, revision_id: str, document_sha256: str, use_case_revision_id: str | None,
                entries: list[dict[str, Any]]) -> int:
        """Replace every entry with the parsed rows of one document revision, in one transaction."""
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Ledger imports require a transaction.")
        now = utc_now()
        self.connection.execute("DELETE FROM ledger_entry WHERE workspace_id=?", (self.workspace_id,))
        for position, entry in enumerate(entries, start=1):
            self.connection.execute(
                "INSERT INTO ledger_entry(id,workspace_id,document_id,source_revision_id,ledger_id,title,origin_id,deviation_status,linked_story_id,"
                "original_requirement,actual_behavior,justification,approved_by,entry_sha256,position,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, entry["ledger_id"], entry["title"], entry["origin_id"],
                 entry["deviation_status"], entry["linked_story_id"], entry["original_requirement"], entry["actual_behavior"],
                 entry["justification"], entry["approved_by"], entry["entry_sha256"], position, now))
        self.connection.execute("DELETE FROM ledger_import WHERE workspace_id=?", (self.workspace_id,))
        self.connection.execute(
            "INSERT INTO ledger_import(workspace_id,document_id,source_revision_id,document_sha256,use_case_revision_id,entry_count,imported_at) VALUES(?,?,?,?,?,?,?)",
            (self.workspace_id, document_id, revision_id, document_sha256, use_case_revision_id, len(entries), now))
        return len(entries)

    def entries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ledger_entry WHERE workspace_id=? ORDER BY position", (self.workspace_id,))
        return [dict(row) for row in rows]

    def marker(self) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM ledger_import WHERE workspace_id=?", (self.workspace_id,)).fetchone()
        return dict(row) if row else None
