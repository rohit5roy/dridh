"""Execution run persistence (schema v12).

* ``execution_run``         - one confirmed run of a feature's approved plan. Status changes use an
                              optimistic revision check.
* ``execution_lot``         - a review checkpoint: the pathfinder story, a 25/50/100% threshold, the final
                              lot, or a starvation checkpoint. A lot is decided as a whole.
* ``execution_lot_story``   - the stories a lot covers.
* ``execution_lot_review``  - immutable verdicts (a lot may be reviewed again after rework).
* ``story_attempt``         - one implementation or repair attempt of a story, with its evidence.

Every change appends a hash-chained ``audit_event``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.reviews import _audit
from dridh.state.store import utc_now

RUN_STATUSES = ("RUNNING", "PAUSED", "COMPLETED", "STOPPED", "FAILED")
LOT_KINDS = ("PATHFINDER", "THRESHOLD", "FINAL", "STARVATION")
LOT_STATUSES = ("OPEN", "AWAITING_REVIEW", "APPROVED", "REWORK_REQUIRED")
VERDICTS = ("APPROVED", "REWORK_REQUIRED")
ATTEMPT_KINDS = ("IMPLEMENT", "REPAIR")
ATTEMPT_STATUSES = ("RUNNING", "PASSED", "FAILED")


def _q(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


RUN_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS execution_run (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  feature_id TEXT NOT NULL REFERENCES feature(id) ON DELETE RESTRICT,
  planner_session_id TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ({_q(RUN_STATUSES)})),
  max_parallel INTEGER NOT NULL CHECK(max_parallel BETWEEN 1 AND 5),
  max_repairs INTEGER NOT NULL CHECK(max_repairs BETWEEN 0 AND 3),
  confirmed_by TEXT NOT NULL,
  pause_reason TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS execution_run_feature ON execution_run(feature_id, created_at)",
    f"""CREATE TABLE IF NOT EXISTS execution_lot (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES execution_run(id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ({_q(LOT_KINDS)})),
  threshold_percent INTEGER CHECK(threshold_percent IS NULL OR threshold_percent BETWEEN 1 AND 100),
  status TEXT NOT NULL CHECK(status IN ({_q(LOT_STATUSES)})),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, ordinal)
)""",
    """CREATE TABLE IF NOT EXISTS execution_lot_story (
  lot_id TEXT NOT NULL REFERENCES execution_lot(id) ON DELETE RESTRICT,
  workspace_id TEXT NOT NULL,
  story_id TEXT NOT NULL,
  PRIMARY KEY(lot_id, story_id)
)""",
    f"""CREATE TABLE IF NOT EXISTS execution_lot_review (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  lot_id TEXT NOT NULL REFERENCES execution_lot(id) ON DELETE RESTRICT,
  verdict TEXT NOT NULL CHECK(verdict IN ({_q(VERDICTS)})),
  reviewer TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
)""",
    f"""CREATE TABLE IF NOT EXISTS story_attempt (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES execution_run(id) ON DELETE RESTRICT,
  story_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
  kind TEXT NOT NULL CHECK(kind IN ({_q(ATTEMPT_KINDS)})),
  status TEXT NOT NULL CHECK(status IN ({_q(ATTEMPT_STATUSES)})),
  commit_sha TEXT,
  evidence_json TEXT NOT NULL DEFAULT '{{}}',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id, story_id, attempt_no)
)""",
    "CREATE INDEX IF NOT EXISTS story_attempt_story ON story_attempt(run_id, story_id)",
    *(
        f"""CREATE TRIGGER IF NOT EXISTS {table}_workspace_guard BEFORE INSERT ON {table}
WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key='workspace_id')
BEGIN SELECT RAISE(ABORT,'{table} workspace mismatch'); END"""
        for table in ("execution_run", "execution_lot", "execution_lot_story", "execution_lot_review", "story_attempt")
    ),
    """CREATE TRIGGER IF NOT EXISTS execution_run_no_delete BEFORE DELETE ON execution_run
BEGIN SELECT RAISE(ABORT,'execution runs are retained'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_lot_review_no_update BEFORE UPDATE ON execution_lot_review
BEGIN SELECT RAISE(ABORT,'lot reviews are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_lot_review_no_delete BEFORE DELETE ON execution_lot_review
BEGIN SELECT RAISE(ABORT,'lot reviews are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS story_attempt_no_delete BEFORE DELETE ON story_attempt
BEGIN SELECT RAISE(ABORT,'story attempts are retained'); END""",
)


class RunRepository:
    """Reads and writes execution runs. Writes must run inside a transaction opened by the caller."""

    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def _tx(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Execution run writes require a transaction.")

    # ---------------------------------------------------------------- runs

    def create_run(self, *, feature_id: str, planner_session_id: str, plan_digest: str, max_parallel: int,
                   max_repairs: int, confirmed_by: str, actor: str) -> dict[str, Any]:
        self._tx()
        active = self.connection.execute(
            "SELECT id FROM execution_run WHERE workspace_id=? AND feature_id=? AND status IN ('RUNNING','PAUSED')",
            (self.workspace_id, feature_id),
        ).fetchone()
        if active:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "This feature already has an active execution run.", details=(active["id"],))
        run_id, now = str(uuid.uuid4()), utc_now()
        self.connection.execute(
            "INSERT INTO execution_run(id,workspace_id,feature_id,planner_session_id,plan_digest,status,max_parallel,max_repairs,confirmed_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'RUNNING',?,?,?,?,?)",
            (run_id, self.workspace_id, feature_id, planner_session_id, plan_digest, max_parallel, max_repairs, confirmed_by, now, now),
        )
        _audit(self.connection, self.workspace_id, actor, "execution.run_start", "execution_run", run_id,
               {"feature_id": feature_id, "plan_digest": plan_digest, "max_parallel": max_parallel})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM execution_run WHERE id=? AND workspace_id=?", (run_id, self.workspace_id)).fetchone()
        if row is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown execution run '{run_id}'.")
        return dict(row)

    def runs(self, feature_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM execution_run WHERE workspace_id=? AND feature_id=? ORDER BY created_at, rowid", (self.workspace_id, feature_id))
        return [dict(row) for row in rows]

    def set_run_status(self, run: dict[str, Any], status: str, *, reason: str | None, actor: str) -> dict[str, Any]:
        self._tx()
        if status not in RUN_STATUSES:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Invalid run status.", details=(status,))
        if run["status"] in ("COMPLETED", "STOPPED", "FAILED"):
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"The run is already {run['status']}.")
        updated = self.connection.execute(
            "UPDATE execution_run SET status=?, pause_reason=?, revision=revision+1, updated_at=? WHERE id=? AND workspace_id=? AND revision=?",
            (status, reason if status == "PAUSED" else None, utc_now(), run["id"], self.workspace_id, run["revision"]),
        )
        if updated.rowcount != 1:
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The run changed since it was read; nothing was applied.")
        _audit(self.connection, self.workspace_id, actor, f"execution.run_{status.lower()}", "execution_run", run["id"],
               {"from": run["status"], "reason": reason})
        return self.get_run(run["id"])

    # ---------------------------------------------------------------- lots

    def open_lot(self, *, run_id: str, kind: str, story_ids: list[str], threshold_percent: int | None, actor: str) -> dict[str, Any]:
        self._tx()
        if kind not in LOT_KINDS or not story_ids:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A lot needs a valid kind and at least one story.")
        ordinal = self.connection.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM execution_lot WHERE run_id=?", (run_id,)).fetchone()[0]
        lot_id, now = str(uuid.uuid4()), utc_now()
        self.connection.execute(
            "INSERT INTO execution_lot(id,workspace_id,run_id,ordinal,kind,threshold_percent,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'AWAITING_REVIEW',?,?)",
            (lot_id, self.workspace_id, run_id, ordinal, kind, threshold_percent, now, now),
        )
        for story_id in sorted(set(story_ids)):
            self.connection.execute("INSERT INTO execution_lot_story(lot_id,workspace_id,story_id) VALUES(?,?,?)", (lot_id, self.workspace_id, story_id))
        _audit(self.connection, self.workspace_id, actor, "execution.lot_open", "execution_lot", lot_id,
               {"run_id": run_id, "kind": kind, "stories": len(set(story_ids))})
        return self.get_lot(lot_id)

    def get_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM execution_lot WHERE id=? AND workspace_id=?", (lot_id, self.workspace_id)).fetchone()
        if row is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown lot '{lot_id}'.")
        lot = dict(row)
        lot["story_ids"] = [r["story_id"] for r in self.connection.execute(
            "SELECT story_id FROM execution_lot_story WHERE lot_id=? ORDER BY story_id", (lot_id,))]
        lot["reviews"] = [dict(r) for r in self.connection.execute(
            "SELECT verdict,reviewer,notes,created_at FROM execution_lot_review WHERE lot_id=? ORDER BY created_at, rowid", (lot_id,))]
        return lot

    def lots(self, run_id: str) -> list[dict[str, Any]]:
        ids = [r["id"] for r in self.connection.execute("SELECT id FROM execution_lot WHERE run_id=? ORDER BY ordinal", (run_id,))]
        return [self.get_lot(lot_id) for lot_id in ids]

    def review_lot(self, lot_id: str, *, verdict: str, reviewer: str, notes: str, actor: str) -> dict[str, Any]:
        """Record a whole-lot verdict. Only a lot awaiting review can be decided."""
        self._tx()
        if verdict not in VERDICTS or not reviewer.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A lot verdict needs APPROVED or REWORK_REQUIRED and a reviewer.")
        lot = self.get_lot(lot_id)
        if lot["status"] != "AWAITING_REVIEW":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"The lot is {lot['status']}, not awaiting review.")
        now = utc_now()
        self.connection.execute(
            "INSERT INTO execution_lot_review(id,workspace_id,lot_id,verdict,reviewer,notes,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.workspace_id, lot_id, verdict, reviewer.strip(), notes[:4000], now))
        self.connection.execute("UPDATE execution_lot SET status=?, updated_at=? WHERE id=?", (verdict, now, lot_id))
        _audit(self.connection, self.workspace_id, actor, f"execution.lot_{verdict.lower()}", "execution_lot", lot_id, {"reviewer": reviewer})
        return self.get_lot(lot_id)

    def reopen_lot(self, lot_id: str, *, actor: str) -> dict[str, Any]:
        """After rework, a REWORK_REQUIRED lot returns to review (its earlier verdicts stay on record)."""
        self._tx()
        lot = self.get_lot(lot_id)
        if lot["status"] != "REWORK_REQUIRED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"The lot is {lot['status']}, not awaiting rework.")
        self.connection.execute("UPDATE execution_lot SET status='AWAITING_REVIEW', updated_at=? WHERE id=?", (utc_now(), lot_id))
        _audit(self.connection, self.workspace_id, actor, "execution.lot_reopen", "execution_lot", lot_id, {})
        return self.get_lot(lot_id)

    # ------------------------------------------------------------ attempts

    def start_attempt(self, *, run_id: str, story_id: str, kind: str, actor: str) -> dict[str, Any]:
        self._tx()
        if kind not in ATTEMPT_KINDS:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Invalid attempt kind.", details=(kind,))
        number = self.connection.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 FROM story_attempt WHERE run_id=? AND story_id=?", (run_id, story_id)).fetchone()[0]
        attempt_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO story_attempt(id,workspace_id,run_id,story_id,attempt_no,kind,status,started_at) VALUES(?,?,?,?,?,?,'RUNNING',?)",
            (attempt_id, self.workspace_id, run_id, story_id, number, kind, utc_now()))
        _audit(self.connection, self.workspace_id, actor, "execution.attempt_start", "story_attempt", attempt_id,
               {"story_id": story_id, "attempt_no": number, "kind": kind})
        return self.get_attempt(attempt_id)

    def finish_attempt(self, attempt_id: str, *, status: str, commit_sha: str | None, evidence: dict[str, Any], actor: str) -> dict[str, Any]:
        self._tx()
        if status not in ("PASSED", "FAILED"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "An attempt finishes as PASSED or FAILED.")
        updated = self.connection.execute(
            "UPDATE story_attempt SET status=?, commit_sha=?, evidence_json=?, finished_at=? WHERE id=? AND workspace_id=? AND status='RUNNING'",
            (status, commit_sha, json.dumps(evidence, sort_keys=True, default=str), utc_now(), attempt_id, self.workspace_id))
        if updated.rowcount != 1:
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The attempt is not running.")
        _audit(self.connection, self.workspace_id, actor, "execution.attempt_finish", "story_attempt", attempt_id, {"status": status})
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM story_attempt WHERE id=? AND workspace_id=?", (attempt_id, self.workspace_id)).fetchone()
        if row is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown attempt '{attempt_id}'.")
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def rework_notes(self, run_id: str, story_id: str) -> list[str]:
        """The latest human rework request covering a story whose lot has not been approved since."""
        rows = self.connection.execute(
            "SELECT r.verdict, r.notes FROM execution_lot_review r JOIN execution_lot l ON l.id=r.lot_id "
            "JOIN execution_lot_story s ON s.lot_id=l.id WHERE l.run_id=? AND s.story_id=? ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1",
            (run_id, story_id)).fetchone()
        return [rows["notes"]] if rows and rows["verdict"] == "REWORK_REQUIRED" and rows["notes"].strip() else []

    def passing_commit(self, run_id: str, story_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT commit_sha FROM story_attempt WHERE run_id=? AND story_id=? AND status='PASSED' AND commit_sha IS NOT NULL "
            "ORDER BY attempt_no DESC LIMIT 1", (run_id, story_id)).fetchone()
        return row["commit_sha"] if row else None

    def attempts(self, run_id: str, story_id: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT id FROM story_attempt WHERE run_id=?", [run_id]
        if story_id:
            query, args = query + " AND story_id=?", [*args, story_id]
        ids = [r["id"] for r in self.connection.execute(query + " ORDER BY story_id, attempt_no", args)]
        return [self.get_attempt(attempt_id) for attempt_id in ids]


# Schema v13: story definitions stay immutable (replaced only by import) but their execution status may change.
STORY_STATUS_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS story_immutable_update",
    """CREATE TRIGGER IF NOT EXISTS story_definition_immutable_update
BEFORE UPDATE OF id, workspace_id, document_id, source_revision_id, story_id, title, component_id, phase_number, position ON story
BEGIN SELECT RAISE(ABORT, 'story definitions are replaced by import, not updated; only status may change'); END""",
)
