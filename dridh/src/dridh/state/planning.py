"""Planning domain repository (Phase 5 increment 3): stories, dependency DAG, parallel groups,
component phases, milestones, risks, test phases and evidence.

Sources, each validated by the canonical validator before any row is written:
- Central plan (CENTRAL-PLAN-INDEX.md): Table 2 stories, Table 9 risks, Table 10 milestones.
- Phase file: Table 3 story cards, which add the precise Flow IDs of each story.
- Test plan (TEST-PLAN.md): Table 15 test phases.

Rules enforced by the database (see store.py): self-dependency, cycles, phase ordering,
independence of declared parallel pairs and group members, workspace scope, canonical IDs.
Rules enforced here: referenced components and use cases exist; a re-import never discards
curated data (parallel groups, milestone links, evidence) and refuses instead; each
projection is verified against the stored revision text by a canonical signature.

Unlocks are never stored. They are the reverse of the dependency edges, computed on read.
"""

from __future__ import annotations

import heapq
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.findings import Finding
from dridh.document_contracts.markdown import parse_tables, plain
from dridh.document_contracts.validator import (
    H_CENTRAL,
    H_MILESTONES,
    H_RISKS,
    H_TEST,
    story_cards,
    validate_central_plan,
    validate_phase_file,
    validate_test_plan,
)

STORY_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "IN_REVIEW", "COMPLETED", "BLOCKED")
EVIDENCE_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "PASSED", "FAILED", "BLOCKED", "NOT_APPLICABLE")
_UC = re.compile(r"UC-\d{2}")
_FLOW = re.compile(r"(?:SF|AF|EF|BE|BR)-\d{2}\.\d+")
_CMP = re.compile(r"CMP-\d{2}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _list(cell: str) -> tuple[str, ...]:
    value = plain(cell)
    if value in ("", "—", "-", "N/A", "None", "Ops"):
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


# ---------------------------------------------------------------------------
# Models and parsers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoryRow:
    story_id: str
    title: str
    component_id: str
    phase: int
    status: str
    position: int
    use_cases: tuple[str, ...]
    dependencies: tuple[str, ...]
    unlocks: tuple[str, ...]
    parallel_with: tuple[str, ...]


@dataclass(frozen=True)
class MilestoneRow:
    milestone_id: str
    title: str
    criteria: str
    target_date: str | None
    status: str
    position: int


@dataclass(frozen=True)
class RiskRow:
    risk_id: str
    title: str
    likelihood: str
    impact: str
    mitigation: str
    owner: str
    position: int


@dataclass(frozen=True)
class CentralModel:
    stories: tuple[StoryRow, ...]
    milestones: tuple[MilestoneRow, ...]
    risks: tuple[RiskRow, ...]


@dataclass(frozen=True)
class CardRow:
    story_id: str
    title: str
    use_cases: tuple[str, ...]
    flows: tuple[str, ...]
    dependencies: tuple[str, ...]
    unlocks: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class PhaseModel:
    cards: tuple[CardRow, ...]


@dataclass(frozen=True)
class TestPhaseRow:
    test_phase_id: str
    scope: str
    use_cases: tuple[str, ...]
    flows: tuple[str, ...]
    components: tuple[str, ...]
    mode: str
    environment: str
    command: str
    evidence: str
    status: str
    position: int


@dataclass(frozen=True)
class TestPlanModel:
    phases: tuple[TestPhaseRow, ...]


def parse_central(content: str) -> CentralModel:
    stories: list[StoryRow] = []
    milestones: list[MilestoneRow] = []
    risks: list[RiskRow] = []
    for table in parse_tables(content):
        if table.header == H_CENTRAL:
            for row in table.rows:
                cells = [plain(c) for c in row.cells]
                stories.append(
                    StoryRow(
                        story_id=cells[1],
                        title=cells[5],
                        component_id=_CMP.search(row.cells[2]).group(0),  # type: ignore[union-attr]
                        phase=int(cells[3]),
                        status=cells[9],
                        position=int(cells[0]),
                        use_cases=tuple(_UC.findall(row.cells[4])),
                        dependencies=_list(cells[6]),
                        unlocks=_list(cells[7]),
                        parallel_with=_list(cells[8]),
                    )
                )
        elif table.header == H_MILESTONES:
            for index, row in enumerate(table.rows, start=1):
                cells = [plain(c) for c in row.cells]
                milestones.append(MilestoneRow(cells[0], cells[1], cells[2], None if cells[3] == "—" else cells[3], cells[4], index))
        elif table.header == H_RISKS:
            for index, row in enumerate(table.rows, start=1):
                cells = [plain(c) for c in row.cells]
                risks.append(RiskRow(cells[0], cells[1], cells[2], cells[3], cells[4], cells[5], index))
    return CentralModel(tuple(stories), tuple(milestones), tuple(risks))


def parse_phase(content: str) -> PhaseModel:
    cards = []
    for story_id, card in story_cards(content).items():
        cards.append(
            CardRow(
                story_id=story_id,
                title=str(card["title"]),
                use_cases=tuple(card["use_cases"]),  # type: ignore[arg-type]
                flows=tuple(card["flows"]),  # type: ignore[arg-type]
                dependencies=tuple(card["deps"]),  # type: ignore[arg-type]
                unlocks=tuple(card["unlocks"]),  # type: ignore[arg-type]
                status=str(card["status"]),
            )
        )
    return PhaseModel(tuple(cards))


def parse_test_plan(content: str) -> TestPlanModel:
    phases: list[TestPhaseRow] = []
    for table in parse_tables(content):
        if table.header != H_TEST:
            continue
        for index, row in enumerate(table.rows, start=1):
            cells = [plain(c) for c in row.cells]
            phases.append(
                TestPhaseRow(
                    test_phase_id=cells[0],
                    scope=cells[1],
                    use_cases=tuple(_UC.findall(row.cells[2])),
                    flows=tuple(_FLOW.findall(row.cells[2])),
                    components=tuple(_CMP.findall(row.cells[3])),
                    mode=cells[4],
                    environment=cells[5],
                    command=cells[6],
                    evidence=cells[7],
                    status=cells[8],
                    position=index,
                )
            )
    return TestPlanModel(tuple(phases))


def validated_central(content: str, file: str) -> CentralModel:
    result = validate_central_plan(content, file=file)
    if not result.ok:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{file} does not pass central-plan validation; nothing was imported", details=tuple(result.errors[:5]))
    model = parse_central(content)
    partners = {s.story_id: set(s.parallel_with) for s in model.stories}
    for story_id, others in partners.items():
        for other in others:
            if other not in partners or story_id not in partners[other]:
                raise DridhError(
                    ErrorCode.VALIDATION_FAILED,
                    f"Parallel With must be symmetric: {story_id} lists {other} but not the reverse; nothing was imported",
                )
    return model


def validated_phase(content: str, file: str) -> PhaseModel:
    result = validate_phase_file(content, file=file)
    if not result.ok:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{file} does not pass phase-file validation; nothing was imported", details=tuple(result.errors[:5]))
    return parse_phase(content)


def validated_test_plan(content: str, file: str) -> TestPlanModel:
    result = validate_test_plan(content, file=file)
    if not result.ok:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{file} does not pass test-plan validation; nothing was imported", details=tuple(result.errors[:5]))
    return parse_test_plan(content)


# ---------------------------------------------------------------------------
# Canonical signatures: the same projection computed from parsed text and from stored rows
# ---------------------------------------------------------------------------


def central_signature(model: CentralModel) -> tuple:
    stories = tuple(
        sorted(
            (s.story_id, s.title, s.component_id, s.phase, s.status, tuple(sorted(s.use_cases)), tuple(sorted(s.dependencies)), tuple(sorted(s.parallel_with)))
            for s in model.stories
        )
    )
    milestones = tuple(sorted((m.milestone_id, m.title, m.criteria, m.target_date or "", m.status) for m in model.milestones))
    risks = tuple(sorted((r.risk_id, r.title, r.likelihood, r.impact, r.mitigation, r.owner) for r in model.risks))
    return stories, milestones, risks


def phase_identity(content: str) -> tuple[str | None, int | None]:
    """The component and phase a phase file declares in its header (`# Phase N — ...` and `> **Component:** CMP-NN`)."""
    phase = re.search(r"^# Phase (\d+)\b", content, re.MULTILINE)
    component = re.search(r"^> \*\*Component:\*\* (\S+)", content, re.MULTILINE)
    return (component.group(1) if component else None, int(phase.group(1)) if phase else None)


def phase_signature(model: PhaseModel) -> tuple:
    return tuple(sorted((c.story_id, c.title, tuple(sorted(c.use_cases)), tuple(sorted(c.flows)), tuple(sorted(c.dependencies)), tuple(sorted(c.unlocks)), c.status) for c in model.cards))


def test_plan_signature(model: TestPlanModel) -> tuple:
    return tuple(
        sorted(
            (t.test_phase_id, t.scope, tuple(sorted(t.use_cases)), tuple(sorted(t.flows)), tuple(sorted(t.components)), t.mode, t.environment, t.command, t.evidence, t.status)
            for t in model.phases
        )
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoryView:
    story_id: str
    title: str
    component_id: str
    phase: int
    status: str
    use_cases: tuple[str, ...]
    dependencies: tuple[str, ...]
    unlocks: tuple[str, ...]
    parallel_with: tuple[str, ...]
    state: str


class PlanningRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Planning writes must run inside a transaction.")

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, (self.workspace_id, *params)).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, (self.workspace_id, *params)).fetchall()

    def import_marker(self, document_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM planning_import WHERE workspace_id = ? AND document_id = ?", (document_id,))

    def _missing(self, sql: str, values: set[str], label: str, recovery: str) -> None:
        if not values:
            return
        missing = sorted(value for value in values if self.connection.execute(sql, (self.workspace_id, value)).fetchone() is None)
        if missing:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"{label} not found in this workspace; nothing was imported", details=tuple(missing), recovery=recovery)

    # ------------------------------------------------------------- central plan

    def import_central(self, *, document_id: str, revision_id: str, model: CentralModel) -> dict[str, int]:
        self._require_transaction()
        components = {s.component_id for s in model.stories}
        self._missing(
            "SELECT 1 FROM component WHERE workspace_id = ? AND component_id = ?",
            components,
            "Components",
            "Import the feature TDD first: dridh design import <tdd path>",
        )
        ucs = {uc for s in model.stories for uc in s.use_cases}
        self._missing(
            "SELECT 1 FROM use_case WHERE workspace_id = ? AND use_case_id = ?",
            ucs,
            "Use cases",
            "Import the use-case document first: dridh usecases import <path>",
        )
        own_stories = {r["story_id"] for r in self._all("SELECT story_id FROM story WHERE workspace_id = ? AND document_id = ?", (document_id,))}
        self._refuse_curated(own_stories, document_id)
        other = self._all(
            f"SELECT story_id FROM story WHERE workspace_id = ? AND story_id IN ({','.join('?' for _ in model.stories)}) AND document_id <> ?",
            tuple(s.story_id for s in model.stories) + (document_id,),
        ) if model.stories else []
        if other:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Story IDs already belong to another planning document; nothing was imported", details=tuple(r[0] for r in other))

        try:
            self._clear_central(document_id, own_stories)
            seen_phases: set[tuple[str, int]] = set()
            for story in model.stories:
                key = (story.component_id, story.phase)
                if key not in seen_phases:
                    seen_phases.add(key)
                    self.connection.execute(
                        "INSERT INTO component_phase(id, workspace_id, document_id, source_revision_id, component_id, phase_number) VALUES (?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, story.component_id, story.phase),
                    )
            for story in model.stories:
                self.connection.execute(
                    "INSERT INTO story(id, workspace_id, document_id, source_revision_id, story_id, title, component_id, phase_number, status, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, story.story_id, story.title, story.component_id, story.phase, story.status, story.position),
                )
                for uc in story.use_cases:
                    self.connection.execute(
                        "INSERT INTO story_use_case(id, workspace_id, document_id, source_revision_id, story_id, use_case_id, flow_id) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, story.story_id, uc),
                    )
            for story in model.stories:
                for prerequisite in story.dependencies:
                    self.connection.execute(
                        "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id, rationale) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, story.story_id, prerequisite),
                    )
            for story in model.stories:
                for other_id in story.parallel_with:
                    if story.story_id < other_id:
                        self.connection.execute(
                            "INSERT INTO story_parallel_pair(id, workspace_id, document_id, source_revision_id, story_id, other_story_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, story.story_id, other_id),
                        )
            for milestone in model.milestones:
                self.connection.execute(
                    "INSERT INTO milestone(id, workspace_id, document_id, source_revision_id, milestone_id, title, criteria, target_date, status, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, milestone.milestone_id, milestone.title, milestone.criteria, milestone.target_date, milestone.status, milestone.position),
                )
            for risk in model.risks:
                self.connection.execute(
                    "INSERT INTO risk(id, workspace_id, document_id, source_revision_id, risk_id, title, likelihood, impact, mitigation, owner, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, risk.risk_id, risk.title, risk.likelihood, risk.impact, risk.mitigation, risk.owner, risk.position),
                )
            counts = {"stories": len(model.stories), "dependencies": sum(len(s.dependencies) for s in model.stories), "milestones": len(model.milestones), "risks": len(model.risks)}
            self._mark(document_id, revision_id, "CENTRAL_PLAN")
        except sqlite3.IntegrityError as exc:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "The central plan violates a planning rule; nothing was imported",
                details=(str(exc),),
                recovery="Fix the dependency graph, phase ordering or parallel declarations in the central plan.",
            ) from None
        return counts

    def _refuse_curated(self, stories: set[str], document_id: str) -> None:
        if not stories:
            return
        marks = ",".join("?" for _ in stories)
        checks = (
            (f"SELECT m.story_id FROM story_parallel_member m WHERE m.workspace_id = ? AND m.story_id IN ({marks})", "parallel group membership"),
            (f"SELECT l.story_id FROM milestone_story l WHERE l.workspace_id = ? AND l.story_id IN ({marks})", "milestone links"),
            (f"SELECT e.story_id FROM story_test_evidence e WHERE e.workspace_id = ? AND e.story_id IN ({marks})", "recorded test evidence"),
        )
        for sql, label in checks:
            rows = self._all(sql, tuple(sorted(stories)))
            if rows:
                raise DridhError(
                    ErrorCode.VALIDATION_FAILED,
                    f"Re-import would discard {label}; nothing was changed",
                    details=tuple(sorted({r[0] for r in rows})),
                    recovery=f"Remove the {label} for these stories first, then re-import the central plan.",
                )

    def _clear_central(self, document_id: str, own_stories: set[str]) -> None:
        phase_docs: set[str] = set()
        if own_stories:
            marks = ",".join("?" for _ in own_stories)
            phase_docs = {
                r[0]
                for r in self.connection.execute(
                    f"SELECT DISTINCT document_id FROM story_use_case WHERE workspace_id = ? AND story_id IN ({marks}) AND document_id <> ?",
                    (self.workspace_id, *sorted(own_stories), document_id),
                ).fetchall()
            }
            self.connection.execute(
                f"DELETE FROM story_use_case WHERE workspace_id = ? AND story_id IN ({marks}) AND document_id <> ?",
                (self.workspace_id, *sorted(own_stories), document_id),
            )
        for phase_doc in phase_docs:
            self.connection.execute("DELETE FROM planning_import WHERE workspace_id = ? AND document_id = ?", (self.workspace_id, phase_doc))
        scope = (self.workspace_id, document_id)
        self.connection.execute("DELETE FROM story_parallel_pair WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM story_dependency WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM story_use_case WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM milestone WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM risk WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM story WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM component_phase WHERE workspace_id = ? AND document_id = ?", scope)
        self.connection.execute("DELETE FROM planning_import WHERE workspace_id = ? AND document_id = ?", scope)

    def _mark(self, document_id: str, revision_id: str, kind: str) -> None:
        self.connection.execute(
            "INSERT INTO planning_import(workspace_id, document_id, source_revision_id, document_kind, imported_at) VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, document_id, revision_id, kind, _now()),
        )

    # ------------------------------------------------------------- phase file

    def import_phase(self, *, document_id: str, revision_id: str, model: PhaseModel) -> dict[str, int]:
        self._require_transaction()
        ids = {c.story_id for c in model.cards}
        self._missing("SELECT 1 FROM story WHERE workspace_id = ? AND story_id = ?", ids, "Stories", "Import the central plan first: dridh planning import <central path>")
        graph = self.graph()
        for card in model.cards:
            view = graph.get(card.story_id)
            if view is None:
                continue
            problems = []
            if view.title != card.title:
                problems.append("title")
            if tuple(sorted(view.use_cases)) != tuple(sorted(card.use_cases)):
                problems.append("use cases")
            if tuple(sorted(view.dependencies)) != tuple(sorted(card.dependencies)):
                problems.append("dependencies")
            if tuple(sorted(view.unlocks)) != tuple(sorted(card.unlocks)):
                problems.append("unlocks")
            if view.status != card.status:
                problems.append("status")
            if problems:
                raise DridhError(
                    ErrorCode.VALIDATION_FAILED,
                    f"Story card {card.story_id} disagrees with the central plan on {', '.join(problems)}; nothing was imported",
                    recovery="Make the phase file and the central plan agree, then import again.",
                )
        try:
            self.connection.execute("DELETE FROM story_use_case WHERE workspace_id = ? AND document_id = ?", (self.workspace_id, document_id))
            self.connection.execute("DELETE FROM planning_import WHERE workspace_id = ? AND document_id = ?", (self.workspace_id, document_id))
            flows = 0
            for card in model.cards:
                covered: set[str] = set()
                for flow in card.flows:
                    uc = f"UC-{flow[3:5]}"
                    covered.add(uc)
                    self.connection.execute(
                        "INSERT INTO story_use_case(id, workspace_id, document_id, source_revision_id, story_id, use_case_id, flow_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, card.story_id, uc, flow),
                    )
                    flows += 1
                for uc in sorted(set(card.use_cases) - covered):  # a use case named without a flow is still part of the card
                    self.connection.execute(
                        "INSERT INTO story_use_case(id, workspace_id, document_id, source_revision_id, story_id, use_case_id, flow_id) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (str(uuid.uuid4()), self.workspace_id, document_id, revision_id, card.story_id, uc),
                    )
            self._mark(document_id, revision_id, "PHASE_FILE")
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A story flow does not belong to the story's use case; nothing was imported", details=(str(exc),)) from None
        return {"stories": len(model.cards), "flows": flows}

    # ------------------------------------------------------------- test plan

    def import_test_plan(self, *, document_id: str, revision_id: str, model: TestPlanModel) -> dict[str, int]:
        self._require_transaction()
        self._missing("SELECT 1 FROM use_case WHERE workspace_id = ? AND use_case_id = ?", {u for t in model.phases for u in t.use_cases}, "Use cases", "Import the use-case document first.")
        self._missing("SELECT 1 FROM use_case_flow WHERE workspace_id = ? AND flow_id = ?", {f for t in model.phases for f in t.flows}, "Flows", "Import the use-case document first.")
        self._missing("SELECT 1 FROM component WHERE workspace_id = ? AND component_id = ?", {c for t in model.phases for c in t.components}, "Components", "Import the feature TDD first.")
        own = {r[0] for r in self._all("SELECT test_phase_id FROM test_phase WHERE workspace_id = ? AND document_id = ?", (document_id,))}
        if own:
            marks = ",".join("?" for _ in own)
            evidence = self._all(f"SELECT DISTINCT test_phase_id FROM story_test_evidence WHERE workspace_id = ? AND test_phase_id IN ({marks})", tuple(sorted(own)))
            if evidence:
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Re-import would discard recorded test evidence; nothing was changed", details=tuple(sorted(r[0] for r in evidence)))
        try:
            scope = (self.workspace_id, document_id)
            for row in self._all("SELECT test_phase_id FROM test_phase WHERE workspace_id = ? AND document_id = ?", (document_id,)):
                self.connection.execute("DELETE FROM test_phase_use_case WHERE workspace_id = ? AND test_phase_id = ?", (self.workspace_id, row["test_phase_id"]))
                self.connection.execute("DELETE FROM test_phase_flow WHERE workspace_id = ? AND test_phase_id = ?", (self.workspace_id, row["test_phase_id"]))
                self.connection.execute("DELETE FROM test_phase_component WHERE workspace_id = ? AND test_phase_id = ?", (self.workspace_id, row["test_phase_id"]))
            self.connection.execute("DELETE FROM test_phase WHERE workspace_id = ? AND document_id = ?", scope)
            self.connection.execute("DELETE FROM planning_import WHERE workspace_id = ? AND document_id = ?", scope)
            for phase in model.phases:
                phase_row = str(uuid.uuid4())
                self.connection.execute(
                    "INSERT INTO test_phase(id, workspace_id, document_id, source_revision_id, test_phase_id, test_scope, dependency_mode, environment, execution_command, evidence_expectation, status, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (phase_row, self.workspace_id, document_id, revision_id, phase.test_phase_id, phase.scope, phase.mode, phase.environment, phase.command, phase.evidence, phase.status, phase.position),
                )
                for uc in phase.use_cases:
                    self.connection.execute("INSERT INTO test_phase_use_case(workspace_id, test_phase_id, use_case_id) VALUES (?, ?, ?)", (self.workspace_id, phase.test_phase_id, uc))
                for flow in phase.flows:
                    self.connection.execute("INSERT INTO test_phase_flow(workspace_id, test_phase_id, flow_id) VALUES (?, ?, ?)", (self.workspace_id, phase.test_phase_id, flow))
                for component in phase.components:
                    self.connection.execute("INSERT INTO test_phase_component(workspace_id, test_phase_id, component_id) VALUES (?, ?, ?)", (self.workspace_id, phase.test_phase_id, component))
            self._mark(document_id, revision_id, "TEST_PLAN")
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A test phase conflicts with an existing one in this workspace; nothing was imported", details=(str(exc),)) from None
        return {"test_phases": len(model.phases)}

    # ------------------------------------------------------------- reads

    def graph(self) -> dict[str, StoryView]:
        """Every story with derived unlocks and a deterministic ready/blocked state."""

        stories = {r["story_id"]: r for r in self._all("SELECT * FROM story WHERE workspace_id = ?")}
        deps: dict[str, list[str]] = {sid: [] for sid in stories}
        for r in self._all("SELECT story_id, depends_on_story_id FROM story_dependency WHERE workspace_id = ?"):
            deps[r["story_id"]].append(r["depends_on_story_id"])
        unlocks: dict[str, list[str]] = {sid: [] for sid in stories}
        for sid, prereqs in deps.items():
            for p in prereqs:
                unlocks[p].append(sid)
        uses: dict[str, list[str]] = {sid: [] for sid in stories}
        for r in self._all("SELECT story_id, use_case_id FROM story_use_case WHERE workspace_id = ? AND flow_id IS NULL"):
            uses[r["story_id"]].append(r["use_case_id"])
        flows_uses: dict[str, set[str]] = {sid: set() for sid in stories}
        for r in self._all("SELECT story_id, use_case_id FROM story_use_case WHERE workspace_id = ?"):
            flows_uses[r["story_id"]].add(r["use_case_id"])
        partners: dict[str, list[str]] = {sid: [] for sid in stories}
        for r in self._all("SELECT story_id, other_story_id FROM story_parallel_pair WHERE workspace_id = ?"):
            partners[r["story_id"]].append(r["other_story_id"])
            partners[r["other_story_id"]].append(r["story_id"])
        views: dict[str, StoryView] = {}
        for sid, row in stories.items():
            prereq_status = [stories[p]["status"] for p in deps[sid]]
            if any(status != "COMPLETED" for status in prereq_status):
                state = "BLOCKED"
            elif row["status"] == "COMPLETED":
                state = "DONE"
            elif row["status"] == "NOT_STARTED":
                state = "READY"
            else:
                state = "ACTIVE"
            views[sid] = StoryView(
                story_id=sid,
                title=row["title"],
                component_id=row["component_id"],
                phase=row["phase_number"],
                status=row["status"],
                use_cases=tuple(sorted(uses[sid])),
                dependencies=tuple(sorted(deps[sid])),
                unlocks=tuple(sorted(unlocks[sid])),
                parallel_with=tuple(sorted(partners[sid])),
                state=state,
            )
        return views

    def topological_order(self) -> list[str]:
        """Deterministic order: dependencies first, ties broken by (phase, story ID). Raises on a cycle."""

        views = self.graph()
        indegree = {sid: len(v.dependencies) for sid, v in views.items()}
        heap = [(v.phase, sid) for sid, v in views.items() if indegree[sid] == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            _, sid = heapq.heappop(heap)
            order.append(sid)
            for child in views[sid].unlocks:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(heap, (views[child].phase, child))
        if len(order) != len(views):
            cyclic = sorted(sid for sid in views if sid not in order)
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The dependency graph contains a cycle", details=tuple(cyclic))
        return order

    def story_rows_for(self, document_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM story WHERE workspace_id = ? AND document_id = ? ORDER BY position", (document_id,))

    def central_signature_from_rows(self, document_id: str) -> tuple:
        stories = []
        for row in self.story_rows_for(document_id):
            sid = row["story_id"]
            own_uses = tuple(sorted(r[0] for r in self._all("SELECT use_case_id FROM story_use_case WHERE workspace_id = ? AND story_id = ? AND flow_id IS NULL AND document_id = ?", (sid, document_id))))
            own_deps = tuple(sorted(r[0] for r in self._all("SELECT depends_on_story_id FROM story_dependency WHERE workspace_id = ? AND story_id = ? AND document_id = ?", (sid, document_id))))
            own_parallel = tuple(sorted(
                {r[0] for r in self._all("SELECT other_story_id FROM story_parallel_pair WHERE workspace_id = ? AND story_id = ? AND document_id = ?", (sid, document_id))}
                | {r[0] for r in self._all("SELECT story_id FROM story_parallel_pair WHERE workspace_id = ? AND other_story_id = ? AND document_id = ?", (sid, document_id))}
            ))
            stories.append((sid, row["title"], row["component_id"], row["phase_number"], row["status"], own_uses, own_deps, own_parallel))
        milestones = tuple(sorted((r["milestone_id"], r["title"], r["criteria"], r["target_date"] or "", r["status"]) for r in self._all("SELECT * FROM milestone WHERE workspace_id = ? AND document_id = ?", (document_id,))))
        risks = tuple(sorted((r["risk_id"], r["title"], r["likelihood"], r["impact"], r["mitigation"], r["owner"]) for r in self._all("SELECT * FROM risk WHERE workspace_id = ? AND document_id = ?", (document_id,))))
        return tuple(sorted(stories)), milestones, risks

    def phase_signature_from_rows(self, document_id: str, *, component_id: str | None = None, phase_number: int | None = None) -> tuple:
        """The phase file as SQLite sees it. Membership comes from the story table (component and phase), so a story with
        no use case or flow (infrastructure work) is included; its links come from this document's story_use_case rows."""
        views = self.graph()
        per_story: dict[str, dict[str, set[str]]] = {}
        if component_id is not None and phase_number is not None:
            for r in self._all("SELECT story_id FROM story WHERE workspace_id = ? AND component_id = ? AND phase_number = ?", (component_id, phase_number)):
                per_story.setdefault(r["story_id"], {"ucs": set(), "flows": set()})
        for r in self._all("SELECT story_id, use_case_id, flow_id FROM story_use_case WHERE workspace_id = ? AND document_id = ?", (document_id,)):
            entry = per_story.setdefault(r["story_id"], {"ucs": set(), "flows": set()})
            entry["ucs"].add(r["use_case_id"])
            if r["flow_id"]:
                entry["flows"].add(r["flow_id"])
        rows = []
        for sid, entry in per_story.items():
            view = views[sid]
            rows.append((sid, view.title, tuple(sorted(entry["ucs"])), tuple(sorted(entry["flows"])), view.dependencies, view.unlocks, view.status))
        return tuple(sorted(rows))

    def test_plan_signature_from_rows(self, document_id: str) -> tuple:
        rows = []
        for r in self._all("SELECT * FROM test_phase WHERE workspace_id = ? AND document_id = ?", (document_id,)):
            tid = r["test_phase_id"]
            ucs = tuple(sorted(x[0] for x in self._all("SELECT use_case_id FROM test_phase_use_case WHERE workspace_id = ? AND test_phase_id = ?", (tid,))))
            flows = tuple(sorted(x[0] for x in self._all("SELECT flow_id FROM test_phase_flow WHERE workspace_id = ? AND test_phase_id = ?", (tid,))))
            comps = tuple(sorted(x[0] for x in self._all("SELECT component_id FROM test_phase_component WHERE workspace_id = ? AND test_phase_id = ?", (tid,))))
            rows.append((tid, r["test_scope"], ucs, flows, comps, r["dependency_mode"], r["environment"], r["execution_command"], r["evidence_expectation"], r["status"]))
        return tuple(sorted(rows))

    def findings(self, *, relative_path: str, document_id: str, kind: str, current_revision_id: str, current_content: str) -> list[Finding]:
        marker = self.import_marker(document_id)
        if marker is None:
            return [Finding("PLANNING_NEVER_IMPORTED", "The planning domain has never imported this document", file=relative_path, repair_hint=f"Import it: dridh planning import {relative_path}")]
        findings: list[Finding] = []
        if marker["source_revision_id"] != current_revision_id:
            findings.append(Finding("PLANNING_STALE", "The planning rows were imported from a revision that is no longer current", file=relative_path, expected=current_revision_id, actual=marker["source_revision_id"], repair_hint=f"Re-import: dridh planning import {relative_path}"))
        if kind == "CENTRAL_PLAN":
            if not validate_central_plan(current_content, file=relative_path).ok:
                return findings + [Finding("REVISION_NOT_PROJECTABLE", "The current revision does not pass central-plan validation", file=relative_path)]
            model = parse_central(current_content)
            if central_signature(model) != self.central_signature_from_rows(document_id):
                findings.append(Finding("PLANNING_MISMATCH", "The stored planning rows differ from the stored revision text", file=relative_path, repair_hint=f"Re-import: dridh planning import {relative_path}"))

            # Table 2 Unlocks is a projection of reverse dependency edges. Validate
            # it against the independently derived SQLite graph, not only Markdown.
            graph = self.graph()
            row_lines = {
                plain(row.cells[1]): row.line
                for table in parse_tables(current_content)
                if table.header == H_CENTRAL
                for row in table.rows
                if len(row.cells) > 1
            }
            for story in model.stories:
                persisted = graph.get(story.story_id)
                if persisted is None:
                    continue
                declared = tuple(sorted(story.unlocks))
                derived = tuple(sorted(persisted.unlocks))
                if declared != derived:
                    findings.append(
                        Finding(
                            "PLANNING_UNLOCK_MISMATCH",
                            "Table 2 Unlocks differs from reverse dependency edges in the persisted SQLite planning graph",
                            file=relative_path,
                            line=row_lines.get(story.story_id),
                            entity_id=story.story_id,
                            expected=", ".join(derived) or "—",
                            actual=", ".join(declared) or "—",
                            repair_hint="Reconcile Central Plan dependencies with persisted Coordinator state, then re-import and verify.",
                        )
                    )
        elif kind == "PHASE_FILE":
            if not validate_phase_file(current_content, file=relative_path).ok:
                return findings + [Finding("REVISION_NOT_PROJECTABLE", "The current revision does not pass phase-file validation", file=relative_path)]
            component_id, phase_number = phase_identity(current_content)
            if phase_signature(parse_phase(current_content)) != self.phase_signature_from_rows(document_id, component_id=component_id, phase_number=phase_number):
                findings.append(Finding("PLANNING_MISMATCH", "The stored story flows differ from the stored revision text", file=relative_path, repair_hint=f"Re-import: dridh planning import {relative_path}"))
        else:
            if not validate_test_plan(current_content, file=relative_path).ok:
                return findings + [Finding("REVISION_NOT_PROJECTABLE", "The current revision does not pass test-plan validation", file=relative_path)]
            if test_plan_signature(parse_test_plan(current_content)) != self.test_plan_signature_from_rows(document_id):
                findings.append(Finding("PLANNING_MISMATCH", "The stored test phases differ from the stored revision text", file=relative_path, repair_hint=f"Re-import: dridh planning import {relative_path}"))
        return findings

    # ------------------------------------------------------------- curated data

    def create_parallel_group(self, name: str, story_ids: list[str]) -> str:
        self._require_transaction()
        group_id = str(uuid.uuid4())
        try:
            self.connection.execute("INSERT INTO story_parallel_group(id, workspace_id, group_name, created_at) VALUES (?, ?, ?, ?)", (group_id, self.workspace_id, name, _now()))
            for sid in story_ids:
                self.connection.execute("INSERT INTO story_parallel_member(id, workspace_id, group_id, story_id) VALUES (?, ?, ?, ?)", (str(uuid.uuid4()), self.workspace_id, group_id, sid))
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The parallel group was not created", details=(str(exc),), recovery="Use stories that exist in this workspace and are mutually independent.") from None
        return group_id

    def link_milestone(self, milestone_id: str, story_id: str) -> None:
        self._require_transaction()
        try:
            self.connection.execute("INSERT INTO milestone_story(workspace_id, milestone_id, story_id) VALUES (?, ?, ?)", (self.workspace_id, milestone_id, story_id))
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The milestone link was not created", details=(str(exc),), recovery="Use a milestone and a story that exist in this workspace.") from None

    def record_evidence(self, *, story_id: str, test_phase_id: str, status: str, reference: str, recorded_by: str) -> str:
        self._require_transaction()
        if status not in EVIDENCE_STATUSES:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Evidence status must be one of {', '.join(EVIDENCE_STATUSES)}")
        evidence_id = str(uuid.uuid4())
        try:
            self.connection.execute(
                "INSERT INTO story_test_evidence(id, workspace_id, story_id, test_phase_id, evidence_status, evidence_ref, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, self.workspace_id, story_id, test_phase_id, status, reference, recorded_by, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The evidence was not recorded", details=(str(exc),), recovery="Use a story and test phase in this workspace, a canonical status, and a non-empty reference.") from None
        return evidence_id
