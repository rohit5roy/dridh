"""Workspace schema migration: v1 -> v2.

Phase 2 shipped schema version 1 with no automatic downgrade. Phase 3 needs
new tables for parent/subtask relationships, dependency edges, worker attempts,
worktree references, scheduling decisions, checkpoints, QA results and
integration status. This module performs an explicit, idempotent, transactional
upgrade from v1 to v2 with a database backup and hash-chain preservation.

The migration never regenerates or rewrites existing Ledger rows. It only adds
tables and columns, so the hash chain remains byte-for-byte identical.
"""
# ruff: noqa: I001 — compact SQL persistence declarations and focused integration fixtures

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import (
    _DECISION_TABLE_V6,
    _DECISION_TRIGGERS,
    _DESIGN_V5_STATEMENTS,
    _DOCUMENT_STATEMENTS,
    _MAPPING_INDEX_AND_TRIGGERS,
    _MAPPING_TABLE_V6,
    _PLANNING_STATEMENTS,
    _USECASE_STATEMENTS,
    _EXECUTION_STATEMENTS,
    utc_now,
)

PHASE2_SCHEMA_VERSION = 1
PHASE3_SCHEMA_VERSION = 2
PHASE5_SCHEMA_VERSION = 3
USECASE_SCHEMA_VERSION = 4
DESIGN_SCHEMA_VERSION = 5
CDD_SCOPE_SCHEMA_VERSION = 6
PLANNING_SCHEMA_VERSION = 7
REVIEW_SCHEMA_VERSION = 8
PHASE5_EXECUTION_SCHEMA_VERSION = 9
PHASE5_COMPLETE_SCHEMA_VERSION = 10
LIFECYCLE_SCHEMA_VERSION = 11  # feature + workflow_transition (stage lifecycle)
RUNS_SCHEMA_VERSION = 12  # execution_run, execution_lot*, story_attempt
STORY_STATUS_SCHEMA_VERSION = 13  # story status may change; definitions stay immutable
INVOCATION_SCHEMA_VERSION = 14  # tool_invocation audit table
LEDGER_SCHEMA_VERSION = 15  # ledger_entry + ledger_import
CURRENT_SCHEMA_VERSION = LEDGER_SCHEMA_VERSION

_V2_ADDITIONS = """
CREATE TABLE IF NOT EXISTS task_dependencies (
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (workflow_id, task_id, depends_on)
);
CREATE TABLE IF NOT EXISTS subtasks (
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  parent_task_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  PRIMARY KEY (workflow_id, parent_task_id, task_id)
);
CREATE TABLE IF NOT EXISTS worker_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  session_id TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  exit_code INTEGER,
  error_code TEXT,
  lease_token TEXT,
  worktree_id TEXT
);
CREATE TABLE IF NOT EXISTS worktrees (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  path TEXT NOT NULL,
  branch TEXT,
  commit_sha TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  cleaned_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduling_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  decision_type TEXT NOT NULL,
  task_id TEXT,
  worker_id TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT,
  checkpoint_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  verdict TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clarifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  answer TEXT,
  source TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL,
  answered_at TEXT
);
CREATE TABLE IF NOT EXISTS integration_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  branch TEXT NOT NULL,
  status TEXT NOT NULL,
  commit_sha TEXT,
  conflict_detail TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _backup_database(db_path: Path, label: str = "v1") -> Path:
    stamp = utc_now().replace(":", "").replace("-", "")
    backup = db_path.with_name(f"{db_path.name}.{label}-backup-{stamp}")
    shutil.copy2(db_path, backup)
    # Also back up the WAL and SHM if present.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(backup) + suffix))
    return backup


def migrate_v1_to_v2(db_path: Path) -> dict[str, Any]:
    """Migrate a v1 workspace database to v2. Idempotent.

    Returns a dict with keys: from_version, to_version, backup_path, migrated.
    Raises DridhError on any failure; the database is left untouched on error
    because all DDL runs inside one immediate transaction.
    """

    if not db_path.exists():
        raise DridhError(ErrorCode.STATE_CORRUPT, "State database does not exist.", details=(str(db_path),))

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        current = _current_schema_version(conn)
        if current in (PHASE3_SCHEMA_VERSION, PHASE5_SCHEMA_VERSION, USECASE_SCHEMA_VERSION, DESIGN_SCHEMA_VERSION, CDD_SCOPE_SCHEMA_VERSION, PLANNING_SCHEMA_VERSION, REVIEW_SCHEMA_VERSION, PHASE5_EXECUTION_SCHEMA_VERSION, PHASE5_COMPLETE_SCHEMA_VERSION, LIFECYCLE_SCHEMA_VERSION, RUNS_SCHEMA_VERSION, STORY_STATUS_SCHEMA_VERSION, INVOCATION_SCHEMA_VERSION, LEDGER_SCHEMA_VERSION):
            return {"from_version": current, "to_version": current, "backup_path": None, "migrated": False}
        if current != PHASE2_SCHEMA_VERSION:
            raise DridhError(
                ErrorCode.SCHEMA_UNSUPPORTED,
                f"Unsupported schema version {current}; expected {PHASE2_SCHEMA_VERSION}, {PHASE3_SCHEMA_VERSION} or {REVIEW_SCHEMA_VERSION}.",
                details=(f"current: {current}",),
            )

        # Verify integrity before touching anything.
        check = conn.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise DridhError(ErrorCode.STATE_CORRUPT, "Database failed integrity check before migration.", details=(str(check),))

        backup_path = _backup_database(db_path)

        try:
            conn.execute("BEGIN IMMEDIATE")
            # Add parent_task_id column if missing (v1 -> v2).
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists.
            # Add v2 tables one statement at a time (executescript commits implicitly).
            for statement in _V2_ADDITIONS.strip().split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)
            # Update schema version.
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(PHASE3_SCHEMA_VERSION),))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "Migration failed; database rolled back. Restore from backup if needed.",
                details=(str(exc), str(backup_path)),
            ) from None

        return {
            "from_version": PHASE2_SCHEMA_VERSION,
            "to_version": PHASE3_SCHEMA_VERSION,
            "backup_path": str(backup_path),
            "migrated": True,
        }
    finally:
        conn.close()


def schema_version(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError, TypeError):
        return 0
    finally:
        conn.close()


def _apply_statements(db_path: Path, *, expected: int, target: int, statements: tuple[str, ...], label: str) -> dict[str, Any]:
    """Run additive DDL for one schema step. Idempotent, backed up, all-or-nothing.

    Existing rows, the ledger hash chain and earlier schema objects are not modified.
    """

    if not db_path.exists():
        raise DridhError(ErrorCode.STATE_CORRUPT, "State database does not exist.", details=(str(db_path),))
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        current = _current_schema_version(conn)
        if current == target:
            return {"from_version": current, "to_version": current, "backup_path": None, "migrated": False}
        if current != expected:
            raise DridhError(
                ErrorCode.SCHEMA_UNSUPPORTED,
                f"Unsupported schema version {current}; expected {expected} or {target}.",
                details=(f"current: {current}",),
            )
        check = conn.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise DridhError(ErrorCode.STATE_CORRUPT, "Database failed integrity check before migration.", details=(str(check),))
        backup_path = _backup_database(db_path, label=label)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(target),))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "Migration failed; database rolled back. Restore from backup if needed.",
                details=(str(exc), str(backup_path)),
            ) from None
        return {"from_version": expected, "to_version": target, "backup_path": str(backup_path), "migrated": True}
    finally:
        conn.close()


def migrate_v2_to_v3(db_path: Path) -> dict[str, Any]:
    """Add the document persistence tables (Phase 5 increment 0)."""

    return _apply_statements(db_path, expected=PHASE3_SCHEMA_VERSION, target=PHASE5_SCHEMA_VERSION, statements=_DOCUMENT_STATEMENTS, label="v2")


def migrate_v3_to_v4(db_path: Path) -> dict[str, Any]:
    """Add the use-case domain: actors, use cases and flows (Phase 5 increment 1)."""

    return _apply_statements(db_path, expected=PHASE5_SCHEMA_VERSION, target=USECASE_SCHEMA_VERSION, statements=_USECASE_STATEMENTS, label="v3")


def migrate_v4_to_v5(db_path: Path) -> dict[str, Any]:
    """Add the design domain: components, design decisions, traceability and API/event rows (increment 2)."""

    return _apply_statements(db_path, expected=USECASE_SCHEMA_VERSION, target=DESIGN_SCHEMA_VERSION, statements=_DESIGN_V5_STATEMENTS, label="v4")


def _renamed(ddl: str, old: str, new: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {old} ("
    assert ddl.startswith(prefix), ddl[:60]
    return f"CREATE TABLE {new} (" + ddl[len(prefix) :]


def migrate_v5_to_v6(db_path: Path) -> dict[str, Any]:
    """Correct CDD-N uniqueness to per-component scope (TABLE-STANDARDS 2 and 13.1).

    v5 made decision_id unique per workspace, so two components could not both define CDD-1.
    SQLite cannot drop a table constraint, so design_decision and its mapping table are rebuilt:
    every row is copied, row counts and foreign keys are verified, and the whole step is one
    transaction after a backup. Persisted decisions and mappings are preserved unchanged.
    """

    if not db_path.exists():
        raise DridhError(ErrorCode.STATE_CORRUPT, "State database does not exist.", details=(str(db_path),))
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        current = _current_schema_version(conn)
        if current == CDD_SCOPE_SCHEMA_VERSION:
            return {"from_version": current, "to_version": current, "backup_path": None, "migrated": False}
        if current != DESIGN_SCHEMA_VERSION:
            raise DridhError(ErrorCode.SCHEMA_UNSUPPORTED, f"Unsupported schema version {current}; expected {DESIGN_SCHEMA_VERSION} or {CDD_SCOPE_SCHEMA_VERSION}.", details=(f"current: {current}",))
        check = conn.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise DridhError(ErrorCode.STATE_CORRUPT, "Database failed integrity check before migration.", details=(str(check),))
        backup_path = _backup_database(db_path, label="v5")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            decisions_before = conn.execute("SELECT COUNT(*) FROM design_decision").fetchone()[0]
            mappings_before = conn.execute("SELECT COUNT(*) FROM use_case_component_mapping").fetchone()[0]
            conn.execute(_renamed(_DECISION_TABLE_V6, "design_decision", "design_decision_v6"))
            conn.execute(
                """INSERT INTO design_decision_v6(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position)
                   SELECT id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position FROM design_decision"""
            )
            conn.execute(_renamed(_MAPPING_TABLE_V6, "use_case_component_mapping", "use_case_component_mapping_v6"))
            conn.execute(
                """INSERT INTO use_case_component_mapping_v6(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, design_decision_id, position)
                   SELECT id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, design_decision_id, position FROM use_case_component_mapping"""
            )
            conn.execute("DROP TABLE use_case_component_mapping")
            conn.execute("DROP TABLE design_decision")
            conn.execute("ALTER TABLE design_decision_v6 RENAME TO design_decision")
            conn.execute("ALTER TABLE use_case_component_mapping_v6 RENAME TO use_case_component_mapping")
            for statement in (*_DECISION_TRIGGERS, *_MAPPING_INDEX_AND_TRIGGERS):
                conn.execute(statement)
            decisions_after = conn.execute("SELECT COUNT(*) FROM design_decision").fetchone()[0]
            mappings_after = conn.execute("SELECT COUNT(*) FROM use_case_component_mapping").fetchone()[0]
            if (decisions_before, mappings_before) != (decisions_after, mappings_after):
                raise RuntimeError(f"row counts changed: decisions {decisions_before}->{decisions_after}, mappings {mappings_before}->{mappings_after}")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"foreign key violations after rebuild: {len(violations)}")
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(CDD_SCOPE_SCHEMA_VERSION),))
            conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "Migration to CDD-N scope failed; the database was rolled back. The backup is retained.",
                details=(str(exc), str(backup_path)),
            ) from None
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        return {
            "from_version": DESIGN_SCHEMA_VERSION,
            "to_version": CDD_SCOPE_SCHEMA_VERSION,
            "backup_path": str(backup_path),
            "migrated": True,
            "rows_preserved": {"design_decision": decisions_before, "use_case_component_mapping": mappings_before},
        }
    finally:
        conn.close()


def migrate_v6_to_v7(db_path: Path) -> dict[str, Any]:
    """Add the planning domain: stories, dependency DAG, parallel groups, component phases, milestones,
    risks, test phases and evidence (Phase 5 increment 3). Additive only: no existing row or constraint changes."""

    return _apply_statements(db_path, expected=CDD_SCOPE_SCHEMA_VERSION, target=PLANNING_SCHEMA_VERSION, statements=_PLANNING_STATEMENTS, label="v6")


def migrate_v7_to_v8(db_path: Path) -> dict[str, Any]:
    from dridh.state.reviews import REVIEW_STATEMENTS
    return _apply_statements(db_path, expected=PLANNING_SCHEMA_VERSION, target=REVIEW_SCHEMA_VERSION, statements=REVIEW_STATEMENTS, label="v7")


def migrate_to_current(db_path: Path) -> dict[str, Any]:
    """Apply every pending migration in order, each with its own backup."""

    steps: list[dict[str, Any]] = []
    version = schema_version(db_path)
    if version == PHASE2_SCHEMA_VERSION:
        steps.append(migrate_v1_to_v2(db_path))
        version = schema_version(db_path)
    if version == PHASE3_SCHEMA_VERSION:
        steps.append(migrate_v2_to_v3(db_path))
        version = schema_version(db_path)
    if version == PHASE5_SCHEMA_VERSION:
        steps.append(migrate_v3_to_v4(db_path))
        version = schema_version(db_path)
    if version == USECASE_SCHEMA_VERSION:
        steps.append(migrate_v4_to_v5(db_path))
        version = schema_version(db_path)
    if version == DESIGN_SCHEMA_VERSION:
        steps.append(migrate_v5_to_v6(db_path))
        version = schema_version(db_path)
    if version == CDD_SCOPE_SCHEMA_VERSION:
        steps.append(migrate_v6_to_v7(db_path))
        version = schema_version(db_path)
    if version == PLANNING_SCHEMA_VERSION:
        steps.append(migrate_v7_to_v8(db_path))
        version = schema_version(db_path)
    if version == REVIEW_SCHEMA_VERSION:
        steps.append(_apply_statements(db_path, expected=8, target=9, statements=_EXECUTION_STATEMENTS[:10], label='v8-execution'))
        version = schema_version(db_path)
    if version == PHASE5_EXECUTION_SCHEMA_VERSION:
        steps.append(_apply_statements(db_path, expected=9, target=10, statements=_EXECUTION_STATEMENTS[10:], label='v9-code-jev'))
        version = schema_version(db_path)
    if version == PHASE5_COMPLETE_SCHEMA_VERSION:
        from dridh.state.lifecycle import LIFECYCLE_STATEMENTS
        steps.append(_apply_statements(db_path, expected=10, target=11, statements=LIFECYCLE_STATEMENTS, label="v10-lifecycle"))
        version = schema_version(db_path)
    if version == LIFECYCLE_SCHEMA_VERSION:
        from dridh.state.runs import RUN_STATEMENTS
        steps.append(_apply_statements(db_path, expected=11, target=12, statements=RUN_STATEMENTS, label="v11-execution-runs"))
        version = schema_version(db_path)
    if version == RUNS_SCHEMA_VERSION:
        from dridh.state.runs import STORY_STATUS_STATEMENTS
        steps.append(_apply_statements(db_path, expected=12, target=13, statements=STORY_STATUS_STATEMENTS, label="v12-story-status"))
        version = schema_version(db_path)
    if version == STORY_STATUS_SCHEMA_VERSION:
        from dridh.state.invocations import INVOCATION_STATEMENTS
        steps.append(_apply_statements(db_path, expected=13, target=14, statements=INVOCATION_STATEMENTS, label="v13-tool-invocation"))
        version = schema_version(db_path)
    if version == INVOCATION_SCHEMA_VERSION:
        from dridh.state.ledger_entries import LEDGER_ENTRY_STATEMENTS
        steps.append(_apply_statements(db_path, expected=14, target=15, statements=LEDGER_ENTRY_STATEMENTS, label="v14-ledger-entries"))
    return {
        "to_version": CURRENT_SCHEMA_VERSION,
        "steps": steps,
        "migrated": any(step["migrated"] for step in steps),
    }
