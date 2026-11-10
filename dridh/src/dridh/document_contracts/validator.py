"""Deterministic validators for Dridh planning documents (Phase 6).

Every validator parses Markdown structurally (see ``markdown.py``), checks exact
table headers and row widths, validates ID formats, uniqueness, enums and
references, and reports precise findings. Validators never mutate source content.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dridh.document_contracts.findings import Finding, ValidationResult
from dridh.document_contracts.markdown import (
    Heading,
    Table,
    heading_before,
    parse_headings,
    parse_tables,
    plain,
    split_row,
)
from dridh.document_contracts.registry import ContractBundleError, load_contract_bundle

STANDARDS_VERSION = "2.1.0"

UC_ID = re.compile(r"UC-\d{2}")
ACT_ID = re.compile(r"ACT-\d{2}")
FLOW_ID = re.compile(r"(SF|AF|EF|BE|BR)-(\d{2})\.(\d+)")
CMP_ID = re.compile(r"CMP-\d{2}")
LDG_ID = re.compile(r"LDG-\d{2}")
RISK_ID = re.compile(r"R\d+")
TEST_PHASE_ID = re.compile(r"[SDX]\d+")
STORY_ID = re.compile(r"ST-\d+\.\d+|PRE-\d+|X\d+|[A-Z]{2,3}-\d+")
COMPONENT_CELL = re.compile(r"CMP-\d{2} — .+")
_RESERVED_PREFIXES = {"UC", "ACT", "CMP", "CDD", "DD", "LDG", "SF", "AF", "EF", "BE", "BR"}

FLOW_TYPE_BY_PREFIX = {
    "SF": "SUCCESS",
    "AF": "ALTERNATIVE",
    "EF": "EXCEPTION",
    "BE": "BOUNDARY",
    "BR": "BUSINESS_RULE",
}
ACTOR_TYPES = ("USER", "SYSTEM", "SERVICE")
ACTOR_STATUSES = ("EXISTING", "NEW")
STORY_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "IN_REVIEW", "COMPLETED", "BLOCKED")
LEDGER_STATUSES = ("DONE", "DEVIATED", "NOT_DONE", "WONT_DO")
LEDGER_APPROVAL_REQUIRED = ("DEVIATED", "NOT_DONE", "WONT_DO")
DEPENDENCY_MODES = ("TESTCONTAINERS", "DOCKER_COMPOSE", "REAL_QA_ENDPOINT", "WIREMOCK_EXCEPTION")
TEST_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "PASSED", "FAILED", "BLOCKED", "NOT_APPLICABLE")
NO_APPROVER = ("", "—", "-", "N/A", "PENDING", "TBD")
WIREMOCK_REQUIRED_FIELDS = ("dependency:", "contract:", "owner:", "expires:")

H_ACTORS = ("Actor ID", "Actor Name", "Type", "Status", "Description")
H_UC_SUMMARY = ("ID", "Use Case", "Primary Actor", "Trigger", "Category")
H_FLOWS = ("Flow ID", "Type", "Origin Step", "Subject", "Actor ID", "Action", "Result", "Priority")
H_CENTRAL = (
    "Order",
    "Story ID",
    "Component",
    "Phase",
    "Use Cases",
    "Title",
    "Dependencies",
    "Unlocks",
    "Parallel With",
    "Status",
)
H_STORY_CARD = ("Field", "Value")
STORY_CARD_FIELDS = (
    "Use Case ID",
    "Flow IDs",
    "Title",
    "Description",
    "Status",
    "Dependencies",
    "Unlocks",
    "What Will Be Done",
    "Acceptance Criteria",
    "Definition of Done",
)
H_STORY_INDEX = ("Story ID", "Phase", "Story Name", "Use Cases", "Status")
H_DASHBOARD = ("Phase", "Total Stories", "NOT_STARTED", "IN_PROGRESS", "IN_REVIEW", "COMPLETED", "BLOCKED")
H_TRACE = ("UC ID", "Flow IDs", "Catering Component(s)", "API / Event", "Design Decision (DD-N)")
H_LEDGER = (
    "Ledger ID",
    "UC / Flow ID",
    "Deviation Status",
    "Linked Story ID",
    "Original Requirement",
    "Actual / Deviated Behavior",
    "Justification",
    "Approved By",
)
H_TEST = (
    "Test Phase ID",
    "Test Scope",
    "Use Case / Flow IDs",
    "Component IDs",
    "Dependency Mode",
    "Environment",
    "Execution Command",
    "Evidence",
    "Status",
)

TDD_SECTIONS = (
    "Document Control",
    "Blocking Reviewers",
    "Problem Statement & Resources",
    "Definitions",
    "Scope",
    "Out of Scope",
    "Target Dates and Milestones",
    "Design Summary",
    "Design Details",
    "Design Decisions",
    "Dependencies",
    "API Design",
    "Events",
    "Data Model",
    "Quality Approach",
    "Well-Architected Considerations",
    "Expected Level of Effort",
    "Open Questions And Assumptions",
    "OPTIONAL — Technical Overview Supporting Sections",
    "Technical Justifications / Alternative Designs",
    "Integration/Migration Plan",
    "Rollback Plan",
    "Feature Flags",
    "Feature/Service Logic",
    "Scaling Estimate",
    "Monitoring & Alerting",
    "Failure Scenarios And Recovery",
    "Risks",
    "Acceptance Criteria",
    "AI Systems",
    "TDD Completion Checklist",
    "Document References",
)
TDD_BOILERPLATE = (
    "Replace the illustrative diagram above",
    "Replace the illustrative sequence",
    "Replace the illustrative row",
    "Replace the illustrative specification",
    "TEMPLATE AUTHORING CONTRACT",
)
TDD_PLACEHOLDERS = (
    "<PLACEHOLDER>",
    "<FEATURE NAME>",
    "<Feature name>",
    "<Name or role>",
    "[authoring instruction]",
    "<Component Name>",
    "<Decision Title>",
    "<Risk Title>",
)

_STANDARDS_VERSION = re.compile(r"\*\*Standards Version:\*\*\s*(\S+)")
_SOURCE_REVISION = re.compile(r"\*\*Source Revision:\*\*\s*(.+?)\s*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_ASIDE = re.compile(r"<aside\b[^>]*>.*?</aside>", re.DOTALL | re.IGNORECASE)


class _Report:
    def __init__(self, file: str | None = None) -> None:
        self.file = file
        self.findings: list[Finding] = []

    def error(self, code: str, message: str, **kwargs: object) -> None:
        self._add("ERROR", code, message, **kwargs)

    def warn(self, code: str, message: str, **kwargs: object) -> None:
        self._add("WARNING", code, message, **kwargs)

    def _add(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        entity: str | None = None,
        expected: object = None,
        actual: object = None,
        hint: str | None = None,
    ) -> None:
        self.findings.append(
            Finding(
                code=code,
                message=message,
                severity=severity,
                file=file or self.file,
                line=line,
                entity_id=entity,
                expected=None if expected is None else str(expected),
                actual=None if actual is None else str(actual),
                repair_hint=hint,
            )
        )

    def result(self) -> ValidationResult:
        return ValidationResult(list(self.findings))


def _split(cell: str) -> list[str]:
    value = plain(cell)
    if value in ("", "—", "-", "N/A", "None"):
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def _tokens(cell: str, prefixes: Iterable[str]) -> list[str]:
    alternation = "|".join(prefixes)
    return re.findall(rf"\b(?:{alternation})-[\w.]+", cell)


def is_story_id(value: str) -> bool:
    if value.startswith("ST-"):
        return re.fullmatch(r"ST-\d+\.\d+", value) is not None
    if not STORY_ID.fullmatch(value):
        return False
    return value.split("-", 1)[0] not in _RESERVED_PREFIXES


def _all_ids_valid(cell: str, prefixes: Iterable[str], patterns: tuple[re.Pattern[str], ...]) -> bool:
    tokens = _tokens(cell, prefixes)
    return bool(tokens) and all(any(p.fullmatch(t) for p in patterns) for t in tokens)


def _check_separators(report: _Report, tables: list[Table]) -> None:
    """Reject malformed separator rows (TABLE_SEPARATOR_INVALID) and cut-off rows (TABLE_ROW_UNTERMINATED) anywhere in the document."""
    for table in tables:
        if table.header_terminated:
            for row in table.rows:
                if not row.terminated:
                    report.error(
                        "TABLE_ROW_UNTERMINATED",
                        "Table row does not end with '|' although its header does: the document may be truncated or half-written",
                        line=row.line,
                        entity=row.cells[0] if row.cells else None,
                        expected="a row ending with '|'",
                        actual=f"{len(row.cells)} cells, unterminated",
                        hint="Regenerate the document, or restore it from its last recorded revision",
                    )
        if table.separator_ok:
            continue
        report.error(
            "TABLE_SEPARATOR_INVALID",
            f"Table separator row is malformed: {table.separator_error}",
            line=table.separator_line,
            entity=table.header[0] if table.header else None,
            expected=f"{len(table.header)} columns of ---",
            actual=" | ".join(table.separator_cells),
            hint="Use one '---' style alignment marker per header column",
        )


def _locate(report: _Report, tables: list[Table], header: tuple[str, ...], label: str) -> Table | None:
    for table in tables:
        if table.header == header:
            return table
    for table in tables:
        if table.header and table.header[0] == header[0]:
            report.error(
                "TABLE_HEADER_MISMATCH",
                f"{label} header does not match the canonical header",
                line=table.line,
                expected=" | ".join(header),
                actual=" | ".join(table.header),
                hint="Use the exact header and column order from TABLE-STANDARDS.md",
            )
            return table
    report.error(
        "TABLE_MISSING",
        f"{label} is missing",
        expected=" | ".join(header),
        hint=f"Add the {label} table with the canonical header",
    )
    return None


def _check_width(report: _Report, row_line: int, cells: tuple[str, ...], width: int, entity: str | None = None) -> bool:
    if len(cells) == width:
        return True
    report.error(
        "ROW_WIDTH",
        "Table row has the wrong number of columns",
        line=row_line,
        entity=entity,
        expected=width,
        actual=len(cells),
        hint="Each row must have exactly one cell per header column",
    )
    return False


def _check_enum(report: _Report, line: int, field: str, value: str, allowed: tuple[str, ...], entity: str | None) -> None:
    if value not in allowed:
        report.error(
            "ENUM_INVALID",
            f"{field} must be one of the controlled values",
            line=line,
            entity=entity,
            expected=" | ".join(allowed),
            actual=value,
            hint=f"Set {field} to one of: {', '.join(allowed)}",
        )


def _check_id(report: _Report, line: int, value: str, pattern: re.Pattern[str], label: str) -> bool:
    if pattern.fullmatch(value):
        return True
    report.error(
        "ID_FORMAT",
        f"{label} does not match the canonical ID format",
        line=line,
        entity=value,
        expected=pattern.pattern,
        actual=value,
    )
    return False


# ---------------------------------------------------------------------------
# Use Cases (Tables 1A, 1, 1B)
# ---------------------------------------------------------------------------

_UC_HEADING = re.compile(r"(UC-\d{2}):\s+\S")


def validate_use_cases(content: str, file: str = "001-USE-CASES.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    tables = parse_tables(content)
    headings = parse_headings(content)

    actors: dict[str, int] = {}
    table_1a = _locate(report, tables, H_ACTORS, "Table 1A Actors Registry")
    if table_1a is not None:
        for row in table_1a.rows:
            if not _check_width(report, row.line, row.cells, len(H_ACTORS)):
                continue
            cells = [plain(c) for c in row.cells]
            if cells[0] == "—":
                continue
            if not _check_id(report, row.line, cells[0], ACT_ID, "Actor ID"):
                continue
            if cells[0] in actors:
                report.error("DUPLICATE_ID", "Actor ID is defined more than once", line=row.line, entity=cells[0])
            actors[cells[0]] = row.line
            _check_enum(report, row.line, "Type", cells[2], ACTOR_TYPES, cells[0])
            _check_enum(report, row.line, "Status", cells[3], ACTOR_STATUSES, cells[0])

    summary: list[str] = []
    table_1 = _locate(report, tables, H_UC_SUMMARY, "Table 1 Use Case Summary Matrix")
    empty_table_1 = table_1 is not None and len(table_1.rows) == 1 and all(c == "—" for c in table_1.rows[0].cells)
    if table_1 is not None and not empty_table_1:
        for row in table_1.rows:
            if not _check_width(report, row.line, row.cells, len(H_UC_SUMMARY)):
                continue
            cells = [plain(c) for c in row.cells]
            if not _check_id(report, row.line, cells[0], UC_ID, "Use case ID"):
                continue
            if cells[0] in summary:
                report.error("DUPLICATE_ID", "Use case ID appears more than once in Table 1", line=row.line, entity=cells[0])
                continue
            summary.append(cells[0])
            if cells[2] not in actors:
                report.error(
                    "ACTOR_UNRESOLVED",
                    "Primary Actor is not registered in Table 1A",
                    line=row.line,
                    entity=cells[2],
                    expected="ACT-NN from Table 1A",
                    actual=cells[2],
                )

    uc_headings: dict[str, int] = {}
    for heading in headings:
        match = _UC_HEADING.match(heading.title) if heading.level == 3 else None
        if not match:
            continue
        if match.group(1) in uc_headings:
            report.error("DUPLICATE_DEFINITION", "Use case heading is defined more than once", line=heading.line, entity=match.group(1))
        uc_headings[match.group(1)] = heading.line
    for uc in summary:
        if uc not in uc_headings:
            report.error(
                "UC_SECTION_MISSING",
                "Table 1 row has no matching ### UC-NN section",
                entity=uc,
                expected=f"### {uc}: <Title>",
            )
    for uc, line in uc_headings.items():
        if uc not in summary:
            report.error("UC_ROW_MISSING", "### UC-NN section has no row in Table 1", line=line, entity=uc)

    flow_tables: dict[str, list[Table]] = {}
    for table in tables:
        if not table.header or table.header[0] != H_FLOWS[0]:
            continue
        owner = heading_before(headings, table.line, 3)
        match = _UC_HEADING.match(owner.title) if owner is not None and owner.level == 3 else None
        if match is None:
            report.error(
                "FLOW_TABLE_OUTSIDE_UC",
                "Table 1B must appear inside a ### UC-NN section",
                line=table.line,
            )
            continue
        flow_tables.setdefault(match.group(1), []).append(table)

    seen_flows: dict[str, int] = {}
    for uc in summary:
        tables_for_uc = flow_tables.get(uc, [])
        if not tables_for_uc:
            report.error("FLOW_TABLE_MISSING", "Use case has no Table 1B flow table", entity=uc)
            continue
        if len(tables_for_uc) > 1:
            report.error("FLOW_TABLE_DUPLICATE", "Use case has more than one Table 1B flow table", entity=uc, line=tables_for_uc[1].line)
        _validate_flow_table(report, uc, tables_for_uc[0], actors, seen_flows)

    return report.result()


def _validate_flow_table(
    report: _Report,
    uc: str,
    table: Table,
    actors: Mapping[str, int],
    seen_flows: dict[str, int],
) -> None:
    if table.header != H_FLOWS:
        report.error(
            "TABLE_HEADER_MISMATCH",
            f"Table 1B for {uc} does not match the canonical header",
            line=table.line,
            entity=uc,
            expected=" | ".join(H_FLOWS),
            actual=" | ".join(table.header),
        )
    uc_number = uc[3:]
    last_success = 0
    success_ids: set[str] = set()
    branches: list[tuple[int, str, str]] = []
    for row in table.rows:
        if not _check_width(report, row.line, row.cells, len(H_FLOWS)):
            continue
        cells = [plain(c) for c in row.cells]
        flow_id, flow_type, origin, actor = cells[0], cells[1], cells[2], cells[4]
        match = FLOW_ID.fullmatch(flow_id)
        if not match:
            report.error("ID_FORMAT", "Flow ID does not match SF/AF/EF/BE/BR-NN.N", line=row.line, entity=flow_id)
            continue
        prefix, parent, sequence = match.group(1), match.group(2), int(match.group(3))
        if parent != uc_number:
            report.error(
                "FLOW_PARENT_MISMATCH",
                f"Flow ID belongs to UC-{parent} but appears under {uc}",
                line=row.line,
                entity=flow_id,
                expected=f"UC-{uc_number}",
                actual=f"UC-{parent}",
            )
        if flow_id in seen_flows:
            report.error("DUPLICATE_ID", "Flow ID is defined more than once", line=row.line, entity=flow_id)
        seen_flows[flow_id] = row.line
        expected_type = FLOW_TYPE_BY_PREFIX[prefix]
        if flow_type != expected_type:
            report.error(
                "FLOW_TYPE_MISMATCH",
                "Flow type does not match its ID prefix",
                line=row.line,
                entity=flow_id,
                expected=expected_type,
                actual=flow_type,
            )
        if actor not in actors:
            report.error("ACTOR_UNRESOLVED", "Actor ID is not registered in Table 1A", line=row.line, entity=flow_id, expected="ACT-NN from Table 1A", actual=actor)
        if prefix == "SF":
            if origin != "—":
                report.error("ORIGIN_NOT_ALLOWED", "Success steps have no Origin Step; use —", line=row.line, entity=flow_id, expected="—", actual=origin)
            if sequence <= last_success:
                report.error("SF_ORDER", "Success steps must have increasing sequence numbers in journey order", line=row.line, entity=flow_id, expected=f"> SF-{uc_number}.{last_success}", actual=flow_id)
            last_success = sequence
            success_ids.add(flow_id)
        else:
            branches.append((row.line, flow_id, origin))

    for line, flow_id, origin in branches:
        if not FLOW_ID.fullmatch(origin) or not origin.startswith("SF-") or origin not in success_ids:
            report.error(
                "ORIGIN_INVALID",
                f"Origin Step must reference an SF row in {uc}",
                line=line,
                entity=flow_id,
                expected=f"SF-{uc_number}.N row in {uc}",
                actual=origin,
                hint="Reference the exact primary success step (SF-NN.N) this branch departs from",
            )


# ---------------------------------------------------------------------------
# Central plan (Table 2) and story DAG
# ---------------------------------------------------------------------------


def _parse_central(content: str, report: _Report) -> dict[str, dict[str, object]]:
    tables = parse_tables(content)
    table = _locate(report, tables, H_CENTRAL, "Table 2 Execution Order")
    stories: dict[str, dict[str, object]] = {}
    if table is None:
        return stories
    for index, row in enumerate(table.rows, start=1):
        if not _check_width(report, row.line, row.cells, len(H_CENTRAL)):
            continue
        cells = [plain(c) for c in row.cells]
        story_id = cells[1]
        if cells[0] != str(index):
            report.error("ORDER_SEQUENCE", "Order must be a sequential integer starting at 1", line=row.line, entity=story_id, expected=index, actual=cells[0])
        if not is_story_id(story_id):
            report.error("ID_FORMAT", "Story ID does not match a registered story format", line=row.line, entity=story_id)
            continue
        if story_id in stories:
            report.error("DUPLICATE_ID", "Story ID appears more than once in Table 2", line=row.line, entity=story_id)
            continue
        if not COMPONENT_CELL.fullmatch(row.cells[2]):
            report.error("COMPONENT_FORMAT", "Component must be 'CMP-NN — <Name>'", line=row.line, entity=story_id, actual=row.cells[2])
        if not re.fullmatch(r"[1-9]\d*", cells[3]):
            report.error("PHASE_FORMAT", "Phase must be a positive integer", line=row.line, entity=story_id, actual=cells[3])
        if cells[4] not in ("—", "Ops") and not all(UC_ID.fullmatch(t) for t in _split(row.cells[4])):
            report.error("ID_FORMAT", "Use Cases must be UC-NN values, — or Ops", line=row.line, entity=story_id, actual=cells[4])
        _check_enum(report, row.line, "Status", cells[9], STORY_STATUSES, story_id)
        stories[story_id] = {
            "order": index,
            "line": row.line,
            "use_cases": _split(row.cells[4]),
            "deps": _split(row.cells[6]),
            "unlocks": _split(row.cells[7]),
            "parallel": _split(row.cells[8]),
            "status": cells[9],
        }
    return stories


def _check_graph(
    report: _Report,
    deps: Mapping[str, list[str]],
    unlocks: Mapping[str, list[str]] | None = None,
    order: Mapping[str, int] | None = None,
    lines: Mapping[str, int] | None = None,
) -> None:
    lines = lines or {}
    for story, dependencies in deps.items():
        for dep in dependencies:
            if dep == story:
                report.error("DEPENDENCY_CYCLE", "Story depends on itself", line=lines.get(story), entity=story)
                continue
            if dep not in deps:
                report.error(
                    "UNKNOWN_DEPENDENCY",
                    "Dependency does not resolve to a story definition",
                    line=lines.get(story),
                    entity=story,
                    expected="defined story ID",
                    actual=dep,
                )
            elif order is not None and order[dep] >= order[story]:
                report.error(
                    "TOPOLOGICAL_ORDER",
                    "A predecessor must appear before its dependent story",
                    line=lines.get(story),
                    entity=story,
                    expected=f"{dep} before {story}",
                    actual=f"{dep} at {order[dep]}, {story} at {order[story]}",
                )

    indegree = {story: 0 for story in deps}
    children: dict[str, list[str]] = {story: [] for story in deps}
    for story, dependencies in deps.items():
        for dep in dependencies:
            if dep in deps and dep != story:
                indegree[story] += 1
                children[dep].append(story)
    ready = [story for story, count in indegree.items() if count == 0]
    resolved = 0
    while ready:
        node = ready.pop()
        resolved += 1
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if resolved != len(deps):
        cyclic = sorted(story for story, count in indegree.items() if count > 0)
        report.error("DEPENDENCY_CYCLE", "Dependency graph contains a cycle", entity=", ".join(cyclic), hint="Break the cycle; the dependency graph must be acyclic")

    if unlocks is not None:
        reverse: dict[str, set[str]] = {story: set() for story in deps}
        for story, dependencies in deps.items():
            for dep in dependencies:
                if dep in reverse:
                    reverse[dep].add(story)
        for story in deps:
            expected = sorted(reverse[story])
            actual = sorted(unlocks.get(story, []))
            if expected != actual:
                report.error(
                    "UNLOCKS_MISMATCH",
                    "Unlocks must equal the reverse dependency edges",
                    line=lines.get(story),
                    entity=story,
                    expected=", ".join(expected) or "—",
                    actual=", ".join(actual) or "—",
                    hint="Recompute Unlocks from the dependency graph",
                )


def _ancestors(deps: Mapping[str, list[str]]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def walk(story: str, stack: frozenset[str]) -> set[str]:
        if story in memo:
            return memo[story]
        out: set[str] = set()
        for dep in deps.get(story, []):
            if dep in deps and dep not in stack:
                out.add(dep)
                out |= walk(dep, stack | {story})
        memo[story] = out
        return out

    return {story: walk(story, frozenset()) for story in deps}


def validate_story_dag(
    deps: Mapping[str, Iterable[str]],
    unlocks: Mapping[str, Iterable[str]] | None = None,
    order: Mapping[str, int] | None = None,
) -> ValidationResult:
    report = _Report()
    normalized = {story: list(values) for story, values in deps.items()}
    normalized_unlocks = None if unlocks is None else {story: list(values) for story, values in unlocks.items()}
    _check_graph(report, normalized, normalized_unlocks, order)
    return report.result()


def validate_central_plan(content: str, file: str = "CENTRAL-PLAN-INDEX.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    _check_risks_and_milestones(report, parse_tables(content), parse_headings(content))
    stories = _parse_central(content, report)
    deps = {story: data["deps"] for story, data in stories.items()}
    unlocks = {story: data["unlocks"] for story, data in stories.items()}
    order = {story: data["order"] for story, data in stories.items()}
    lines = {story: data["line"] for story, data in stories.items()}
    _check_graph(report, deps, unlocks, order, lines)  # type: ignore[arg-type]
    ancestors = _ancestors(deps)  # type: ignore[arg-type]
    for story, data in stories.items():
        for parallel in data["parallel"]:  # type: ignore[union-attr]
            if parallel not in stories:
                report.error("UNKNOWN_REFERENCE", "Parallel With references an undefined story", line=data["line"], entity=story, actual=parallel)  # type: ignore[arg-type]
            elif parallel == story or parallel in ancestors[story] or story in ancestors[parallel]:
                report.error(
                    "PARALLEL_DEPENDENCY_CONFLICT",
                    "Parallel With lists a dependency ancestor or descendant",
                    line=data["line"],  # type: ignore[arg-type]
                    entity=story,
                    actual=parallel,
                )
    return report.result()


# ---------------------------------------------------------------------------
# Phase files (Table 3 story cards)
# ---------------------------------------------------------------------------

_STORY_HEADING = re.compile(r"(\S+?):\s+(\S.*)")


def _parse_phase(content: str, report: _Report) -> dict[str, dict[str, object]]:
    tables = parse_tables(content)
    headings = parse_headings(content)
    cards: dict[str, dict[str, object]] = {}
    for heading in headings:
        if heading.level != 2:
            continue
        match = _STORY_HEADING.fullmatch(heading.title)
        if not match or not is_story_id(match.group(1)):
            continue
        story_id, title = match.group(1), match.group(2)
        if story_id in cards:
            report.error("DUPLICATE_ID", "Story heading is defined more than once", line=heading.line, entity=story_id)
            continue
        card_table = next(
            (
                t
                for t in tables
                if t.header and t.header[0] == H_STORY_CARD[0] and heading_before(headings, t.line, 2) is heading
            ),
            None,
        )
        if card_table is None:
            report.error(
                "STORY_CARD_MISSING",
                "Story has no Table 3 story card",
                line=heading.line,
                entity=story_id,
                expected="| Field | Value | table with ten mandatory rows",
            )
            continue
        cards[story_id] = _validate_card(report, story_id, title, card_table)
    return cards


def _validate_card(report: _Report, story_id: str, title: str, table: Table) -> dict[str, object]:
    if table.header != H_STORY_CARD:
        report.error(
            "TABLE_HEADER_MISMATCH",
            f"Story card for {story_id} must use the Field | Value header",
            line=table.line,
            entity=story_id,
            expected=" | ".join(H_STORY_CARD),
            actual=" | ".join(table.header),
        )
    fields: list[str] = []
    values: dict[str, tuple[str, int]] = {}
    for row in table.rows:
        if not _check_width(report, row.line, row.cells, 2, story_id):
            continue
        name = plain(row.cells[0])
        fields.append(name)
        values[name] = (plain(row.cells[1]), row.line)
    if fields != list(STORY_CARD_FIELDS):
        report.error(
            "STORY_CARD_FIELDS",
            f"Story card {story_id} must contain exactly the ten mandatory rows in order",
            line=table.line,
            entity=story_id,
            expected=", ".join(STORY_CARD_FIELDS),
            actual=", ".join(fields),
        )

    def value(name: str) -> tuple[str, int]:
        return values.get(name, ("", table.line))

    use_case, line = value("Use Case ID")
    if use_case != "N/A" and not (_split(use_case) and all(UC_ID.fullmatch(t) for t in _split(use_case))):
        report.error("ID_FORMAT", "Use Case ID must be UC-NN values or N/A", line=line, entity=story_id, actual=use_case)
    flows, line = value("Flow IDs")
    if flows != "N/A" and not (_split(flows) and all(FLOW_ID.fullmatch(t) for t in _split(flows))):
        report.error("ID_FORMAT", "Flow IDs must be SF/AF/EF/BE/BR IDs or N/A", line=line, entity=story_id, actual=flows)
    if use_case == "N/A" and flows != "N/A":
        report.error("STORY_ORIGIN_MISMATCH", "Infrastructure story with no Use Case must have Flow IDs N/A", line=line, entity=story_id, actual=flows)
    card_title, line = value("Title")
    if card_title != title:
        report.error("STORY_TITLE_MISMATCH", "Story card Title must match the story heading", line=line, entity=story_id, expected=title, actual=card_title)
    status, line = value("Status")
    _check_enum(report, line, "Status", status, STORY_STATUSES, story_id)
    dependencies, line = value("Dependencies")
    if dependencies != "None" and not (_split(dependencies) and all(is_story_id(t) for t in _split(dependencies))):
        report.error("ID_FORMAT", "Dependencies must be story IDs or None", line=line, entity=story_id, actual=dependencies)
    unlocks, line = value("Unlocks")
    if unlocks != "—" and not (_split(unlocks) and all(is_story_id(t) for t in _split(unlocks))):
        report.error("ID_FORMAT", "Unlocks must be story IDs or —", line=line, entity=story_id, actual=unlocks)
    for name in ("What Will Be Done", "Acceptance Criteria", "Definition of Done"):
        text, line = value(name)
        if text in ("", "—"):
            report.error("STORY_FIELD_EMPTY", f"{name} must not be empty", line=line, entity=story_id)
    return {
        "id": story_id,
        "title": title,
        "line": table.line,
        "use_cases": _split(use_case),
        "flows": _split(flows),
        "deps": _split(dependencies),
        "unlocks": _split(unlocks),
        "status": status,
    }


def story_cards(content: str) -> dict[str, dict[str, object]]:
    """Parsed Table 3 story cards keyed by story ID. Findings are discarded; validate first."""

    return _parse_phase(content, _Report())


def validate_phase_file(content: str, file: str = "phase.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    _parse_phase(content, report)
    return report.result()


# ---------------------------------------------------------------------------
# Component master (Tables 4 and 5, CDD definitions)
# ---------------------------------------------------------------------------


def validate_component_master(content: str, file: str = "COMPONENT-MASTER.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    tables = parse_tables(content)
    headings = parse_headings(content)

    for heading in headings:
        if heading.level == 3 and re.match(r"CMP-\d{2}:", heading.title):
            report.error(
                "CMP_REDEFINED",
                "Component master must reference the TDD CMP definition, not redefine it",
                line=heading.line,
                entity=heading.title.split(":", 1)[0],
                hint="Remove the ### CMP heading and reference the canonical TDD anchor",
            )
    for heading in headings:
        if heading.level == 4 and re.match(r"DD-\d+:", heading.title):
            report.error(
                "COMPONENT_DD_DEFINITION",
                "Feature-level DD-N decisions belong in the TDD; component masters define CDD-N only",
                line=heading.line,
                entity=heading.title.split(":", 1)[0],
            )
    cdd_seen: set[str] = set()
    for heading in headings:
        match = re.match(r"(CDD-\d+):\s+\S", heading.title) if heading.level == 4 else None
        if match:
            if match.group(1) in cdd_seen:
                report.error("DUPLICATE_DEFINITION", "CDD ID is defined more than once", line=heading.line, entity=match.group(1))
            cdd_seen.add(match.group(1))

    index: dict[str, tuple[int, str]] = {}
    table_4 = _locate(report, tables, H_STORY_INDEX, "Table 4 Story Index")
    if table_4 is not None:
        for row in table_4.rows:
            if not _check_width(report, row.line, row.cells, len(H_STORY_INDEX)):
                continue
            cells = [plain(c) for c in row.cells]
            story_id = cells[0]
            if not is_story_id(story_id):
                report.error("ID_FORMAT", "Story ID does not match a registered story format", line=row.line, entity=story_id)
                continue
            if story_id in index:
                report.error("DUPLICATE_ID", "Story ID appears more than once in Table 4", line=row.line, entity=story_id)
                continue
            if not re.fullmatch(r"[1-9]\d*", cells[1]):
                report.error("PHASE_FORMAT", "Phase must be a positive integer", line=row.line, entity=story_id, actual=cells[1])
            if cells[3] not in ("—", "Ops") and not all(UC_ID.fullmatch(t) for t in _split(row.cells[3])):
                report.error("ID_FORMAT", "Use Cases must be UC-NN values, — or Ops", line=row.line, entity=story_id, actual=cells[3])
            _check_enum(report, row.line, "Status", cells[4], STORY_STATUSES, story_id)
            if cells[1].isdigit() and cells[4] in STORY_STATUSES:
                index[story_id] = (int(cells[1]), cells[4])

    table_5 = _locate(report, tables, H_DASHBOARD, "Table 5 Phase Status Dashboard")
    if table_5 is None:
        return report.result()
    expected: dict[int, dict[str, int]] = {}
    for phase, status in index.values():
        counts = expected.setdefault(phase, {name: 0 for name in STORY_STATUSES})
        counts[status] += 1
    status_columns = H_DASHBOARD[2:]
    phase_sums = {name: 0 for name in H_DASHBOARD[1:]}
    seen_phases: set[int] = set()
    total_seen = False
    for position, row in enumerate(table_5.rows):
        if not _check_width(report, row.line, row.cells, len(H_DASHBOARD)):
            continue
        cells = [plain(c) for c in row.cells]
        if cells[0] == "Total":
            total_seen = True
            if position != len(table_5.rows) - 1:
                report.error("TOTAL_ROW_POSITION", "The Total row must be the last row", line=row.line)
            continue
        if not re.fullmatch(r"[1-9]\d*", cells[0]):
            report.error("PHASE_FORMAT", "Phase must be a positive integer", line=row.line, actual=cells[0])
            continue
        try:
            numbers = [int(c) for c in cells[1:]]
        except ValueError:
            report.error("COUNT_FORMAT", "Dashboard counts must be non-negative integers", line=row.line, entity=cells[0])
            continue
        phase = int(cells[0])
        seen_phases.add(phase)
        if numbers[0] != sum(numbers[1:]):
            report.error("TOTAL_MISMATCH", "Total Stories must equal the sum of the five status counts", line=row.line, entity=cells[0], expected=sum(numbers[1:]), actual=numbers[0])
        counts = expected.get(phase, {name: 0 for name in STORY_STATUSES})
        for name, actual in zip(status_columns, numbers[1:], strict=False):
            if actual != counts[name]:
                report.error("COUNT_MISMATCH", "Dashboard count does not match the story index", line=row.line, entity=cells[0], expected=counts[name], actual=actual, hint="Recalculate counts from Table 4")
        if numbers[0] != sum(counts.values()):
            report.error("COUNT_MISMATCH", "Dashboard Total Stories does not match the story index", line=row.line, entity=cells[0], expected=sum(counts.values()), actual=numbers[0])
        for name, value in zip(H_DASHBOARD[1:], numbers, strict=False):
            phase_sums[name] += value
    for phase in sorted(set(expected) - seen_phases):
        report.error("PHASE_MISSING", "Phase in the story index has no Table 5 row", entity=str(phase))
    if not total_seen:
        report.error("TOTAL_ROW_MISSING", "Table 5 must end with a **Total** row")
    else:
        total_row = next(row for row in table_5.rows if plain(row.cells[0]) == "Total" and len(row.cells) == len(H_DASHBOARD))
        totals = [int(plain(c)) for c in total_row.cells[1:] if plain(c).isdigit()]
        for name, actual in zip(H_DASHBOARD[1:], totals, strict=False):
            if actual != phase_sums[name]:
                report.error("TOTAL_MISMATCH", "Total row must equal the sum of the phase rows", line=total_row.line, entity=name, expected=phase_sums[name], actual=actual)
    return report.result()


# ---------------------------------------------------------------------------
# Ledger (Table 14) and test plan (Table 15)
# ---------------------------------------------------------------------------


def validate_ledger(
    content: str,
    file: str = "LEDGER.md",
    decisions: Mapping[str, LedgerDecision] | None = None,
) -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    tables = parse_tables(content)
    headings = parse_headings(content)
    definitions = {
        match.group(1)
        for heading in headings
        if heading.level == 4 and (match := re.match(r"(LDG-\d{2}):\s+\S", heading.title))
    }
    table = _locate(report, tables, H_LEDGER, "Table 14 Coordinator Ledger")
    if table is None:
        return report.result()
    seen: set[str] = set()
    for row in table.rows:
        if not _check_width(report, row.line, row.cells, len(H_LEDGER)):
            continue
        cells = [plain(c) for c in row.cells]
        ledger_id = cells[0]
        if not _check_id(report, row.line, ledger_id, LDG_ID, "Ledger ID"):
            continue
        if ledger_id in seen:
            report.error("DUPLICATE_ID", "Ledger ID appears more than once", line=row.line, entity=ledger_id)
        seen.add(ledger_id)
        if ledger_id not in definitions:
            report.error("LEDGER_DEFINITION_MISSING", "Ledger ID has no #### LDG-NN definition heading", line=row.line, entity=ledger_id, expected=f"#### {ledger_id}: <Title>")
        origin = cells[1]
        if not (UC_ID.fullmatch(origin) or FLOW_ID.fullmatch(origin)):
            report.error("ID_FORMAT", "UC / Flow ID must reference one approved UC or flow", line=row.line, entity=ledger_id, actual=origin)
        status = cells[2]
        _check_enum(report, row.line, "Deviation Status", status, LEDGER_STATUSES, ledger_id)
        if cells[3] != "—" and not (is_story_id(cells[3])):
            report.error("ID_FORMAT", "Linked Story ID must be a story ID or —", line=row.line, entity=ledger_id, actual=cells[3])
        if status in LEDGER_APPROVAL_REQUIRED and cells[7] in NO_APPROVER:
            report.error(
                "APPROVAL_REQUIRED",
                f"{status} entries require a named human approver",
                line=row.line,
                entity=ledger_id,
                expected="approving human identity",
                actual=cells[7] or "(empty)",
                hint="Record approval through the human-only `dridh ledger approve` command",
            )
        if status in LEDGER_APPROVAL_REQUIRED:
            _check_ledger_approval(report, row.line, ledger_id, content, cells[7], decisions)
    for heading in headings:
        if heading.level == 4:
            match = re.match(r"(LDG-\d{2}):\s+\S", heading.title)
            if match and match.group(1) not in seen:
                report.error("LEDGER_ORPHAN_DEFINITION", "#### LDG-NN definition has no Table 14 row", line=heading.line, entity=match.group(1))
    return report.result()


def validate_test_plan(content: str, file: str = "TEST-PLAN.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    tables = parse_tables(content)
    table = _locate(report, tables, H_TEST, "Table 15 Test Phase Plan")
    if table is None:
        return report.result()
    seen: set[str] = set()
    for row in table.rows:
        if not _check_width(report, row.line, row.cells, len(H_TEST)):
            continue
        cells = [plain(c) for c in row.cells]
        phase_id = cells[0]
        if not _check_id(report, row.line, phase_id, TEST_PHASE_ID, "Test Phase ID"):
            continue
        if phase_id in seen:
            report.error("DUPLICATE_ID", "Test Phase ID appears more than once", line=row.line, entity=phase_id)
        seen.add(phase_id)
        if cells[2] != "N/A" and not _all_ids_valid(row.cells[2], ["UC", "SF", "AF", "EF", "BE", "BR"], (UC_ID, FLOW_ID)):
            report.error("ID_FORMAT", "Use Case / Flow IDs must reference UC or flow IDs, or N/A", line=row.line, entity=phase_id, actual=cells[2])
        if cells[3] != "N/A" and not _all_ids_valid(row.cells[3], ["CMP"], (CMP_ID,)):
            report.error("ID_FORMAT", "Component IDs must reference CMP-NN IDs, or N/A", line=row.line, entity=phase_id, actual=cells[3])
        _check_enum(report, row.line, "Dependency Mode", cells[4], DEPENDENCY_MODES, phase_id)
        _check_enum(report, row.line, "Status", cells[8], TEST_STATUSES, phase_id)
        for name, index in (("Execution Command", 6), ("Evidence", 7)):
            if cells[index] in ("", "—"):
                report.error("STORY_FIELD_EMPTY", f"{name} must not be empty", line=row.line, entity=phase_id)
        if cells[4] == "WIREMOCK_EXCEPTION":
            environment = cells[5].lower()
            missing = [name for name in WIREMOCK_REQUIRED_FIELDS if name not in environment]
            if missing:
                report.error(
                    "WIREMOCK_JUSTIFICATION_INCOMPLETE",
                    "WIREMOCK_EXCEPTION rows must document the missing dependency, contract, owner and expiry",
                    line=row.line,
                    entity=phase_id,
                    expected=", ".join(WIREMOCK_REQUIRED_FIELDS),
                    actual=", ".join(missing) + " missing",
                )
    return report.result()


# ---------------------------------------------------------------------------
# TDD sanitisation and validation
# ---------------------------------------------------------------------------


def sanitize_tdd(content: str) -> tuple[str, list[dict[str, object]]]:
    """Remove HTML comments and instructional <aside> blocks, reporting each removal."""
    actions: list[dict[str, object]] = []
    text = content
    for pattern, action in ((_COMMENT, "remove_html_comment"), (_ASIDE, "remove_aside_callout")):
        for match in pattern.finditer(text):
            actions.append({"action": action, "line": text.count("\n", 0, match.start()) + 1})
        text = pattern.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, actions


def validate_tdd(content: str, file: str = "tdd.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    lines = content.splitlines()
    tables = parse_tables(content)
    headings = parse_headings(content)

    fenced = False
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        # Inside fenced code `-->` is content (a Mermaid arrow, a shell redirect), not the end of an HTML comment.
        if not fenced and ("<!--" in line or "-->" in line):
            report.error("TDD_HTML_COMMENT", "HTML comments must be removed from generated TDDs", line=number, hint="Run sanitize_tdd before validation")
        if re.search(r"</?aside\b", line, re.IGNORECASE):
            report.error("TDD_ASIDE", "Instructional <aside> callouts must be removed", line=number)
        for phrase in TDD_BOILERPLATE:
            if phrase in line:
                report.error("TDD_BOILERPLATE", "Known template instruction remains in the TDD", line=number, actual=phrase, hint="Replace the illustrative example with real content or remove it")
        for placeholder in TDD_PLACEHOLDERS:
            if placeholder in line:
                report.error("TDD_PLACEHOLDER", "Unresolved template placeholder remains", line=number, actual=placeholder)

    level2 = [h for h in headings if h.level == 2]
    titles = [h.title for h in level2]
    positions: list[int] = []
    for name in TDD_SECTIONS:
        if name not in titles:
            report.error("TDD_MISSING_SECTION", f"Missing required section ## {name}", expected=f"## {name}", hint="Keep every template section; mark unused sections N/A")
            continue
        positions.append(titles.index(name))
    if positions != sorted(positions):
        report.error("TDD_SECTION_ORDER", "Template sections are out of canonical order")
    for index, heading in enumerate(level2):
        if heading.title not in TDD_SECTIONS:
            continue
        end = level2[index + 1].line if index + 1 < len(level2) else len(lines) + 1
        body = [line for line in lines[heading.line : end - 1] if line.strip() and not line.lstrip().startswith("#")]
        if not body:
            report.error("TDD_EMPTY_SECTION", f"Section {heading.title} is empty", line=heading.line, hint="Mark unused sections as N/A with a reason")

    component_defs: set[str] = set()
    for heading in headings:
        if heading.level == 3 and (match := re.match(r"(CMP-\d{2}):\s+\S", heading.title)):
            if match.group(1) in component_defs:
                report.error("DUPLICATE_DEFINITION", "CMP definition is defined more than once", line=heading.line, entity=match.group(1))
            component_defs.add(match.group(1))
    decision_defs: set[str] = set()
    for heading in headings:
        if heading.level == 4 and (match := re.match(r"(DD-\d+):\s+\S", heading.title)):
            if match.group(1) in decision_defs:
                report.error("DUPLICATE_DEFINITION", "DD definition is defined more than once", line=heading.line, entity=match.group(1))
            decision_defs.add(match.group(1))
        if heading.level == 4 and re.match(r"CDD-\d+:", heading.title):
            report.error("TDD_CDD_IN_TDD", "Component-level CDD-N decisions belong in COMPONENT-MASTER.md", line=heading.line)
    if not component_defs:
        report.error("TDD_NO_COMPONENT_DEFINITION", "The TDD must define at least one ### CMP-NN component")

    table_13 = _locate(report, tables, H_TRACE, "Table 13 Use Case-to-Design Traceability Matrix")
    if table_13 is not None:
        owner = heading_before(headings, table_13.line, 2)
        if owner is None or owner.title != "Design Details":
            report.error("TABLE_SECTION", "Table 13 must appear under ## Design Details", line=table_13.line)
        for row in table_13.rows:
            if not _check_width(report, row.line, row.cells, len(H_TRACE)):
                continue
            cells = [plain(c) for c in row.cells]
            if re.search(r"\bCDD-", " ".join(row.cells)):
                report.error(
                    "TDD_CDD_REFERENCE",
                    "Table 13 is feature-level; it must reference DD-N, not component-level CDD-N",
                    line=row.line,
                    entity=cells[0],
                )
            if not UC_ID.fullmatch(cells[0]):
                report.error("ID_FORMAT", "UC ID must be UC-NN", line=row.line, entity=cells[0])
            if cells[1] != "N/A":
                flows = _tokens(row.cells[1], ["SF", "AF", "EF", "BE", "BR"])
                if not flows or any(not FLOW_ID.fullmatch(f) for f in flows):
                    report.error("ID_FORMAT", "Flow IDs must be SF/AF/EF/BE/BR-NN.N IDs or N/A", line=row.line, entity=cells[0], actual=cells[1])
            components = _tokens(row.cells[2], ["CMP"])
            if not components:
                report.error("ID_FORMAT", "Catering Component(s) must reference CMP-NN", line=row.line, entity=cells[0], actual=cells[2])
            for component in components:
                if component not in component_defs:
                    report.error("TDD_UNRESOLVED_COMPONENT", "Component is not defined in this TDD", line=row.line, entity=component, expected="### CMP-NN heading in this TDD", actual=component)
            if cells[4] != "N/A":
                decisions = re.findall(r"(?<![A-Z])DD-\d+", row.cells[4])
                if not decisions:
                    report.error("ID_FORMAT", "Design Decision must reference DD-N or N/A", line=row.line, entity=cells[0], actual=cells[4])
                for decision in decisions:
                    if decision not in decision_defs:
                        report.error("TDD_UNRESOLVED_DECISION", "Design decision is not defined in this TDD", line=row.line, entity=decision, expected="#### DD-N heading in this TDD", actual=decision)
    return report.result()


# ---------------------------------------------------------------------------
# Revision binding, cross-references and dispatch
# ---------------------------------------------------------------------------


def content_revision(text: str) -> str:
    normalized = text.replace("\r\n", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_document_sync(content: str, expected_revision: str, file: str | None = None) -> ValidationResult:
    report = _Report(file)
    match = _SOURCE_REVISION.search(content)
    if match is None:
        report.error("DOC_REVISION_MISSING", "Document has no Source Revision metadata", expected=expected_revision, hint="Record the approved source revision or content hash")
    else:
        actual = match.group(1).strip("`* ")
        if actual != expected_revision:
            report.error(
                "DOC_STALE_REVISION",
                "Source Revision does not match the approved source; regenerate or re-review this document",
                expected=expected_revision,
                actual=actual,
            )
    return report.result()


def classify_document(name: str, content: str = "") -> str | None:
    lower = Path(name).name.lower()
    if lower == "ledger.md":
        return "ledger"
    if lower == "test-plan.md":
        return "test_plan"
    if lower == "central-plan-index.md":
        return "central_plan"
    if lower == "component-master.md":
        return "component_master"
    if lower == "coordinator.md":
        return "coordinator"
    if lower == "qa-log.md":
        return "qa_log"
    if "use-cases" in lower:
        return "use_cases"
    if lower.endswith("tdd.md"):
        return "tdd"
    if "phase" in lower and lower.endswith(".md"):
        return "phase_file"
    headers = {table.header for table in parse_tables(content)}
    if H_LEDGER in headers:
        return "ledger"
    if H_TEST in headers:
        return "test_plan"
    if H_CENTRAL in headers:
        return "central_plan"
    if H_FLOWS in headers:
        return "use_cases"
    if H_TRACE in headers:
        return "tdd"
    if H_STORY_INDEX in headers:
        return "component_master"
    if H_PIPELINE in headers:
        return "coordinator"
    if H_QA in headers:
        return "qa_log"
    return None


def _check_metadata(report: _Report, content: str) -> None:
    match = _STANDARDS_VERSION.search(content)
    if match is None:
        report.error("METADATA_MISSING", "Missing Standards Version metadata", expected=f"**Standards Version:** {STANDARDS_VERSION}")
    elif match.group(1) != STANDARDS_VERSION:
        report.error(
            "UNSUPPORTED_STANDARDS_VERSION",
            "Document was generated with an unsupported standards version",
            expected=STANDARDS_VERSION,
            actual=match.group(1),
            hint="Migrate the document or add an explicit compatibility path",
        )
        return

    # Hash metadata is optional for backwards compatibility with existing 2.1.0
    # documents. When supplied by a generator, it is verified against the runtime bundle.
    hash_fields = {
        "Standards SHA256": "standards_sha256",
        "Template SHA256": "template_sha256",
    }
    if not any(re.search(rf"\*\*{re.escape(label)}:\*\*\s*\S+", content) for label in hash_fields):
        return
    try:
        bundle = load_contract_bundle(STANDARDS_VERSION)
    except ContractBundleError as exc:
        report.error("CONTRACT_BUNDLE_UNAVAILABLE", str(exc), hint="Reinstall Dridh with the matching contract package")
        return
    template_version = re.search(r"\*\*Template Version:\*\*\s*(\S+)", content)
    if template_version is not None and template_version.group(1) != bundle.version:
        report.error("CONTRACT_VERSION_MISMATCH", "Template Version does not match the packaged contract bundle", expected=bundle.version, actual=template_version.group(1))
    for label, attribute in hash_fields.items():
        field_match = re.search(rf"\*\*{re.escape(label)}:\*\*\s*(\S+)", content)
        if field_match is None:
            continue
        actual = field_match.group(1)
        expected = getattr(bundle, attribute)
        if not re.fullmatch(r"[0-9a-f]{64}", actual) or actual != expected:
            report.error(
                "CONTRACT_HASH_MISMATCH",
                f"{label} does not match the installed versioned contract bundle",
                expected=expected,
                actual=actual,
                hint="Regenerate metadata using the packaged contract metadata helper",
            )
    source_hash = re.search(r"\*\*Source SHA256:\*\*\s*(\S+)", content)
    if source_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", source_hash.group(1)):
        report.error("METADATA_INVALID", "Source SHA256 must be a lowercase 64-character digest", expected="64 lowercase hexadecimal characters", actual=source_hash.group(1))


_VALIDATORS: dict[str, Callable[..., ValidationResult]] = {}


def validate_document(
    name: str,
    content: str,
    *,
    expected_revision: str | None = None,
    ledger_decisions: Mapping[str, LedgerDecision] | None = None,
) -> ValidationResult:
    report = _Report(name)
    kind = classify_document(name, content)
    if kind is None:
        report.error(
            "UNSUPPORTED_DOCUMENT",
            f"{name} is not a supported Dridh document type",
            hint="Use a canonical filename or a canonical table schema; unknown documents are never accepted",
        )
        return report.result()
    _check_metadata(report, content)
    findings = list(report.findings)
    if kind == "ledger":
        findings += validate_ledger(content, file=name, decisions=ledger_decisions).findings
    else:
        findings += _VALIDATORS[kind](content, file=name).findings
    if expected_revision is not None:
        findings += validate_document_sync(content, expected_revision, file=name).findings
    return ValidationResult(findings)


_XREF_PROVIDERS = {
    "ACT": {"use_cases"},
    "UC": {"use_cases"},
    "FLOW": {"use_cases"},
    "CMP": {"tdd"},
    "DD": {"tdd"},
    "RISK": {"tdd", "central_plan"},
    "CDD": {"component_master"},
    "STORY": {"central_plan", "phase_file", "component_master"},
}
_UNIQUE_CATEGORIES = ("ACT", "UC", "FLOW", "CMP", "DD", "RISK", "CDD")
_XREF_LABELS = {
    "ACT": "the Use Case Actors Registry",
    "UC": "the Use Case document",
    "FLOW": "the Use Case document (Table 1B)",
    "CMP": "the TDD component definitions",
    "DD": "the TDD design decisions",
    "RISK": "the TDD risk definitions or the Central Plan risk definitions",
    "CDD": "the component master component design decisions",
    "STORY": "the central plan, phase files or component master",
}


def _definitions(kind: str | None, content: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {key: [] for key in _XREF_PROVIDERS}
    if kind is None:
        return result
    tables = parse_tables(content)
    headings = parse_headings(content)
    if kind == "use_cases":
        for table in tables:
            if table.header == H_ACTORS:
                result["ACT"] += [plain(r.cells[0]) for r in table.rows if ACT_ID.fullmatch(plain(r.cells[0]))]
            elif table.header == H_UC_SUMMARY:
                result["UC"] += [plain(r.cells[0]) for r in table.rows if UC_ID.fullmatch(plain(r.cells[0]))]
            elif table.header and table.header[0] == H_FLOWS[0]:
                result["FLOW"] += [plain(r.cells[0]) for r in table.rows if FLOW_ID.fullmatch(plain(r.cells[0]))]
    elif kind == "tdd":
        for heading in headings:
            if heading.level == 3 and (match := re.match(r"(CMP-\d{2}):\s+\S", heading.title)):
                result["CMP"].append(match.group(1))
            if heading.level == 3 and (match := re.match(r"(R\d+):\s+\S", heading.title)):
                result["RISK"].append(match.group(1))
            if heading.level == 4 and (match := re.match(r"(DD-\d+):\s+\S", heading.title)):
                result["DD"].append(match.group(1))
    elif kind == "central_plan":
        for table in tables:
            if table.header == H_CENTRAL:
                result["STORY"] += [plain(r.cells[1]) for r in table.rows if len(r.cells) == len(H_CENTRAL)]
        for heading in headings:
            if heading.level == 3 and (match := re.match(r"(R\d+):\s+\S", heading.title)):
                result["RISK"].append(match.group(1))
    elif kind == "component_master":
        for table in tables:
            if table.header == H_STORY_INDEX:
                result["STORY"] += [plain(r.cells[0]) for r in table.rows if len(r.cells) == len(H_STORY_INDEX)]
        for heading in headings:
            if heading.level == 4 and (match := re.match(r"(CDD-\d+):\s+\S", heading.title)):
                result["CDD"].append(match.group(1))
    elif kind == "phase_file":
        result["STORY"] += list(_parse_phase(content, _Report()))
    return result


def _references(kind: str | None, content: str) -> list[tuple[str, str, int, str]]:
    refs: list[tuple[str, str, int, str]] = []
    if kind is None:
        return refs
    tables = parse_tables(content)

    def add(category: str, line: int, field: str, tokens: Iterable[str]) -> None:
        for token in tokens:
            refs.append((category, token, line, field))

    if kind == "tdd":
        for table in tables:
            if table.header != H_TRACE:
                continue
            for row in table.rows:
                if len(row.cells) != len(H_TRACE):
                    continue
                add("UC", row.line, "UC ID", re.findall(r"\bUC-\w+", row.cells[0]))
                add("FLOW", row.line, "Flow IDs", re.findall(r"\b(?:SF|AF|EF|BE|BR)-[\w.]+", row.cells[1]))
                add("CMP", row.line, "Catering Component(s)", re.findall(r"\bCMP-[\w.]+", row.cells[2]))
                add("DD", row.line, "Design Decision (DD-N)", re.findall(r"(?<![A-Z])DD-[\w.]+", row.cells[4]))
    elif kind == "central_plan":
        for table in tables:
            if table.header == H_CENTRAL:
                for row in table.rows:
                    if len(row.cells) != len(H_CENTRAL):
                        continue
                    add("UC", row.line, "Use Cases", re.findall(r"\bUC-\w+", row.cells[4]))
                    add("CMP", row.line, "Component", re.findall(r"\bCMP-[\w.]+", row.cells[2]))
                    for index, name in ((6, "Dependencies"), (7, "Unlocks"), (8, "Parallel With")):
                        add("STORY", row.line, name, _split(row.cells[index]))
            elif table.header == H_RISKS:
                for row in table.rows:
                    if len(row.cells) == len(H_RISKS):
                        add("RISK", row.line, "Risk register #", _split(row.cells[0]))
    elif kind == "phase_file":
        for card in _parse_phase(content, _Report()).values():
            line = int(card["line"])  # type: ignore[arg-type]
            add("UC", line, "Use Case ID", card["use_cases"])  # type: ignore[arg-type]
            add("FLOW", line, "Flow IDs", card["flows"])  # type: ignore[arg-type]
            add("STORY", line, "Dependencies", card["deps"])  # type: ignore[arg-type]
            add("STORY", line, "Unlocks", card["unlocks"])  # type: ignore[arg-type]
    elif kind == "component_master":
        for table in tables:
            if table.header != H_STORY_INDEX:
                continue
            for row in table.rows:
                if len(row.cells) == len(H_STORY_INDEX):
                    add("UC", row.line, "Use Cases", re.findall(r"\bUC-\w+", row.cells[3]))
    elif kind == "ledger":
        for table in tables:
            if table.header != H_LEDGER:
                continue
            for row in table.rows:
                if len(row.cells) != len(H_LEDGER):
                    continue
                add("UC", row.line, "UC / Flow ID", re.findall(r"\bUC-\w+", row.cells[1]))
                add("FLOW", row.line, "UC / Flow ID", re.findall(r"\b(?:SF|AF|EF|BE|BR)-[\w.]+", row.cells[1]))
                add("STORY", row.line, "Linked Story ID", _split(row.cells[3]))
    elif kind == "test_plan":
        for table in tables:
            if table.header != H_TEST:
                continue
            for row in table.rows:
                if len(row.cells) != len(H_TEST):
                    continue
                add("UC", row.line, "Use Case / Flow IDs", re.findall(r"\bUC-\w+", row.cells[2]))
                add("FLOW", row.line, "Use Case / Flow IDs", re.findall(r"\b(?:SF|AF|EF|BE|BR)-[\w.]+", row.cells[2]))
                add("CMP", row.line, "Component IDs", re.findall(r"\bCMP-[\w.]+", row.cells[3]))
    return refs


def validate_cross_references(documents: Mapping[str, str]) -> ValidationResult:
    """Resolve each reference against the document that owns its definition.

    Stage-aware: a reference is checked only when its owning document was supplied,
    so a Stage 2 TDD validates against the use cases without Stage 3 files existing.
    A document of unknown type is an error, never silently skipped.
    """
    report = _Report()
    kinds = {name: classify_document(name, content) for name, content in documents.items()}
    for name, kind in kinds.items():
        if kind is None:
            report.error("UNSUPPORTED_DOCUMENT", f"{name} is not a supported Dridh document type", file=name)
    provided = {kind for kind in kinds.values() if kind is not None}

    universe: dict[str, set[str]] = {key: set() for key in _XREF_PROVIDERS}
    owners: dict[tuple[str, str], list[str]] = {}
    for name, content in documents.items():
        for category, ids in _definitions(kinds[name], content).items():
            universe[category] |= set(ids)
            for identifier in set(ids):
                owners.setdefault((category, identifier), []).append(name)
    for (category, identifier), files in sorted(owners.items()):
        if category in _UNIQUE_CATEGORIES and len(files) > 1:
            report.error(
                "DUPLICATE_DEFINITION",
                f"{identifier} is defined in more than one document: {', '.join(sorted(files))}",
                file=sorted(files)[0],
                entity=identifier,
                hint="Keep one canonical definition and reference it from the other document",
            )

    covered_ucs: set[str] = set()
    tdd_name = next((name for name, kind in kinds.items() if kind == "tdd"), None)
    for name, content in documents.items():
        for category, token, line, field in _references(kinds[name], content):
            if not (_XREF_PROVIDERS[category] & provided):
                continue
            if category == "UC" and kinds[name] == "tdd" and field == "UC ID":
                covered_ucs.add(token)
            if token not in universe[category]:
                report.error(
                    "XREF_MISSING_TARGET",
                    f"{name} line {line} {field} references {token}, which is not defined in {_XREF_LABELS[category]}",
                    file=name,
                    line=line,
                    entity=token,
                    expected=f"{category} definition",
                    actual=token,
                )

    if "use_cases" in provided and "tdd" in provided:
        for uc in sorted(universe["UC"] - covered_ucs):
            report.error(
                "XREF_UC_UNCOVERED",
                "Approved use case is not addressed by the Table 13 traceability matrix",
                file=tdd_name,
                entity=uc,
                hint="Add a Table 13 row for this UC or record its treatment in the Ledger",
            )
    return report.result()


def validate_file(
    path: Path | str,
    *,
    ledger_decisions: Mapping[str, LedgerDecision] | None = None,
) -> ValidationResult:
    target = Path(path)
    if not target.exists():
        return ValidationResult([Finding("FILE_NOT_FOUND", f"File not found: {target}", file=str(target))])
    return validate_document(target.name, target.read_text(encoding="utf-8"), ledger_decisions=ledger_decisions)


def validate_document_collection(
    documents: Mapping[str, str],
    *,
    ledger_decisions: Mapping[str, LedgerDecision] | None = None,
) -> ValidationResult:
    findings: list[Finding] = []
    for name, content in documents.items():
        findings += validate_document(name, content, ledger_decisions=ledger_decisions).findings
    findings += [f for f in validate_cross_references(documents).findings if f.code != "UNSUPPORTED_DOCUMENT"]
    return ValidationResult(findings)


# ---------------------------------------------------------------------------
# Coordinator tables (6, 7, 8, 12), Q&A log (11), risks and milestones (9, 10)
# ---------------------------------------------------------------------------

H_PIPELINE = ("#", "Stage", "Agent", "Status", "Output", "Date Completed", "Reviewed By")
H_DESIGN_ARTIFACTS = ("Document", "Path", "Status", "Lines", "Key Metrics")
H_IMPLEMENTATION = ("Component", "Phase", "Branch", "PR #", "PR Status", "Build Status", "Stories Done", "Stories Total")
H_REVIEW_QUALITY = ("Document", "Readiness", "Checklist", "Sections", "Blockers", "Last Reviewer")
H_QA = ("#", "Question", "Asked In", "Answer", "Source", "Impact")
H_RISKS = ("#", "Risk", "Likelihood", "Impact", "Mitigation", "Owner")
H_MILESTONES = ("#", "Milestone", "Criteria", "Target", "Status")
PIPELINE_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED", "REVIEWED", "REVIEWED (STALE)", "N/A")
DOCUMENT_REVIEW_STATUSES = ("DRAFT", "READY_FOR_REVIEW", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED", "STALE")
PR_STATUSES = ("NOT_CREATED", "DRAFT", "OPEN", "IN_REVIEW", "APPROVED", "MERGED", "CLOSED")
BUILD_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "PASSED", "FAILED", "CANCELLED", "UNKNOWN")
MILESTONE_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED")
RISK_SCALE = ("Low", "Medium", "High")
PIPELINE_STAGE_COUNT = 10
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BACKTICK_PATH = re.compile(r"`([^`]+)`")
_RATIO_100 = re.compile(r"(\d{1,3})/100")
_RATIO = re.compile(r"(\d+)/(\d+)")


def _is_relative_path(value: str) -> bool:
    return bool(value) and not value.startswith("/") and ".." not in Path(value).parts


def _check_date_or_dash(report: _Report, line: int, field: str, value: str, entity: str | None) -> None:
    if value != "—" and not _DATE.fullmatch(value):
        report.error("DATE_FORMAT", f"{field} must be YYYY-MM-DD or —", line=line, entity=entity, expected="YYYY-MM-DD", actual=value)


def _check_count(report: _Report, line: int, field: str, value: str, entity: str | None) -> int | None:
    if not value.isdigit():
        report.error("COUNT_FORMAT", f"{field} must be a non-negative integer", line=line, entity=entity, actual=value)
        return None
    return int(value)


def validate_coordinator(content: str, file: str = "COORDINATOR.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    tables = parse_tables(content)

    table_6 = _locate(report, tables, H_PIPELINE, "Table 6 Pipeline Progress")
    if table_6 is not None:
        for index, row in enumerate(table_6.rows, start=1):
            if not _check_width(report, row.line, row.cells, len(H_PIPELINE)):
                continue
            cells = [plain(c) for c in row.cells]
            if cells[0] != str(index):
                report.error("ORDER_SEQUENCE", "Pipeline stage numbers must run sequentially from 1", line=row.line, entity=cells[1], expected=index, actual=cells[0])
            if not cells[1] or not cells[2]:
                report.error("STORY_FIELD_EMPTY", "Stage and Agent are required", line=row.line, entity=cells[0])
            _check_enum(report, row.line, "Status", cells[3], PIPELINE_STATUSES, cells[0])
            output = row.cells[4].strip()
            if output != "—":
                match = _BACKTICK_PATH.fullmatch(output)
                if match is None or not _is_relative_path(match.group(1)):
                    report.error("ID_FORMAT", "Output must be a backtick-wrapped workspace-relative path or —", line=row.line, entity=cells[0], actual=output)
            _check_date_or_dash(report, row.line, "Date Completed", cells[5], cells[0])
            if not cells[6]:
                report.error("STORY_FIELD_EMPTY", "Reviewed By must be a reviewer name or —", line=row.line, entity=cells[0])
        if len(table_6.rows) != PIPELINE_STAGE_COUNT:
            report.error("PIPELINE_STAGE_COUNT", "Table 6 must list pipeline stages 1 through 10", expected=PIPELINE_STAGE_COUNT, actual=len(table_6.rows))

    table_7 = _locate(report, tables, H_DESIGN_ARTIFACTS, "Table 7 Design Artifacts")
    if table_7 is not None:
        for row in table_7.rows:
            if not _check_width(report, row.line, row.cells, len(H_DESIGN_ARTIFACTS)):
                continue
            cells = [plain(c) for c in row.cells]
            if not cells[0].endswith(".md"):
                report.error("ID_FORMAT", "Document must be the canonical filename", line=row.line, entity=cells[0], actual=cells[0])
            if not _is_relative_path(cells[1]):
                report.error("ID_FORMAT", "Path must be a workspace-relative path", line=row.line, entity=cells[0], actual=cells[1])
            if cells[2] not in PIPELINE_STATUSES + DOCUMENT_REVIEW_STATUSES:
                report.error("ENUM_INVALID", "Status must be a Document Review or Pipeline Progress value", line=row.line, entity=cells[0], expected=" | ".join(PIPELINE_STATUSES + DOCUMENT_REVIEW_STATUSES), actual=cells[2])
            if cells[3] != "—":
                _check_count(report, row.line, "Lines", cells[3], cells[0])
            if cells[4] in ("", "—"):
                report.error("STORY_FIELD_EMPTY", "Key Metrics must state factual counts", line=row.line, entity=cells[0])

    table_8 = _locate(report, tables, H_IMPLEMENTATION, "Table 8 Implementation Progress")
    if table_8 is not None:
        for row in table_8.rows:
            if not _check_width(report, row.line, row.cells, len(H_IMPLEMENTATION)):
                continue
            cells = [plain(c) for c in row.cells]
            if not COMPONENT_CELL.fullmatch(row.cells[0]):
                report.error("COMPONENT_FORMAT", "Component must be 'CMP-NN — <Name>'", line=row.line, entity=cells[0], actual=row.cells[0])
            if cells[1] != "—" and not re.fullmatch(r"[1-9]\d*", cells[1]):
                report.error("PHASE_FORMAT", "Phase must be a positive integer or —", line=row.line, entity=cells[0], actual=cells[1])
            if not cells[2]:
                report.error("STORY_FIELD_EMPTY", "Branch must be a branch name or —", line=row.line, entity=cells[0])
            if cells[3] != "—" and not cells[3].isdigit():
                report.error("ID_FORMAT", "PR # must be a GitHub PR number or —", line=row.line, entity=cells[0], actual=cells[3])
            _check_enum(report, row.line, "PR Status", cells[4], PR_STATUSES, cells[0])
            _check_enum(report, row.line, "Build Status", cells[5], BUILD_STATUSES, cells[0])
            done = _check_count(report, row.line, "Stories Done", cells[6], cells[0])
            total = _check_count(report, row.line, "Stories Total", cells[7], cells[0])
            if done is not None and total is not None and done > total:
                report.error("COUNT_MISMATCH", "Stories Done must not exceed Stories Total", line=row.line, entity=cells[0], expected=f"<= {total}", actual=done)

    table_12 = _locate(report, tables, H_REVIEW_QUALITY, "Table 12 Review Quality")
    if table_12 is not None:
        for row in table_12.rows:
            if not _check_width(report, row.line, row.cells, len(H_REVIEW_QUALITY)):
                continue
            cells = [plain(c) for c in row.cells]
            if not cells[0].endswith(".md"):
                report.error("ID_FORMAT", "Document must be the canonical filename", line=row.line, entity=cells[0], actual=cells[0])
            readiness = _RATIO_100.fullmatch(cells[1])
            if readiness is None or int(readiness.group(1)) > 100:
                report.error("ID_FORMAT", "Readiness must be N/100 with N from 0 to 100", line=row.line, entity=cells[0], actual=cells[1])
            for field, value in (("Checklist", cells[2]), ("Sections", cells[3])):
                ratio = _RATIO.fullmatch(value)
                if ratio is None or int(ratio.group(1)) > int(ratio.group(2)):
                    report.error("COUNT_MISMATCH", f"{field} must be N/M with N not above M", line=row.line, entity=cells[0], actual=value)
            _check_count(report, row.line, "Blockers", cells[4], cells[0])
            if not cells[5]:
                report.error("STORY_FIELD_EMPTY", "Last Reviewer must be a name or —", line=row.line, entity=cells[0])
    return report.result()


def validate_qa_log(content: str, file: str = "QA-LOG.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    table = _locate(report, parse_tables(content), H_QA, "Table 11 Q&A Log")
    if table is None:
        return report.result()
    if not table.rows:
        report.error(
            "QA_EMPTY_TABLE",
            "Table 11 has no rows; add a single row of — in every column when no questions apply",
            line=table.line,
            expected="one row of —",
        )
        return report.result()
    if len(table.rows) == 1 and all(cell == "—" for cell in table.rows[0].cells):
        return report.result()
    for index, row in enumerate(table.rows, start=1):
        if not _check_width(report, row.line, row.cells, len(H_QA)):
            continue
        cells = [plain(c) for c in row.cells]
        if cells[0] != str(index):
            report.error("ORDER_SEQUENCE", "Q&A numbers must run sequentially from 1", line=row.line, entity=cells[0], expected=index, actual=cells[0])
        if not cells[1]:
            report.error("STORY_FIELD_EMPTY", "Question is required", line=row.line, entity=cells[0])
        if not cells[2]:
            report.error("STORY_FIELD_EMPTY", "Asked In is required", line=row.line, entity=cells[0])
        answer = cells[3]
        if answer == "OPEN":
            report.warn("QA_OPEN_QUESTION", "Material question remains OPEN", line=row.line, entity=cells[0], hint="Resolve it, or record the acceptance decision, before the approval gate")
        elif answer.startswith("N/A"):
            if not re.fullmatch(r"N/A\s*[—-]\s*\S.*", answer):
                report.error("QA_ANSWER_INVALID", "An N/A answer must give a reason: 'N/A — reason'", line=row.line, entity=cells[0], actual=answer)
        elif answer in ("", "—"):
            report.error("QA_ANSWER_MISSING", "Answer must be the agreed answer, OPEN, or N/A with a reason", line=row.line, entity=cells[0])
        if cells[4] in ("", "—"):
            report.error("QA_SOURCE_MISSING", "Source must identify the human, document or evidence", line=row.line, entity=cells[0])
        if cells[5] == "":
            report.error("STORY_FIELD_EMPTY", "Impact must describe the effect or be None", line=row.line, entity=cells[0])
    return report.result()


def _check_risks_and_milestones(report: _Report, tables: list[Table], headings: list[Heading]) -> None:
    risk_defs: set[str] = set()
    for heading in headings:
        if heading.level == 3 and (match := re.match(r"(R\d+):\s+\S", heading.title)):
            if match.group(1) in risk_defs:
                report.error("DUPLICATE_DEFINITION", "Risk definition appears more than once", line=heading.line, entity=match.group(1))
            risk_defs.add(match.group(1))

    table_9 = _locate(report, tables, H_RISKS, "Table 9 Risk Register")
    if table_9 is not None:
        seen: set[str] = set()
        for row in table_9.rows:
            if not _check_width(report, row.line, row.cells, len(H_RISKS)):
                continue
            cells = [plain(c) for c in row.cells]
            if not RISK_ID.fullmatch(cells[0]):
                report.error("ID_FORMAT", "Risk ID must be R<n>", line=row.line, entity=cells[0], actual=cells[0])
                continue
            if cells[0] in seen:
                report.error("DUPLICATE_ID", "Risk ID appears more than once in Table 9", line=row.line, entity=cells[0])
            seen.add(cells[0])
            _check_enum(report, row.line, "Likelihood", cells[2], RISK_SCALE, cells[0])
            _check_enum(report, row.line, "Impact", cells[3], RISK_SCALE, cells[0])
            if not cells[4] or cells[4] == "—":
                report.error("STORY_FIELD_EMPTY", "Mitigation must describe a concrete action", line=row.line, entity=cells[0])
            if cells[5] in ("", "—"):
                report.warn("RISK_OWNER_UNASSIGNED", "Risk has no owner; record it as an open question", line=row.line, entity=cells[0])

    table_10 = _locate(report, tables, H_MILESTONES, "Table 10 Milestones")
    if table_10 is not None:
        milestone_defs = {
            match.group(1)
            for heading in headings
            if heading.level == 3 and (match := re.match(r"(M\d+):\s+\S", heading.title))
        }
        seen_milestones: set[str] = set()
        for row in table_10.rows:
            if not _check_width(report, row.line, row.cells, len(H_MILESTONES)):
                continue
            cells = [plain(c) for c in row.cells]
            if not re.fullmatch(r"M\d+", cells[0]):
                report.error("ID_FORMAT", "Milestone ID must be M<n>", line=row.line, entity=cells[0], actual=cells[0])
                continue
            if cells[0] in seen_milestones:
                report.error("DUPLICATE_ID", "Milestone ID appears more than once", line=row.line, entity=cells[0])
            seen_milestones.add(cells[0])
            if cells[0] not in milestone_defs:
                report.error("MILESTONE_DEFINITION_MISSING", "Milestone has no ### Mn definition heading", line=row.line, entity=cells[0], expected=f"### {cells[0]}: <Title>")
            if not cells[2] or cells[2] == "—":
                report.error("STORY_FIELD_EMPTY", "Criteria must be objectively verifiable", line=row.line, entity=cells[0])
            _check_date_or_dash(report, row.line, "Target", cells[3], cells[0])
            _check_enum(report, row.line, "Status", cells[4], MILESTONE_STATUSES, cells[0])


def validate_story_graph(content: str, file: str = "CENTRAL-PLAN-INDEX.md") -> ValidationResult:
    report = _Report(file)
    _check_separators(report, parse_tables(content))
    stories = _parse_central(content, report)
    _check_graph(
        report,
        {story: data["deps"] for story, data in stories.items()},  # type: ignore[misc]
        {story: data["unlocks"] for story, data in stories.items()},  # type: ignore[misc]
        {story: data["order"] for story, data in stories.items()},  # type: ignore[misc]
        {story: data["line"] for story, data in stories.items()},  # type: ignore[misc]
    )
    return report.result()


# ---------------------------------------------------------------------------
# Human ledger decisions (Table 14), bound to the exact entry content
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerDecision:
    decision: str
    entry_sha256: str
    reviewer: str
    decided_at: str


def ledger_entry_revision(content: str, ldg_id: str) -> str | None:
    """Hash the Table 14 row and its #### LDG definition block. Any edit changes the hash."""
    lines = content.splitlines()
    row_line = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            cells = split_row(line)
            if cells and plain(cells[0]) == ldg_id:
                row_line = index
                break
    heading_line = next((i for i, line in enumerate(lines) if line.startswith(f"#### {ldg_id}:")), None)
    if row_line is None or heading_line is None:
        return None
    end = next((i for i in range(heading_line + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return content_revision("\n".join([lines[row_line], *lines[heading_line:end]]))


def _check_ledger_approval(
    report: _Report,
    line: int,
    ldg_id: str,
    content: str,
    approved_by: str,
    decisions: Mapping[str, LedgerDecision] | None,
) -> None:
    if decisions is None:
        report.error(
            "LEDGER_APPROVAL_NOT_VERIFIED",
            "No persisted human decision was supplied; a populated Approved By cell is not approval evidence",
            line=line,
            entity=ldg_id,
            hint="Record the decision with `dridh ledger approve`",
        )
        return
    record = decisions.get(ldg_id)
    if record is None:
        report.error("LEDGER_APPROVAL_MISSING", "No human approval is recorded for this deviation", line=line, entity=ldg_id, hint="Record the decision with `dridh ledger approve`")
        return
    if record.decision != "approve":
        report.error("LEDGER_REJECTED", "The latest human decision rejects this deviation", line=line, entity=ldg_id, actual=record.decision)
        return
    current = ledger_entry_revision(content, ldg_id)
    if record.entry_sha256 != current:
        report.error("LEDGER_APPROVAL_STALE", "The entry changed after approval; re-approval is required", line=line, entity=ldg_id, expected=current, actual=record.entry_sha256)
    if record.reviewer != approved_by:
        report.error("LEDGER_APPROVER_MISMATCH", "Approved By does not match the recorded human reviewer", line=line, entity=ldg_id, expected=record.reviewer, actual=approved_by)


_VALIDATORS.update(
    {
        "use_cases": validate_use_cases,
        "tdd": validate_tdd,
        "central_plan": validate_central_plan,
        "component_master": validate_component_master,
        "phase_file": validate_phase_file,
        "ledger": validate_ledger,
        "test_plan": validate_test_plan,
        "coordinator": validate_coordinator,
        "qa_log": validate_qa_log,
    }
)
