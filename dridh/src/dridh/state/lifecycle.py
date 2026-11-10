"""Feature lifecycle persistence (plan sections 5.2, 5.4 and 5.5).

Two tables:

* ``feature``             - one row per feature with its current lifecycle stage. Updated only through
                            ``record_transition`` / ``set_blocked`` / ``bind_session`` with an optimistic
                            revision check, so a stale agent cannot overwrite newer state.
* ``workflow_transition`` - append-only history of every move (and block/unblock), with the evidence that
                            justified it and an optional idempotency key so a replayed request does not
                            duplicate a transition.

Every change also appends a hash-chained ``audit_event`` (plan section 7, item 13).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.reviews import _audit
from dridh.state.store import utc_now

STAGES = (
    "INITIALISED",
    "CODEBASE_INDEXING",
    "IDEATION",
    "USE_CASE_REVIEW",
    "TECHNICAL_DISCOVERY",
    "TDD_REVIEW",
    "PLANNING",
    "PLAN_VALIDATION",
    "EXECUTION",
    "DELIVERY_REVIEW",
    "COMPLETED",
)
MODES = ("new", "existing")
TRANSITION_KINDS = ("CREATE", "ADVANCE", "REOPEN", "BLOCK", "UNBLOCK")
SESSION_COLUMNS = ("ideator_session_id", "designer_session_id", "planner_session_id")


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


LIFECYCLE_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS feature (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  name TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ({_quoted(MODES)})),
  stage TEXT NOT NULL CHECK(stage IN ({_quoted(STAGES)})),
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','BLOCKED')),
  blocked_reason TEXT,
  ideator_session_id TEXT,
  designer_session_id TEXT,
  planner_session_id TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id, slug)
)""",
    f"""CREATE TABLE IF NOT EXISTS workflow_transition (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  feature_id TEXT NOT NULL REFERENCES feature(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK(kind IN ({_quoted(TRANSITION_KINDS)})),
  from_stage TEXT NOT NULL CHECK(from_stage IN ({_quoted(STAGES)})),
  to_stage TEXT NOT NULL CHECK(to_stage IN ({_quoted(STAGES)})),
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{{}}',
  actor TEXT NOT NULL,
  idempotency_key TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(feature_id, idempotency_key)
)""",
    "CREATE INDEX IF NOT EXISTS workflow_transition_feature ON workflow_transition(feature_id, created_at)",
    """CREATE TRIGGER IF NOT EXISTS feature_workspace_guard BEFORE INSERT ON feature
WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key='workspace_id')
BEGIN SELECT RAISE(ABORT,'feature workspace mismatch'); END""",
    """CREATE TRIGGER IF NOT EXISTS feature_no_delete BEFORE DELETE ON feature
BEGIN SELECT RAISE(ABORT,'features are retained'); END""",
    """CREATE TRIGGER IF NOT EXISTS workflow_transition_workspace_guard BEFORE INSERT ON workflow_transition
WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key='workspace_id')
BEGIN SELECT RAISE(ABORT,'transition workspace mismatch'); END""",
    """CREATE TRIGGER IF NOT EXISTS workflow_transition_no_update BEFORE UPDATE ON workflow_transition
BEGIN SELECT RAISE(ABORT,'transitions are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS workflow_transition_no_delete BEFORE DELETE ON workflow_transition
BEGIN SELECT RAISE(ABORT,'transitions are append-only'); END""",
)


class LifecycleRepository:
    """Reads and writes lifecycle rows. Writes must run inside a transaction opened by the caller."""

    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Lifecycle writes require a transaction.")

    # ---------------------------------------------------------------- reads

    def get(self, reference: str) -> dict[str, Any]:
        """A feature by id or slug."""
        row = self.connection.execute(
            "SELECT * FROM feature WHERE workspace_id=? AND (id=? OR slug=?)", (self.workspace_id, reference, reference)
        ).fetchone()
        if row is None:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                f"Unknown feature '{reference}'.",
                recovery="List features with `dridh feature list`, or create one with `dridh feature create`.",
            )
        return dict(row)

    def list(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM feature WHERE workspace_id=? ORDER BY created_at, slug", (self.workspace_id,))
        return [dict(row) for row in rows]

    def transitions(self, feature_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM workflow_transition WHERE workspace_id=? AND feature_id=? ORDER BY created_at, rowid",
            (self.workspace_id, feature_id),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    def transition_by_key(self, feature_id: str, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM workflow_transition WHERE feature_id=? AND idempotency_key=?", (feature_id, key)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    # --------------------------------------------------------------- writes

    def create(self, *, slug: str, name: str, mode: str, actor: str) -> dict[str, Any]:
        self._require_transaction()
        if mode not in MODES:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"mode must be one of: {', '.join(MODES)}.", details=(str(mode),))
        if self.connection.execute("SELECT 1 FROM feature WHERE workspace_id=? AND slug=?", (self.workspace_id, slug)).fetchone():
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, f"A feature named '{slug}' already exists in this workspace.")
        feature_id, now = str(uuid.uuid4()), utc_now()
        self.connection.execute(
            "INSERT INTO feature(id,workspace_id,slug,name,mode,stage,created_at,updated_at) VALUES(?,?,?,?,?,'INITIALISED',?,?)",
            (feature_id, self.workspace_id, slug, name, mode, now, now),
        )
        self._append(feature_id, "CREATE", "INITIALISED", "INITIALISED", "Feature created.", {"mode": mode}, actor, None)
        _audit(self.connection, self.workspace_id, actor, "feature.create", "feature", feature_id, {"slug": slug, "mode": mode})
        return self.get(feature_id)

    def record_transition(
        self, feature: dict[str, Any], *, to_stage: str, kind: str, reason: str, evidence: dict[str, Any],
        actor: str, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move a feature to ``to_stage`` if its revision is still the one the caller read."""
        self._require_transaction()
        if to_stage not in STAGES or kind not in ("ADVANCE", "REOPEN"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Invalid lifecycle transition.", details=(kind, to_stage))
        updated = self.connection.execute(
            "UPDATE feature SET stage=?, revision=revision+1, updated_at=? WHERE id=? AND workspace_id=? AND revision=?",
            (to_stage, utc_now(), feature["id"], self.workspace_id, feature["revision"]),
        )
        if updated.rowcount != 1:
            raise DridhError(
                ErrorCode.INVALID_TRANSITION,
                "The feature changed since it was read; the transition was not applied.",
                recovery="Read the feature's current status and retry.",
            )
        self._append(feature["id"], kind, feature["stage"], to_stage, reason, evidence, actor, idempotency_key)
        _audit(self.connection, self.workspace_id, actor, f"feature.{kind.lower()}", "feature", feature["id"],
               {"from": feature["stage"], "to": to_stage, "reason": reason})
        return self.get(feature["id"])

    def set_blocked(self, feature: dict[str, Any], *, blocked: bool, reason: str, actor: str) -> dict[str, Any]:
        self._require_transaction()
        updated = self.connection.execute(
            "UPDATE feature SET status=?, blocked_reason=?, revision=revision+1, updated_at=? WHERE id=? AND workspace_id=? AND revision=?",
            ("BLOCKED" if blocked else "ACTIVE", reason if blocked else None, utc_now(), feature["id"], self.workspace_id, feature["revision"]),
        )
        if updated.rowcount != 1:
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The feature changed since it was read; nothing was applied.")
        kind = "BLOCK" if blocked else "UNBLOCK"
        self._append(feature["id"], kind, feature["stage"], feature["stage"], reason, {}, actor, None)
        _audit(self.connection, self.workspace_id, actor, f"feature.{kind.lower()}", "feature", feature["id"], {"stage": feature["stage"], "reason": reason})
        return self.get(feature["id"])

    def bind_session(self, feature: dict[str, Any], column: str, session_id: str, *, actor: str) -> dict[str, Any]:
        self._require_transaction()
        if column not in SESSION_COLUMNS:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Unknown session column.", details=(column,))
        current = feature.get(column)
        if current and current != session_id:
            raise DridhError(
                ErrorCode.CONFIGURATION_CONFLICT,
                f"This feature is already bound to {column.split('_')[0]} session {current}.",
                recovery="A feature has one session per stage; start a new feature for a different one.",
            )
        if current == session_id:
            return feature
        self.connection.execute(
            f"UPDATE feature SET {column}=?, revision=revision+1, updated_at=? WHERE id=? AND workspace_id=? AND revision=?",  # noqa: S608 - column is allow-listed
            (session_id, utc_now(), feature["id"], self.workspace_id, feature["revision"]),
        )
        _audit(self.connection, self.workspace_id, actor, "feature.bind_session", "feature", feature["id"], {column: session_id})
        return self.get(feature["id"])

    def _append(
        self, feature_id: str, kind: str, from_stage: str, to_stage: str, reason: str,
        evidence: dict[str, Any], actor: str, idempotency_key: str | None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO workflow_transition(id,workspace_id,feature_id,kind,from_stage,to_stage,reason,evidence_json,actor,idempotency_key,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.workspace_id, feature_id, kind, from_stage, to_stage, reason[:2000],
             json.dumps(evidence, sort_keys=True, default=str), actor, idempotency_key, utc_now()),
        )
