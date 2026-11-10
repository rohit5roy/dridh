"""Design domain repository (Phase 5 increment 2): components, design decisions and traceability.

Sources:
- A feature TDD (Stage 2) defines components (``### CMP-NN:``), feature design decisions
  (``#### DD-N:``) and the use-case traceability matrix (Table 13), from which the
  use-case/component mappings and API/event references are projected.
- A component master defines component design decisions (``#### CDD-N:``) for the component
  named in its ``**Component ID:**`` line.

Feature ``DD-N`` and component ``CDD-N`` are stored in one table with a scope and a CHECK that
forbids mixing the two. Import validates the document first and refuses it on any finding.
Mappings reference the use-case projection by foreign key, so a Table 13 row that names an
unimported use case or flow is refused with the missing IDs listed.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.findings import Finding
from dridh.document_contracts.markdown import Heading, parse_headings, parse_tables, plain
from dridh.document_contracts.validator import (
    H_TRACE,
    validate_component_master,
    validate_tdd,
)

_COMPONENT_HEADING = re.compile(r"(CMP-\d{2}):\s+(\S.*)")
_DECISION_HEADING = re.compile(r"(DD-\d+):\s+(\S.*)")
_CDD_HEADING = re.compile(r"(CDD-\d+):\s+(\S.*)")
_COMPONENT_ID_LINE = re.compile(r"- \*\*Component ID:\*\*\s*(CMP-\d{2})")
_CMP_TOKEN = re.compile(r"CMP-\d{2}")
_DD_TOKEN = re.compile(r"(?<![A-Z])DD-\d+")
_FLOW_TOKEN = re.compile(r"(?:SF|AF|EF|BE|BR)-\d{2}\.\d+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ComponentRecord:
    component_id: str
    component_name: str
    definition_text: str
    position: int


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    scope: str
    component_id: str | None
    title: str
    definition_text: str
    position: int


@dataclass(frozen=True)
class MappingRecord:
    use_case_id: str
    flow_id: str | None
    component_id: str
    design_decision_id: str | None
    position: int


@dataclass(frozen=True)
class ApiEventRecord:
    use_case_id: str
    flow_id: str | None
    component_id: str
    reference_kind: str
    reference_name: str
    position: int


@dataclass(frozen=True)
class TddModel:
    components: tuple[ComponentRecord, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    mappings: tuple[MappingRecord, ...] = ()
    api_events: tuple[ApiEventRecord, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ComponentMasterModel:
    component_id: str
    decisions: tuple[DecisionRecord, ...] = ()


def _sections(content: str) -> list[tuple[Heading, str]]:
    """Each heading with the text under it, up to the next heading of the same or higher level."""

    lines = content.splitlines()
    headings = parse_headings(content)
    out: list[tuple[Heading, str]] = []
    for index, heading in enumerate(headings):
        end = next((h.line - 1 for h in headings[index + 1 :] if h.level <= heading.level), len(lines))
        out.append((heading, "\n".join(lines[heading.line : end]).strip()))
    return out


def parse_tdd_model(content: str) -> TddModel:
    """Read components, feature decisions and Table 13. Call only on a validated document."""

    components: list[ComponentRecord] = []
    decisions: list[DecisionRecord] = []
    for heading, body in _sections(content):
        if heading.level == 3 and (match := _COMPONENT_HEADING.fullmatch(heading.title)):
            components.append(ComponentRecord(match.group(1), match.group(2), body, len(components) + 1))
        if heading.level == 4 and (match := _DECISION_HEADING.fullmatch(heading.title)):
            decisions.append(DecisionRecord(match.group(1), "FEATURE", None, match.group(2), body, len(decisions) + 1))

    mappings: list[MappingRecord] = []
    api_events: list[ApiEventRecord] = []
    for table in parse_tables(content):
        if table.header != H_TRACE:
            continue
        for row in table.rows:
            if len(row.cells) != len(H_TRACE):
                continue
            use_case = plain(row.cells[0])
            flows: list[str | None] = _FLOW_TOKEN.findall(row.cells[1]) or [None]
            components_in_row = _CMP_TOKEN.findall(row.cells[2])
            decisions_in_row: list[str | None] = _DD_TOKEN.findall(row.cells[4]) or [None]
            references = [] if plain(row.cells[3]) == "N/A" else [part.strip() for part in row.cells[3].split(";") if part.strip()]
            for flow in flows:
                for component in components_in_row:
                    for decision in decisions_in_row:
                        mappings.append(MappingRecord(use_case, flow, component, decision, len(mappings) + 1))
                    for reference in references:
                        kind, name = ("EVENT", reference[len("Event:") :].strip()) if reference.startswith("Event:") else ("API", reference)
                        api_events.append(ApiEventRecord(use_case, flow, component, kind, name, len(api_events) + 1))

    return TddModel(tuple(components), tuple(decisions), tuple(mappings), tuple(api_events))


def parse_component_master_model(content: str) -> ComponentMasterModel:
    """Read the owning component and its CDD decisions. Call only on a validated document."""

    match = _COMPONENT_ID_LINE.search(content)
    if match is None:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "The component master names no Component ID", recovery="Add '- **Component ID:** CMP-NN' to the Component Overview.")
    component = match.group(1)
    decisions: list[DecisionRecord] = []
    for heading, body in _sections(content):
        if heading.level == 4 and (found := _CDD_HEADING.fullmatch(heading.title)):
            decisions.append(DecisionRecord(found.group(1), "COMPONENT", component, found.group(2), body, len(decisions) + 1))
    return ComponentMasterModel(component, tuple(decisions))


def validated_tdd_model(content: str, file: str) -> TddModel:
    result = validate_tdd(content, file=file)
    if not result.ok:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{file} does not pass TDD validation; nothing was imported", details=tuple(result.errors[:5]))
    return parse_tdd_model(content)


def validated_master_model(content: str, file: str) -> ComponentMasterModel:
    result = validate_component_master(content, file=file)
    if not result.ok:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{file} does not pass component-master validation; nothing was imported", details=tuple(result.errors[:5]))
    return parse_component_master_model(content)


class DesignRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Design projection writes must run inside a transaction.")

    def import_marker(self, document_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM design_import WHERE workspace_id = ? AND document_id = ?", (self.workspace_id, document_id)
        ).fetchone()

    def _missing_use_cases(self, model: TddModel) -> list[str]:
        known_ucs = {r[0] for r in self.connection.execute("SELECT use_case_id FROM use_case WHERE workspace_id = ?", (self.workspace_id,))}
        known_flows = {r[0] for r in self.connection.execute("SELECT flow_id FROM use_case_flow WHERE workspace_id = ?", (self.workspace_id,))}
        missing = {m.use_case_id for m in model.mappings if m.use_case_id not in known_ucs}
        missing |= {a.use_case_id for a in model.api_events if a.use_case_id not in known_ucs}
        missing |= {m.flow_id for m in model.mappings if m.flow_id and m.flow_id not in known_flows}
        missing |= {a.flow_id for a in model.api_events if a.flow_id and a.flow_id not in known_flows}
        return sorted(missing)

    def replace_tdd(self, *, document_id: str, revision_id: str, model: TddModel) -> dict[str, int]:
        self._require_transaction()
        missing = self._missing_use_cases(model)
        if missing:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "Table 13 names use cases or flows that are not imported; nothing was imported",
                details=tuple(missing),
                recovery="Import the use-case document first: dridh usecases import <path>",
            )
        scope = (self.workspace_id, document_id)
        try:
            self.connection.execute("DELETE FROM api_event_mapping WHERE workspace_id = ? AND document_id = ?", scope)
            self.connection.execute("DELETE FROM use_case_component_mapping WHERE workspace_id = ? AND document_id = ?", scope)
            self.connection.execute("DELETE FROM design_decision WHERE workspace_id = ? AND document_id = ? AND scope = 'FEATURE'", scope)
            self.connection.execute("DELETE FROM component WHERE workspace_id = ? AND document_id = ?", scope)
            self.connection.execute("DELETE FROM design_import WHERE workspace_id = ? AND document_id = ?", scope)
            for c in model.components:
                self.connection.execute(
                    """INSERT INTO component(id, workspace_id, document_id, source_revision_id, component_id, component_name, definition_text, position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, c.component_id, c.component_name, c.definition_text, c.position),
                )
            for d in model.decisions:
                self.connection.execute(
                    """INSERT INTO design_decision(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position)
                       VALUES (?, ?, ?, ?, ?, 'FEATURE', NULL, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, d.decision_id, d.title, d.definition_text, d.position),
                )
            for m in model.mappings:
                self.connection.execute(
                    """INSERT INTO use_case_component_mapping(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, design_decision_id, position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, m.use_case_id, m.flow_id, m.component_id, m.design_decision_id, m.position),
                )
            for a in model.api_events:
                self.connection.execute(
                    """INSERT INTO api_event_mapping(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, reference_kind, reference_name, position)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, a.use_case_id, a.flow_id, a.component_id, a.reference_kind, a.reference_name, a.position),
                )
            counts = {"components": len(model.components), "decisions": len(model.decisions), "mappings": len(model.mappings), "api_events": len(model.api_events)}
            self.connection.execute(
                """INSERT INTO design_import(workspace_id, document_id, source_revision_id, document_kind, component_count, decision_count, mapping_count, api_event_count, imported_at)
                   VALUES (?, ?, ?, 'TDD', ?, ?, ?, ?, ?)""",
                (self.workspace_id, document_id, revision_id, counts["components"], counts["decisions"], counts["mappings"], counts["api_events"], _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The TDD projection violates a canonical constraint; nothing was imported", details=(str(exc),)) from None
        return counts

    def replace_component_master(self, *, document_id: str, revision_id: str, model: ComponentMasterModel) -> dict[str, int]:
        self._require_transaction()
        exists = self.connection.execute(
            "SELECT 1 FROM component WHERE workspace_id = ? AND component_id = ?", (self.workspace_id, model.component_id)
        ).fetchone()
        if exists is None:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                f"{model.component_id} is not defined by an imported TDD; nothing was imported",
                recovery="Import the feature TDD first: dridh design import <tdd path>",
            )
        scope = (self.workspace_id, document_id)
        try:
            self.connection.execute("DELETE FROM design_decision WHERE workspace_id = ? AND document_id = ? AND scope = 'COMPONENT'", scope)
            self.connection.execute("DELETE FROM design_import WHERE workspace_id = ? AND document_id = ?", scope)
            for d in model.decisions:
                self.connection.execute(
                    """INSERT INTO design_decision(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position)
                       VALUES (?, ?, ?, ?, ?, 'COMPONENT', ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, d.decision_id, model.component_id, d.title, d.definition_text, d.position),
                )
            self.connection.execute(
                """INSERT INTO design_import(workspace_id, document_id, source_revision_id, document_kind, component_count, decision_count, mapping_count, api_event_count, component_id, imported_at)
                   VALUES (?, ?, ?, 'COMPONENT_MASTER', 0, ?, 0, 0, ?, ?)""",
                (self.workspace_id, document_id, revision_id, len(model.decisions), model.component_id, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The component-master projection violates a canonical constraint; nothing was imported", details=(str(exc),)) from None
        return {"decisions": len(model.decisions)}

    def is_current(self, *, document_id: str, revision_id: str, kind: str, model: TddModel | ComponentMasterModel) -> bool:
        marker = self.import_marker(document_id)
        if marker is None or marker["source_revision_id"] != revision_id:
            return False
        return (self.stored_tdd(document_id) if kind == "TDD" else self.stored_master(document_id)) == model

    def stored_tdd(self, document_id: str) -> TddModel:
        components = tuple(
            ComponentRecord(r["component_id"], r["component_name"], r["definition_text"], r["position"])
            for r in self.connection.execute(
                "SELECT * FROM component WHERE workspace_id = ? AND document_id = ? ORDER BY position", (self.workspace_id, document_id)
            )
        )
        decisions = tuple(
            DecisionRecord(r["decision_id"], r["scope"], r["component_id"], r["title"], r["definition_text"], r["position"])
            for r in self.connection.execute(
                "SELECT * FROM design_decision WHERE workspace_id = ? AND document_id = ? AND scope = 'FEATURE' ORDER BY position",
                (self.workspace_id, document_id),
            )
        )
        mappings = tuple(
            MappingRecord(r["use_case_id"], r["flow_id"], r["component_id"], r["design_decision_id"], r["position"])
            for r in self.connection.execute(
                "SELECT * FROM use_case_component_mapping WHERE workspace_id = ? AND document_id = ? ORDER BY position",
                (self.workspace_id, document_id),
            )
        )
        api_events = tuple(
            ApiEventRecord(r["use_case_id"], r["flow_id"], r["component_id"], r["reference_kind"], r["reference_name"], r["position"])
            for r in self.connection.execute(
                "SELECT * FROM api_event_mapping WHERE workspace_id = ? AND document_id = ? ORDER BY position",
                (self.workspace_id, document_id),
            )
        )
        return TddModel(components, decisions, mappings, api_events)

    def stored_master(self, document_id: str) -> ComponentMasterModel:
        rows = self.connection.execute(
            "SELECT * FROM design_decision WHERE workspace_id = ? AND document_id = ? AND scope = 'COMPONENT' ORDER BY position",
            (self.workspace_id, document_id),
        ).fetchall()
        marker = self.import_marker(document_id)
        component = marker["component_id"] if marker is not None and marker["component_id"] else ""
        return ComponentMasterModel(
            component,
            tuple(DecisionRecord(r["decision_id"], r["scope"], r["component_id"], r["title"], r["definition_text"], r["position"]) for r in rows),
        )

    def findings(self, *, relative_path: str, document_id: str, kind: str, current_revision_id: str, current_content: str) -> list[Finding]:
        """Reconcile stored design rows with the stored revision text. Never reads the source file."""

        marker = self.import_marker(document_id)
        if marker is None:
            return [Finding("DESIGN_NEVER_IMPORTED", "The design domain has never imported this document", file=relative_path, repair_hint=f"Import it: dridh design import {relative_path}")]
        findings: list[Finding] = []
        if marker["source_revision_id"] != current_revision_id:
            findings.append(
                Finding("DESIGN_PROJECTION_STALE", "The design rows were imported from a revision that is no longer current", file=relative_path,
                        expected=current_revision_id, actual=marker["source_revision_id"], repair_hint=f"Re-import: dridh design import {relative_path}")
            )
        if kind == "TDD":
            if not validate_tdd(current_content, file=relative_path).ok:
                return findings + [Finding("REVISION_NOT_PROJECTABLE", "The current revision does not pass TDD validation", file=relative_path, repair_hint="Fix the document and record a new revision")]
            if parse_tdd_model(current_content) != self.stored_tdd(document_id):
                findings.append(Finding("DESIGN_PROJECTION_MISMATCH", "The stored design rows differ from the stored revision text", file=relative_path, repair_hint=f"Re-import: dridh design import {relative_path}"))
        else:
            if not validate_component_master(current_content, file=relative_path).ok:
                return findings + [Finding("REVISION_NOT_PROJECTABLE", "The current revision does not pass component-master validation", file=relative_path, repair_hint="Fix the document and record a new revision")]
            if parse_component_master_model(current_content) != self.stored_master(document_id):
                findings.append(Finding("DESIGN_PROJECTION_MISMATCH", "The stored design rows differ from the stored revision text", file=relative_path, repair_hint=f"Re-import: dridh design import {relative_path}"))
            stored = self.stored_master(document_id)
            resolved = self.connection.execute(
                "SELECT 1 FROM component WHERE workspace_id = ? AND component_id = ?", (self.workspace_id, stored.component_id)
            ).fetchone()
            if stored.component_id and resolved is None:
                findings.append(
                    Finding("CDD_COMPONENT_UNRESOLVED", f"{stored.component_id} is no longer defined by an imported TDD", file=relative_path, entity_id=stored.component_id,
                            repair_hint="Re-import the TDD that defines this component, or remove the component master")
                )
        return findings
