"""Use-case domain repository (Phase 5 increment 1): actors, use cases and flows.

The rows are a projection of one approved ``001-USE-CASES.md`` revision. Canonical IDs
(``ACT-NN``, ``UC-NN``, ``SF-NN.N``, ``AF-NN.N``, ``EF-NN.N``, ``BE-NN.N``, ``BR-NN.N``)
are stored as text and enforced by SQLite constraints and triggers. Internal row IDs are
not user-facing.

Import is a full replacement of a document's projection inside one transaction, so a
partial import cannot be observed. Projection rows are never updated in place. Each row
records the revision it was read from, and ``projection_findings`` compares the stored
projection with the stored revision text.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.findings import Finding
from dridh.document_contracts.markdown import parse_tables, plain
from dridh.document_contracts.validator import H_ACTORS, H_FLOWS, H_UC_SUMMARY, validate_use_cases


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ActorRecord:
    actor_id: str
    actor_name: str
    actor_type: str
    actor_status: str
    description: str
    position: int


@dataclass(frozen=True)
class UseCaseRecord:
    use_case_id: str
    title: str
    primary_actor_id: str
    trigger_text: str
    category: str
    position: int


@dataclass(frozen=True)
class FlowRecord:
    flow_id: str
    parent_use_case_id: str
    flow_type: str
    step_order: int
    origin_step_flow_id: str | None
    subject: str
    actor_id: str
    action_text: str
    result_text: str
    priority: str


@dataclass(frozen=True)
class UseCaseModel:
    actors: tuple[ActorRecord, ...]
    use_cases: tuple[UseCaseRecord, ...]
    flows: tuple[FlowRecord, ...]


def _is_empty_row(row: object) -> bool:
    """A row of em-dashes is the standards' placeholder for 'no records' (TABLE-STANDARDS section 17)."""

    return all(plain(cell) == "—" for cell in row.cells)  # type: ignore[attr-defined]


def parse_use_case_model(content: str) -> UseCaseModel:
    """Read the canonical tables into a model. The caller must validate the document first."""

    tables = parse_tables(content)
    actors = tuple(
        ActorRecord(
            actor_id=plain(row.cells[0]),
            actor_name=plain(row.cells[1]),
            actor_type=plain(row.cells[2]),
            actor_status=plain(row.cells[3]),
            description=plain(row.cells[4]),
            position=index,
        )
        for table in tables
        if table.header == H_ACTORS
        for index, row in enumerate((r for r in table.rows if plain(r.cells[0]) != "—"), start=1)
    )
    use_cases = tuple(
        UseCaseRecord(
            use_case_id=plain(row.cells[0]),
            title=plain(row.cells[1]),
            primary_actor_id=plain(row.cells[2]),
            trigger_text=plain(row.cells[3]),
            category=plain(row.cells[4]),
            position=index,
        )
        for table in tables
        if table.header == H_UC_SUMMARY
        for index, row in enumerate((r for r in table.rows if not _is_empty_row(r)), start=1)
    )
    flows: list[FlowRecord] = []
    per_use_case: dict[str, int] = {}
    for table in tables:
        if table.header != H_FLOWS:
            continue
        for row in table.rows:
            flow_id = plain(row.cells[0])
            parent = f"UC-{flow_id[3:5]}"
            per_use_case[parent] = per_use_case.get(parent, 0) + 1
            origin = plain(row.cells[2])
            flows.append(
                FlowRecord(
                    flow_id=flow_id,
                    parent_use_case_id=parent,
                    flow_type=plain(row.cells[1]),
                    step_order=per_use_case[parent],
                    origin_step_flow_id=None if origin == "—" else origin,
                    subject=plain(row.cells[3]),
                    actor_id=plain(row.cells[4]),
                    action_text=plain(row.cells[5]),
                    result_text=plain(row.cells[6]),
                    priority=plain(row.cells[7]),
                )
            )
    flows.sort(key=lambda f: (f.parent_use_case_id, f.step_order))
    return UseCaseModel(actors=actors, use_cases=use_cases, flows=tuple(flows))


def validated_model(content: str, file: str) -> UseCaseModel:
    """Parse only a document that passes the canonical validator. Invalid input is never projected."""

    result = validate_use_cases(content, file=file)
    if not result.ok:
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            f"{file} does not pass use-case validation; nothing was imported",
            details=tuple(result.errors[:5]),
            recovery="Fix the reported findings, record a new revision, then import again.",
        )
    return parse_use_case_model(content)


class UseCaseRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Use-case projection writes must run inside a transaction.")

    def replace_projection(self, *, document_id: str, revision_id: str, model: UseCaseModel) -> dict[str, int]:
        """Replace the projection of one document with ``model``, read from ``revision_id``."""

        self._require_transaction()
        scope = (self.workspace_id, document_id)
        try:
            self.connection.execute("DELETE FROM use_case_import WHERE workspace_id = ? AND document_id = ?", scope)
            # Branches first: only SF rows are ever referenced as origins, so deleting branches
            # before success steps keeps every RESTRICT foreign key satisfied at each row.
            self.connection.execute("DELETE FROM use_case_flow WHERE workspace_id = ? AND document_id = ? AND origin_step_flow_id IS NOT NULL", scope)
            self.connection.execute("DELETE FROM use_case_flow WHERE workspace_id = ? AND document_id = ?", scope)
            self.connection.execute("DELETE FROM use_case WHERE workspace_id = ? AND document_id = ?", scope)
            self.connection.execute("DELETE FROM actor WHERE workspace_id = ? AND document_id = ?", scope)
            for actor in model.actors:
                self.connection.execute(
                    """INSERT INTO actor(id, workspace_id, document_id, source_revision_id, actor_id, actor_name, actor_type, actor_status, description, position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, actor.actor_id, actor.actor_name, actor.actor_type, actor.actor_status, actor.description, actor.position),
                )
            for use_case in model.use_cases:
                self.connection.execute(
                    """INSERT INTO use_case(id, workspace_id, document_id, source_revision_id, use_case_id, title, primary_actor_id, trigger_text, category, position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, use_case.use_case_id, use_case.title, use_case.primary_actor_id, use_case.trigger_text, use_case.category, use_case.position),
                )
            # Success steps first so that branch origins can reference them within the same statement order.
            ordered = sorted(model.flows, key=lambda f: (f.flow_type != "SUCCESS", f.parent_use_case_id, f.step_order))
            for flow in ordered:
                self.connection.execute(
                    """INSERT INTO use_case_flow(id, workspace_id, document_id, source_revision_id, flow_id, parent_use_case_id, flow_type, step_order, origin_step_flow_id, subject, actor_id, action_text, result_text, priority)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        self.workspace_id,
                        document_id,
                        revision_id,
                        flow.flow_id,
                        flow.parent_use_case_id,
                        flow.flow_type,
                        flow.step_order,
                        flow.origin_step_flow_id,
                        flow.subject,
                        flow.actor_id,
                        flow.action_text,
                        flow.result_text,
                        flow.priority,
                    ),
                )
            self.connection.execute(
                """INSERT INTO use_case_import(workspace_id, document_id, source_revision_id, actor_count, use_case_count, flow_count, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.workspace_id, document_id, revision_id, len(model.actors), len(model.use_cases), len(model.flows), _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "The use-case projection violates a canonical constraint; nothing was imported",
                details=(str(exc),),
            ) from None
        return {"actors": len(model.actors), "use_cases": len(model.use_cases), "flows": len(model.flows)}

    def import_marker(self, document_id: str) -> sqlite3.Row | None:
        """The record that an import happened. An empty document has a marker with zero counts."""

        return self.connection.execute(
            "SELECT * FROM use_case_import WHERE workspace_id = ? AND document_id = ?",
            (self.workspace_id, document_id),
        ).fetchone()

    def projection_is_current(self, *, document_id: str, revision_id: str, model: UseCaseModel) -> bool:
        """True when a re-import would change nothing: same source revision and identical rows."""

        marker = self.import_marker(document_id)
        return marker is not None and marker["source_revision_id"] == revision_id and self.stored_model(document_id) == model

    def stored_model(self, document_id: str) -> UseCaseModel:
        actors = tuple(
            ActorRecord(r["actor_id"], r["actor_name"], r["actor_type"], r["actor_status"], r["description"], r["position"])
            for r in self.connection.execute(
                "SELECT * FROM actor WHERE workspace_id = ? AND document_id = ? ORDER BY position", (self.workspace_id, document_id)
            )
        )
        use_cases = tuple(
            UseCaseRecord(r["use_case_id"], r["title"], r["primary_actor_id"], r["trigger_text"], r["category"], r["position"])
            for r in self.connection.execute(
                "SELECT * FROM use_case WHERE workspace_id = ? AND document_id = ? ORDER BY position", (self.workspace_id, document_id)
            )
        )
        flows = tuple(
            FlowRecord(
                r["flow_id"], r["parent_use_case_id"], r["flow_type"], r["step_order"], r["origin_step_flow_id"],
                r["subject"], r["actor_id"], r["action_text"], r["result_text"], r["priority"],
            )
            for r in self.connection.execute(
                "SELECT * FROM use_case_flow WHERE workspace_id = ? AND document_id = ? ORDER BY parent_use_case_id, step_order",
                (self.workspace_id, document_id),
            )
        )
        return UseCaseModel(actors=actors, use_cases=use_cases, flows=flows)

    def projection_revision_ids(self, document_id: str) -> set[str]:
        rows = self.connection.execute(
            """SELECT source_revision_id FROM actor WHERE workspace_id = ? AND document_id = ?
               UNION SELECT source_revision_id FROM use_case WHERE workspace_id = ? AND document_id = ?
               UNION SELECT source_revision_id FROM use_case_flow WHERE workspace_id = ? AND document_id = ?""",
            (self.workspace_id, document_id) * 3,
        ).fetchall()
        return {row[0] for row in rows}

    def projection_findings(self, *, relative_path: str, document_id: str, current_revision_id: str, current_content: str) -> list[Finding]:
        """Reconcile the stored rows with the stored revision text. Never reads the source file."""

        findings: list[Finding] = []
        marker = self.import_marker(document_id)
        if marker is None:
            findings.append(
                Finding(
                    code="PROJECTION_MISSING",
                    message="The use-case domain has never imported this document",
                    file=relative_path,
                    repair_hint=f"Import it: dridh usecases import {relative_path}",
                )
            )
            return findings
        if marker["source_revision_id"] != current_revision_id:
            findings.append(
                Finding(
                    code="PROJECTION_STALE",
                    message="The use-case rows were imported from a revision that is no longer current",
                    file=relative_path,
                    expected=current_revision_id,
                    actual=marker["source_revision_id"],
                    repair_hint=f"Re-import the current revision: dridh usecases import {relative_path}",
                )
            )
        if not validate_use_cases(current_content, file=relative_path).ok:
            findings.append(
                Finding(
                    code="REVISION_NOT_PROJECTABLE",
                    message="The current revision does not pass use-case validation, so it cannot be projected",
                    file=relative_path,
                    repair_hint="Fix the document, record a new revision, then import",
                )
            )
        elif parse_use_case_model(current_content) != self.stored_model(document_id):
            findings.append(
                Finding(
                    code="PROJECTION_MISMATCH",
                    message="The stored use-case rows differ from the stored revision text",
                    file=relative_path,
                    repair_hint=f"Re-import to rebuild the projection: dridh usecases import {relative_path}",
                )
            )
        return findings
