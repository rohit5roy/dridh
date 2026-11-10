"""Traceability read model for Phase 5.

Builds a deterministic, workspace-scoped traceability view from canonical SQLite
relationships. It intentionally does not duplicate relationship facts or derive
unlocks from a second source of truth.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from dridh.common.errors import DridhError, ErrorCode


class TraceabilityRepository:
    """Read-only traceability projections bound to one workspace."""

    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def use_case_story_map(self) -> list[dict[str, Any]]:
        """Return each use case and its stories, including use cases with no stories."""
        rows = self.connection.execute(
            """
            SELECT uc.use_case_id, uc.title AS use_case_title,
                   s.story_id, s.title AS story_title, s.status AS story_status,
                   s.component_id, s.phase_number AS phase
            FROM use_case uc
            LEFT JOIN story_use_case su
              ON su.workspace_id = uc.workspace_id AND su.use_case_id = uc.use_case_id
            LEFT JOIN story s
              ON s.workspace_id = su.workspace_id AND s.story_id = su.story_id
            WHERE uc.workspace_id = ?
            ORDER BY uc.use_case_id, s.story_id
            """,
            (self.workspace_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def story_trace(self, story_id: str) -> dict[str, Any]:
        """Resolve a story to its use cases, flows and test evidence."""
        story = self.connection.execute(
            "SELECT * FROM story WHERE workspace_id=? AND story_id=?",
            (self.workspace_id, story_id),
        ).fetchone()
        if story is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown story {story_id}")
        use_cases = self.connection.execute(
            """
            SELECT uc.use_case_id, uc.title
            FROM story_use_case su JOIN use_case uc
              ON uc.workspace_id=su.workspace_id AND uc.use_case_id=su.use_case_id
            WHERE su.workspace_id=? AND su.story_id=? ORDER BY uc.use_case_id
            """,
            (self.workspace_id, story_id),
        ).fetchall()
        evidence = self.connection.execute(
            """SELECT test_phase_id,evidence_status,evidence_ref,recorded_by,recorded_at
               FROM story_test_evidence WHERE workspace_id=? AND story_id=?
               ORDER BY recorded_at,id""",
            (self.workspace_id, story_id),
        ).fetchall()
        return {
            "story": dict(story),
            "use_cases": [dict(row) for row in use_cases],
            "test_evidence": [dict(row) for row in evidence],
        }

    def risks(self) -> list[dict[str, Any]]:
        """List risks in their canonical source-document order."""
        rows = self.connection.execute(
            "SELECT risk_id,title,likelihood,impact,mitigation,owner,position,source_revision_id "
            "FROM risk WHERE workspace_id=? ORDER BY position,risk_id",
            (self.workspace_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def test_evidence(self, *, story_id: str | None = None) -> list[dict[str, Any]]:
        """List append-only evidence records, optionally filtered to one story."""
        if story_id is None:
            rows = self.connection.execute(
                """SELECT story_id,test_phase_id,evidence_status,evidence_ref,recorded_by,recorded_at
                   FROM story_test_evidence WHERE workspace_id=? ORDER BY story_id,recorded_at,id""",
                (self.workspace_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT story_id,test_phase_id,evidence_status,evidence_ref,recorded_by,recorded_at
                   FROM story_test_evidence WHERE workspace_id=? AND story_id=?
                   ORDER BY recorded_at,id""",
                (self.workspace_id, story_id),
            ).fetchall()
        return [dict(row) for row in rows]
