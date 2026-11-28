"""Where a feature's documents live, and what they are called.

Every feature created by this version gets its own folder in the repository, committed with the feature's code:

    docs/dridh/001-ORDER-INTAKE/
      001-USE-CASES.md
      002-TECHNICAL-DESIGN.md
      003-CENTRAL-PLAN-INDEX.md
      004-TEST-PLAN.md
      005-PROJECT-STRUCTURE-PLAN.md
      006-PLANNING-REVIEW-CHECKLIST.md
      007-LEDGER.md
      components/CMP-01/
        001-COMPONENT-MASTER.md
        002-PHASE-1-ORDER-SERVICE.md

File names are upper case with a three-digit serial: the order the stages write them in (component plans are numbered
inside their own folder). Features created before this layout keep the names Dridh recorded for them (``LEGACY``):
nothing on disk is renamed, so their approvals stay valid.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

DOCS_ROOT = "docs/dridh"
LEGACY_PLAN_DIR = ".opencode/dridh/documents"

USE_CASES = "001-USE-CASES.md"
DESIGN = "002-TECHNICAL-DESIGN.md"
CENTRAL_PLAN = "003-CENTRAL-PLAN-INDEX.md"
TEST_PLAN = "004-TEST-PLAN.md"
PROJECT_STRUCTURE = "005-PROJECT-STRUCTURE-PLAN.md"
PLANNING_CHECKLIST = "006-PLANNING-REVIEW-CHECKLIST.md"
LEDGER = "007-LEDGER.md"
COMPONENT_MASTER = "001-COMPONENT-MASTER.md"


def upper_slug(value: str, *, fallback: str = "COMPONENT") -> str:
    """``Order service`` -> ``ORDER-SERVICE``: upper case letters and digits joined by single hyphens."""
    return re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")[:70] or fallback


def feature_folder(serial: int, slug: str) -> str:
    return f"{DOCS_ROOT}/{serial:03d}-{upper_slug(slug, fallback='FEATURE')}"


@dataclass(frozen=True)
class DocumentLayout:
    """The paths of one feature's documents, relative to the project root. ``folder`` is None for the legacy layout."""

    folder: str | None

    @property
    def legacy(self) -> bool:
        return self.folder is None

    @property
    def plan_dir(self) -> str:
        return self.folder or LEGACY_PLAN_DIR

    def use_cases(self) -> str:
        return f"{self.folder}/{USE_CASES}" if self.folder else "001-USE-CASES.md"

    def design(self, feature_slug: str) -> str:
        return f"{self.folder}/{DESIGN}" if self.folder else f"{feature_slug}_tdd.md"

    def central_plan(self) -> str:
        return f"{self.plan_dir}/{CENTRAL_PLAN if self.folder else 'CENTRAL-PLAN-INDEX.md'}"

    def test_plan(self) -> str:
        return f"{self.plan_dir}/{TEST_PLAN if self.folder else 'TEST-PLAN.md'}"

    def project_structure(self) -> str:
        return f"{self.plan_dir}/{PROJECT_STRUCTURE if self.folder else 'PROJECT-STRUCTURE-PLAN.md'}"

    def planning_checklist(self) -> str:
        return f"{self.plan_dir}/{PLANNING_CHECKLIST if self.folder else 'PLANNING-REVIEW-CHECKLIST.md'}"

    def ledger(self) -> str:
        return f"{self.plan_dir}/{LEDGER if self.folder else 'LEDGER.md'}"

    def component_dir(self, component_id: str) -> str:
        return f"{self.plan_dir}/components/{component_id}"

    def component_master(self, component_id: str) -> str:
        return f"{self.component_dir(component_id)}/{COMPONENT_MASTER if self.folder else 'COMPONENT-MASTER.md'}"

    def phase_name(self, position: int, phase: int, component_name: str) -> str:
        """The file name of a component's ``position``-th phase file (0-based): it follows the component master."""
        if self.folder:
            return f"{position + 2:03d}-PHASE-{phase}-{upper_slug(component_name)}.md"
        legacy = re.sub(r"[^a-z0-9]+", "-", component_name.lower()).strip("-")[:70] or "component"
        return f"PHASE-{phase}-{legacy}.md"

    def phase_file(self, component_id: str, position: int, phase: int, component_name: str) -> str:
        return f"{self.component_dir(component_id)}/{self.phase_name(position, phase, component_name)}"


LEGACY = DocumentLayout(None)


def feature_layout(connection: sqlite3.Connection, workspace_id: str, feature_id: str | None) -> DocumentLayout:
    """The layout of a feature: its folder when it has one, else the legacy names."""
    if not feature_id:
        return LEGACY
    try:
        row = connection.execute(
            "SELECT folder FROM feature_folder WHERE workspace_id=? AND feature_id=?", (workspace_id, feature_id)).fetchone()
    except sqlite3.OperationalError:  # a database from before feature folders
        return LEGACY
    return DocumentLayout(str(row[0])) if row else LEGACY


def session_layout(connection: sqlite3.Connection, workspace_id: str, **sessions: str | None) -> DocumentLayout:
    """The layout of the feature bound to any of the given stage sessions (``ideator=``, ``designer=``, ``planner=``).

    A session that belongs to no feature keeps the legacy names.
    """
    for stage in ("planner", "designer", "ideator"):
        session_id = sessions.get(stage)
        if not session_id:
            continue
        row = connection.execute(
            f"SELECT id FROM feature WHERE workspace_id=? AND {stage}_session_id=?",  # noqa: S608 - stage is allow-listed
            (workspace_id, session_id)).fetchone()
        if row is not None:
            return feature_layout(connection, workspace_id, str(row[0]))
    return LEGACY
