"""Planning domain persistence against real SQLite (Phase 5 increment 3). No mocks.

Each structural rule is asserted through SQLite itself (CHECK, foreign key, trigger), so the
tests prove the database enforces it, not the Python layer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dridh.common.errors import DridhError
from dridh.state.design import DesignRepository, validated_master_model, validated_tdd_model
from dridh.state.documents import DocumentRepository
from dridh.state.migration import CURRENT_SCHEMA_VERSION, migrate_to_current, schema_version
from dridh.state.planning import (
    CentralModel,
    MilestoneRow,
    PlanningRepository,
    StoryRow,
    validated_central,
    validated_phase,
    validated_test_plan,
)
from dridh.state.store import STATE_SCHEMA_VERSION, StateStore, append_ledger, verify_ledger
from dridh.state.usecases import UseCaseRepository, validated_model
from dridh.workspace.layout import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
UC = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
TDD = (FIXTURES / "feature_tdd.md").read_text(encoding="utf-8")
MASTER = (FIXTURES / "COMPONENT-MASTER.md").read_text(encoding="utf-8")
CENTRAL = (FIXTURES / "CENTRAL-PLAN-INDEX.md").read_text(encoding="utf-8")
PHASE = (FIXTURES / "phase-1-order-service.md").read_text(encoding="utf-8")
TEST_PLAN = (FIXTURES / "TEST-PLAN.md").read_text(encoding="utf-8")
PATHS = {"uc": "docs/001-USE-CASES.md", "tdd": "docs/feature_tdd.md", "master": "docs/COMPONENT-MASTER.md", "central": "docs/CENTRAL-PLAN-INDEX.md", "phase": "docs/phase-1.md", "tests": "docs/TEST-PLAN.md"}


@pytest.fixture
def workspace(tmp_path: Path):  # type: ignore[no-untyped-def]
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return info


def open_store(info) -> StateStore:  # type: ignore[no-untyped-def]
    return StateStore(info.state_db, info.workspace_id)


def _revision(connection, info, key: str, kind: str, text: str):  # type: ignore[no-untyped-def]
    documents = DocumentRepository(connection, info.workspace_id)
    revision, _ = documents.record_revision(relative_path=key, document_type=kind, content=text, standards_version="2.1.0", source_path=key)
    return str(documents.document_metadata(key)["id"]), revision.id  # type: ignore[index]


def import_chain(store: StateStore, info) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """The full fixture chain, in dependency order, through the real repositories."""

    ids: dict[str, str] = {}
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["uc"], "use_cases", UC)
        UseCaseRepository(c, info.workspace_id).replace_projection(document_id=doc, revision_id=rev, model=validated_model(UC, PATHS["uc"]))
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["tdd"], "tdd", TDD)
        DesignRepository(c, info.workspace_id).replace_tdd(document_id=doc, revision_id=rev, model=validated_tdd_model(TDD, PATHS["tdd"]))
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["master"], "component_master", MASTER)
        DesignRepository(c, info.workspace_id).replace_component_master(document_id=doc, revision_id=rev, model=validated_master_model(MASTER, PATHS["master"]))
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["central"], "central_plan", CENTRAL)
        ids["central"] = doc
        PlanningRepository(c, info.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(CENTRAL, PATHS["central"]))
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["phase"], "phase_file", PHASE)
        ids["phase"] = doc
        PlanningRepository(c, info.workspace_id).import_phase(document_id=doc, revision_id=rev, model=validated_phase(PHASE, PATHS["phase"]))
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["tests"], "test_plan", TEST_PLAN)
        ids["tests"] = doc
        PlanningRepository(c, info.workspace_id).import_test_plan(document_id=doc, revision_id=rev, model=validated_test_plan(TEST_PLAN, PATHS["tests"]))
    return ids


@pytest.fixture
def planned(workspace):  # type: ignore[no-untyped-def]
    store = open_store(workspace)
    store.open()
    ids = import_chain(store, workspace)
    yield store, workspace, ids
    store.close()


def rows(store: StateStore, sql: str, *params) -> list[tuple]:  # type: ignore[no-untyped-def]
    return [tuple(r) for r in store.connection.execute(sql, params).fetchall()]  # type: ignore[union-attr]


def expect_integrity(store: StateStore, sql: str, params: tuple = (), match: str | None = None) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(sqlite3.IntegrityError, match=match):
        store.connection.execute(sql, params)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Schema, import and graph
# ---------------------------------------------------------------------------


def test_fresh_database_has_planning_tables_at_current_version(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}  # type: ignore[union-attr]
    assert {"story", "story_dependency", "story_parallel_pair", "story_parallel_group", "story_parallel_member", "component_phase", "milestone", "risk", "test_phase", "story_test_evidence"} <= tables
    assert STATE_SCHEMA_VERSION == CURRENT_SCHEMA_VERSION


def test_import_chain_projects_stories_with_derived_unlocks(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    views = PlanningRepository(store.connection, info.workspace_id).graph()  # type: ignore[arg-type]
    assert views["ST-1.1"].unlocks == ("ST-1.2",)
    assert views["ST-1.2"].dependencies == ("ST-1.1",)
    assert views["ST-1.1"].parallel_with == ("ST-2.1",) and views["ST-2.1"].parallel_with == ("ST-1.1",)
    assert views["ST-1.1"].use_cases == ("UC-01",) and views["ST-2.1"].use_cases == ("UC-02",)
    assert views["ST-1.2"].state == "BLOCKED" and views["ST-1.1"].state == "READY" and views["ST-2.1"].state == "READY"


def test_topological_order_is_deterministic_and_survives_reopen(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    first = PlanningRepository(store.connection, info.workspace_id).topological_order()  # type: ignore[arg-type]
    assert first == ["ST-1.1", "ST-2.1", "ST-1.2"] or first == ["ST-1.1", "ST-1.2", "ST-2.1"]
    assert first == PlanningRepository(store.connection, info.workspace_id).topological_order()  # type: ignore[arg-type]
    store.close()
    with open_store(info) as reopened:
        assert PlanningRepository(reopened.connection, info.workspace_id).topological_order() == first  # type: ignore[arg-type]


def test_ready_and_blocked_follow_prerequisite_status_after_reimport(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    completed = CENTRAL.replace("| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | None | ST-1.2 | ST-2.1 | NOT_STARTED |", "| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | None | ST-1.2 | ST-2.1 | COMPLETED |")
    assert completed != CENTRAL
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["central"], "central_plan", completed)
        PlanningRepository(c, info.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(completed, PATHS["central"]))
    views = PlanningRepository(store.connection, info.workspace_id).graph()  # type: ignore[arg-type]
    assert views["ST-1.1"].state == "DONE"
    assert views["ST-1.2"].state == "READY"


def test_central_plan_rejects_a_cycle_through_the_database(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'cyc', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-1.2' FROM story WHERE story_id='ST-1.2'", match="cycle")


def test_self_dependency_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'self', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-1.1' FROM story WHERE story_id='ST-1.1'")


def test_unknown_dependency_endpoint_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'ghost', workspace_id, document_id, source_revision_id, 'ST-1.2', 'ST-9.9' FROM story WHERE story_id='ST-1.2'")


def test_prerequisite_in_a_later_phase_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    _add_story(store, info, "ST-9.9", 9)
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'late', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-9.9' FROM story WHERE story_id='ST-1.1'", match="later phase")


def test_parallel_pair_between_dependent_stories_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    expect_integrity(store, "INSERT INTO story_parallel_pair(id, workspace_id, document_id, source_revision_id, story_id, other_story_id) SELECT 'pp', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-1.2' FROM story WHERE story_id='ST-1.1'", match="independent")


def test_parallel_group_accepts_independent_and_rejects_dependent_members(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    with store.transaction() as c:
        group = PlanningRepository(c, info.workspace_id).create_parallel_group("wave-1", ["ST-1.1", "ST-2.1"])
    assert group
    with pytest.raises(DridhError, match="not created"):
        with store.transaction() as c:
            PlanningRepository(c, info.workspace_id).create_parallel_group("bad", ["ST-1.1", "ST-1.2"])
    assert rows(store, "SELECT group_name FROM story_parallel_group ORDER BY group_name") == [("wave-1",)]


def test_stories_cannot_reference_an_unknown_component_phase(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    expect_integrity(store, "INSERT INTO story(id, workspace_id, document_id, source_revision_id, story_id, title, component_id, phase_number, status, position) SELECT 'x', workspace_id, document_id, source_revision_id, 'ST-7.7', 't', 'CMP-01', 9, 'NOT_STARTED', 9 FROM story LIMIT 1")


def test_canonical_identifier_formats_are_enforced(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    base = "workspace_id, document_id, source_revision_id"
    expect_integrity(store, f"INSERT INTO milestone(id, {base}, milestone_id, title, criteria, target_date, status, position) SELECT 'm', {base}, 'X1', 't', 'c', NULL, 'NOT_STARTED', 9 FROM story WHERE story_id='ST-1.1'")
    expect_integrity(store, f"INSERT INTO risk(id, {base}, risk_id, title, likelihood, impact, mitigation, owner, position) SELECT 'r', {base}, 'RR', 't', 'Low', 'Low', 'm', 'o', 9 FROM story WHERE story_id='ST-1.1'")
    expect_integrity(store, f"INSERT INTO test_phase(id, {base}, test_phase_id, test_scope, dependency_mode, environment, execution_command, evidence_expectation, status, position) SELECT 't', {base}, 'Q1', 's', 'TESTCONTAINERS', 'e', 'c', 'x', 'NOT_STARTED', 9 FROM story WHERE story_id='ST-1.1'")
    expect_integrity(store, f"INSERT INTO story(id, {base}, story_id, title, component_id, phase_number, status, position) SELECT 's2', {base}, 'UC-01', 't', 'CMP-01', 1, 'NOT_STARTED', 9 FROM story WHERE story_id='ST-1.1'")


def test_milestone_target_date_and_status_are_constrained(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    base = "workspace_id, document_id, source_revision_id"
    expect_integrity(store, f"INSERT INTO milestone(id, {base}, milestone_id, title, criteria, target_date, status, position) SELECT 'm2', {base}, 'M9', 't', 'c', '11/06/2026', 'NOT_STARTED', 9 FROM story WHERE story_id='ST-1.1'")
    expect_integrity(store, f"INSERT INTO milestone(id, {base}, milestone_id, title, criteria, target_date, status, position) SELECT 'm3', {base}, 'M8', 't', 'c', NULL, 'DONE', 9 FROM story WHERE story_id='ST-1.1'")


def test_milestone_links_are_workspace_scoped(planned, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    with store.transaction() as c:
        PlanningRepository(c, info.workspace_id).link_milestone("M1", "ST-1.2")
    assert rows(store, "SELECT milestone_id, story_id FROM milestone_story") == [("M1", "ST-1.2")]
    other = tmp_path / "other"
    other.mkdir()
    other_info, _ = init_workspace(other)
    with open_store(other_info):
        pass
    expect_integrity(store, "INSERT INTO milestone_story(workspace_id, milestone_id, story_id) VALUES (?, 'M1', 'ST-1.1')", (other_info.workspace_id,))


def test_evidence_is_append_only_and_validated(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    with store.transaction() as c:
        PlanningRepository(c, info.workspace_id).record_evidence(story_id="ST-1.1", test_phase_id="S1", status="PASSED", reference="verification/s1/report.xml", recorded_by="tester")
    expect_integrity(store, "UPDATE story_test_evidence SET evidence_status='FAILED'")
    expect_integrity(store, "DELETE FROM story_test_evidence")
    expect_integrity(store, "INSERT INTO story_test_evidence(id, workspace_id, story_id, test_phase_id, evidence_status, evidence_ref, recorded_by, recorded_at) SELECT 'e2', workspace_id, 'ST-1.1', 'S1', 'MAYBE', 'r', 'x', 'now' FROM story LIMIT 1")
    expect_integrity(store, "INSERT INTO story_test_evidence(id, workspace_id, story_id, test_phase_id, evidence_status, evidence_ref, recorded_by, recorded_at) SELECT 'e3', workspace_id, 'ST-1.1', 'S1', 'PASSED', '', 'x', 'now' FROM story LIMIT 1")


def test_reimport_refuses_to_discard_evidence_and_group_membership(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    with store.transaction() as c:
        PlanningRepository(c, info.workspace_id).record_evidence(story_id="ST-1.1", test_phase_id="S1", status="PASSED", reference="r", recorded_by="t")
    with pytest.raises(DridhError, match="discard recorded test evidence|discard"):
        with store.transaction() as c:
            doc, rev = _revision(c, info, PATHS["central"], "central_plan", CENTRAL)
            PlanningRepository(c, info.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(CENTRAL, PATHS["central"]))
    assert rows(store, "SELECT COUNT(*) FROM story") == [(3,)]


# ---------------------------------------------------------------------------
# Imports: refusal, provenance, idempotence and rollback
# ---------------------------------------------------------------------------


def test_invalid_central_plan_is_refused_and_writes_nothing(workspace) -> None:  # type: ignore[no-untyped-def]
    cyclic = CENTRAL.replace("| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | None |", "| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | ST-1.2 |")
    store = open_store(workspace)
    store.open()
    try:
        with pytest.raises(DridhError):
            with store.transaction() as c:
                doc, rev = _revision(c, workspace, PATHS["central"], "central_plan", cyclic)
                PlanningRepository(c, workspace.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(cyclic, PATHS["central"]))
        assert rows(store, "SELECT COUNT(*) FROM story") == [(0,)]
    finally:
        store.close()


def test_central_plan_needs_its_components_and_use_cases(workspace) -> None:  # type: ignore[no-untyped-def]
    store = open_store(workspace)
    store.open()
    try:
        with pytest.raises(DridhError, match="Components not found"):
            with store.transaction() as c:
                doc, rev = _revision(c, workspace, PATHS["central"], "central_plan", CENTRAL)
                PlanningRepository(c, workspace.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(CENTRAL, PATHS["central"]))
    finally:
        store.close()


def test_phase_file_that_disagrees_with_the_central_plan_is_refused(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    drifted = PHASE.replace("| **Unlocks** | ST-1.2 |", "| **Unlocks** | — |")
    with pytest.raises(DridhError, match="disagrees"):
        with store.transaction() as c:
            doc, rev = _revision(c, info, "docs/phase-drift.md", "phase_file", drifted)
            PlanningRepository(c, info.workspace_id).import_phase(document_id=doc, revision_id=rev, model=validated_phase(drifted, "phase-drift.md"))


def test_repeated_central_import_is_idempotent(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    before = rows(store, "SELECT story_id, status, phase_number FROM story ORDER BY story_id")
    before_edges = rows(store, "SELECT COUNT(*) FROM story_dependency")
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["central"], "central_plan", CENTRAL)
        PlanningRepository(c, info.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(CENTRAL, PATHS["central"]))
    assert rows(store, "SELECT story_id, status, phase_number FROM story ORDER BY story_id") == before
    assert rows(store, "SELECT COUNT(*) FROM story_dependency") == before_edges


def test_failed_central_import_rolls_back_and_keeps_the_previous_projection(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    before_stories = rows(store, "SELECT story_id FROM story ORDER BY story_id")
    before_phases = rows(store, "SELECT COUNT(*) FROM component_phase")
    # Validator-clean in Python, but ST-1.1 (phase 1) depends on ST-3.1 (phase 3): the database refuses it mid-import.
    bad = CentralModel(
        stories=(
            StoryRow("ST-3.1", "Late prerequisite", "CMP-02", 3, "NOT_STARTED", 1, ("UC-02",), (), (), ()),
            StoryRow("ST-1.1", "Early story", "CMP-01", 1, "NOT_STARTED", 2, ("UC-01",), ("ST-3.1",), (), ()),
        ),
        milestones=(MilestoneRow("M5", "t", "c", None, "NOT_STARTED", 1),),
        risks=(),
    )
    with pytest.raises(DridhError):
        with store.transaction() as c:
            doc, rev = _revision(c, info, "docs/bad.md", "central_plan", CENTRAL)
            PlanningRepository(c, info.workspace_id).import_central(document_id=doc, revision_id=rev, model=bad)
    assert rows(store, "SELECT story_id FROM story ORDER BY story_id") == before_stories
    assert rows(store, "SELECT COUNT(*) FROM component_phase") == before_phases
    assert rows(store, "SELECT COUNT(*) FROM milestone WHERE milestone_id='M5'") == [(0,)]


def test_stale_and_never_imported_planning_documents_are_reported(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    edited = CENTRAL.replace("Domain model", "Domain model v2")
    with store.transaction() as c:
        doc, rev = _revision(c, info, PATHS["central"], "central_plan", edited)
    repository = PlanningRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    current = DocumentRepository(store.connection, info.workspace_id).current_revision(PATHS["central"])  # type: ignore[arg-type]
    stale = repository.findings(relative_path=PATHS["central"], document_id=ids["central"], kind="CENTRAL_PLAN", current_revision_id=current.id, current_content=edited)  # type: ignore[union-attr]
    assert [f.code for f in stale][:1] == ["PLANNING_STALE"]
    never = repository.findings(relative_path="docs/none.md", document_id="nope", kind="CENTRAL_PLAN", current_revision_id="x", current_content=CENTRAL)
    assert [f.code for f in never] == ["PLANNING_NEVER_IMPORTED"]


def test_verify_detects_tampered_planning_rows(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    repository = PlanningRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    current = DocumentRepository(store.connection, info.workspace_id).current_revision(PATHS["central"])  # type: ignore[arg-type]
    assert repository.findings(relative_path=PATHS["central"], document_id=ids["central"], kind="CENTRAL_PLAN", current_revision_id=current.id, current_content=CENTRAL) == []  # type: ignore[union-attr]
    store.connection.execute("DROP TRIGGER story_definition_immutable_update")  # type: ignore[union-attr]
    store.connection.execute("UPDATE story SET title='Forged' WHERE story_id='ST-1.1'")  # type: ignore[union-attr]
    findings = repository.findings(relative_path=PATHS["central"], document_id=ids["central"], kind="CENTRAL_PLAN", current_revision_id=current.id, current_content=CENTRAL)  # type: ignore[union-attr]
    assert [f.code for f in findings] == ["PLANNING_MISMATCH"]


def test_phase_and_test_plan_verify_clean_after_import(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    documents = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    repository = PlanningRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    for key, kind, text, doc in ((PATHS["phase"], "PHASE_FILE", PHASE, ids["phase"]), (PATHS["tests"], "TEST_PLAN", TEST_PLAN, ids["tests"])):
        current = documents.current_revision(key)
        assert repository.findings(relative_path=key, document_id=doc, kind=kind, current_revision_id=current.id, current_content=text) == []  # type: ignore[union-attr]


def test_risks_milestones_and_test_phases_are_projected(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    assert rows(store, "SELECT risk_id, likelihood, owner FROM risk") == [("R1", "Medium", "Platform team")]
    assert rows(store, "SELECT milestone_id, status FROM milestone") == [("M1", "NOT_STARTED")]
    assert rows(store, "SELECT test_phase_id, dependency_mode FROM test_phase ORDER BY test_phase_id") == [("D1", "DOCKER_COMPOSE"), ("S1", "TESTCONTAINERS"), ("X1", "WIREMOCK_EXCEPTION")]
    assert rows(store, "SELECT test_phase_id, component_id FROM test_phase_component WHERE test_phase_id='D1' ORDER BY component_id") == [("D1", "CMP-01"), ("D1", "CMP-02")]
    assert rows(store, "SELECT COUNT(*) FROM story_use_case WHERE flow_id IS NOT NULL") == [(3,)]


def test_duplicate_story_identifier_from_another_document_is_refused(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    with pytest.raises(DridhError, match="another planning document"):
        with store.transaction() as c:
            doc, rev = _revision(c, info, "docs/copy-central.md", "central_plan", CENTRAL)
            PlanningRepository(c, info.workspace_id).import_central(document_id=doc, revision_id=rev, model=validated_central(CENTRAL, "copy-central.md"))


def test_workspace_isolation_for_planning_rows(planned, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    other = tmp_path / "other"
    other.mkdir()
    other_info, _ = init_workspace(other)
    with open_store(other_info) as other_store:
        assert PlanningRepository(other_store.connection, other_info.workspace_id).graph() == {}
    expect_integrity(store, "INSERT INTO story(id, workspace_id, document_id, source_revision_id, story_id, title, component_id, phase_number, status, position) SELECT 'alien', ?, document_id, source_revision_id, 'ST-8.8', 't', 'CMP-01', 1, 'NOT_STARTED', 9 FROM story LIMIT 1", (other_info.workspace_id,))


def test_planning_rows_survive_reopen_with_ledger_intact(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, _ = planned
    with store.transaction() as c:
        append_ledger(c, None, "test.event", "tester", {"n": 1})
    head = verify_ledger(store.connection)["head"]  # type: ignore[arg-type]
    store.close()
    with open_store(info) as reopened:
        assert rows(reopened, "SELECT COUNT(*) FROM story") == [(3,)]
        verdict = verify_ledger(reopened.connection)  # type: ignore[arg-type]
        assert verdict["ok"] is True and verdict["head"] == head


# ---------------------------------------------------------------------------
# Migration from schema v6 with existing data
# ---------------------------------------------------------------------------


def test_v6_database_migrates_to_v7_preserving_all_prior_rows(workspace) -> None:  # type: ignore[no-untyped-def]

    store = open_store(workspace)
    store.open()
    try:
        import_chain(store, workspace)
        with store.transaction() as c:
            append_ledger(c, None, "test.event", "tester", {"n": 1})
        before = {
            "use_case": rows(store, "SELECT use_case_id, title FROM use_case ORDER BY use_case_id"),
            "flow": rows(store, "SELECT flow_id FROM use_case_flow ORDER BY flow_id"),
            "decision": rows(store, "SELECT decision_id, scope FROM design_decision ORDER BY decision_id, scope"),
            "mapping": rows(store, "SELECT COUNT(*) FROM use_case_component_mapping"),
            "revisions": rows(store, "SELECT COUNT(*) FROM document_revision"),
        }
        head = verify_ledger(store.connection)["head"]  # type: ignore[arg-type]
    finally:
        store.close()
    raw = sqlite3.connect(workspace.state_db, isolation_level=None)
    for name in [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()]:
        if name.startswith(("story", "component_phase", "milestone", "risk", "test_phase", "planning")):
            raw.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("test_phase_component", "test_phase_flow", "test_phase_use_case", "test_phase", "risk", "milestone_story", "milestone", "story_test_evidence", "story_parallel_member", "story_parallel_group", "story_parallel_pair", "story_dependency", "story_use_case", "story", "component_phase", "planning_import"):
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    raw.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    raw.close()
    assert schema_version(workspace.state_db) == 6

    result = migrate_to_current(workspace.state_db)
    assert [s["to_version"] for s in result["steps"]][-1] == CURRENT_SCHEMA_VERSION
    step = result["steps"][-1]
    assert step["migrated"] is True and step["backup_path"] and Path(step["backup_path"]).exists()
    assert schema_version(workspace.state_db) == CURRENT_SCHEMA_VERSION

    with open_store(workspace) as after:
        assert rows(after, "SELECT use_case_id, title FROM use_case ORDER BY use_case_id") == before["use_case"]
        assert rows(after, "SELECT flow_id FROM use_case_flow ORDER BY flow_id") == before["flow"]
        assert rows(after, "SELECT decision_id, scope FROM design_decision ORDER BY decision_id, scope") == before["decision"]
        assert rows(after, "SELECT COUNT(*) FROM use_case_component_mapping") == before["mapping"]
        assert rows(after, "SELECT COUNT(*) FROM document_revision") == before["revisions"]
        assert after.connection.execute("PRAGMA foreign_key_check").fetchall() == []  # type: ignore[union-attr]
        assert verify_ledger(after.connection)["head"] == head  # type: ignore[arg-type]
        assert rows(after, "SELECT COUNT(*) FROM story") == [(0,)]
    again = migrate_to_current(workspace.state_db)
    assert again["migrated"] is False


def test_migrated_database_enforces_the_new_planning_rules(workspace) -> None:  # type: ignore[no-untyped-def]
    store = open_store(workspace)
    store.open()
    try:
        import_chain(store, workspace)
    finally:
        store.close()
    raw = sqlite3.connect(workspace.state_db, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    raw.close()
    migrate_to_current(workspace.state_db)
    with open_store(workspace) as after:
        expect_integrity(after, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'cyc2', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-1.2' FROM story WHERE story_id='ST-1.2'", match="cycle")



def _add_story(store, info, story_id, phase, title="x"):  # type: ignore[no-untyped-def]
    with store.transaction() as c:
        c.execute(
            "INSERT INTO component_phase(id, workspace_id, document_id, source_revision_id, component_id, phase_number) "
            "SELECT ?, workspace_id, document_id, source_revision_id, 'CMP-01', ? FROM story WHERE story_id='ST-1.1' LIMIT 1",
            (f"cp-{story_id}", phase),
        )
        c.execute(
            "INSERT INTO story(id, workspace_id, document_id, source_revision_id, story_id, title, component_id, phase_number, status, position) "
            "SELECT ?, workspace_id, document_id, source_revision_id, ?, ?, 'CMP-01', ?, 'NOT_STARTED', 99 FROM story WHERE story_id='ST-1.1' LIMIT 1",
            (f"s-{story_id}", story_id, title, phase),
        )


# ---------------------------------------------------------------------------
# Bidirectional parallel independence (PR #8 review fix)
# ---------------------------------------------------------------------------


def test_scenario_a_dependency_before_parallel_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    """Scenario A: dependency first, then parallel declaration, rejected (existing trigger)."""
    store, info, _ = planned
    with store.transaction() as c:
        c.execute(
            "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) "
            "SELECT 'dep-a', workspace_id, document_id, source_revision_id, 'ST-2.1', 'ST-1.2' FROM story WHERE story_id='ST-2.1'",
        )
    expect_integrity(store, "INSERT INTO story_parallel_pair(id, workspace_id, document_id, source_revision_id, story_id, other_story_id) SELECT 'pp-a', workspace_id, document_id, source_revision_id, 'ST-1.2', 'ST-2.1' FROM story WHERE story_id='ST-1.2'", match="independent")
    assert rows(store, "SELECT COUNT(*) FROM story_parallel_pair WHERE id='pp-a'") == [(0,)]


def test_scenario_b_parallel_pair_before_dependency_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    """Scenario B: parallel declaration first, then dependency, rejected by the new trigger."""
    store, info, _ = planned
    with store.transaction() as c:
        c.execute(
            "INSERT INTO story_parallel_pair(id, workspace_id, document_id, source_revision_id, story_id, other_story_id) "
            "SELECT 'pp-b', workspace_id, document_id, source_revision_id, 'ST-1.2', 'ST-2.1' FROM story WHERE story_id='ST-1.2'",
        )
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'dep-b1', workspace_id, document_id, source_revision_id, 'ST-2.1', 'ST-1.2' FROM story WHERE story_id='ST-2.1'", match="parallel")
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'dep-b2', workspace_id, document_id, source_revision_id, 'ST-1.2', 'ST-2.1' FROM story WHERE story_id='ST-1.2'", match="parallel")
    assert rows(store, "SELECT COUNT(*) FROM story_dependency WHERE id IN ('dep-b1','dep-b2')") == [(0,)]


def test_scenario_c_parallel_group_rejects_a_later_dependency(planned) -> None:  # type: ignore[no-untyped-def]
    """Scenario C: a group is created, then a dependency between members is rejected."""
    store, info, _ = planned
    with store.transaction() as c:
        PlanningRepository(c, info.workspace_id).create_parallel_group("wave", ["ST-1.2", "ST-2.1"])
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'dep-c', workspace_id, document_id, source_revision_id, 'ST-2.1', 'ST-1.2' FROM story WHERE story_id='ST-2.1'", match="parallel group")
    assert rows(store, "SELECT COUNT(*) FROM story_dependency WHERE id='dep-c'") == [(0,)]


def test_scenario_c_dependency_before_group_is_rejected(planned) -> None:  # type: ignore[no-untyped-def]
    """Scenario C reverse: a dependency is inserted first, then group creation is rejected."""
    store, info, _ = planned
    with store.transaction() as c:
        c.execute(
            "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) "
            "SELECT 'dep-c2', workspace_id, document_id, source_revision_id, 'ST-2.1', 'ST-1.2' FROM story WHERE story_id='ST-2.1'",
        )
    with pytest.raises(DridhError, match="not created"):
        with store.transaction() as c:
            PlanningRepository(c, info.workspace_id).create_parallel_group("bad", ["ST-1.2", "ST-2.1"])
    assert rows(store, "SELECT COUNT(*) FROM story_parallel_group WHERE group_name='bad'") == [(0,)]


def test_scenario_d_valid_independence_remains_accepted(planned) -> None:  # type: ignore[no-untyped-def]
    """Scenario D: unrelated stories stay parallelisable in either order."""
    store, info, _ = planned
    with store.transaction() as c:
        c.execute(
            "INSERT INTO story_parallel_pair(id, workspace_id, document_id, source_revision_id, story_id, other_story_id) "
            "SELECT 'pp-d', workspace_id, document_id, source_revision_id, 'ST-1.2', 'ST-2.1' FROM story WHERE story_id='ST-1.2'",
        )
    with store.transaction() as c:
        PlanningRepository(c, info.workspace_id).create_parallel_group("wave-d", ["ST-1.2", "ST-2.1"])
    assert rows(store, "SELECT COUNT(*) FROM story_parallel_pair WHERE id='pp-d'") == [(1,)]
    assert rows(store, "SELECT COUNT(*) FROM story_parallel_group WHERE group_name='wave-d'") == [(1,)]


def test_scenario_e_existing_invariants_still_hold(planned) -> None:  # type: ignore[no-untyped-def]
    """Scenario E: cycle detection, phase ordering and deterministic order remain intact."""
    store, info, _ = planned
    # cycle
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'cyc', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-1.2' FROM story WHERE story_id='ST-1.1'", match="cycle")
    # phase ordering: use a story in a later phase that is not parallel to anything
    _add_story(store, info, "ST-9.9", 9)
    expect_integrity(store, "INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id) SELECT 'late', workspace_id, document_id, source_revision_id, 'ST-1.1', 'ST-9.9' FROM story WHERE story_id='ST-1.1'", match="later phase")
    # deterministic order
    repository = PlanningRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    assert repository.topological_order() == repository.topological_order()
    # rollback leaves no partial rows
    before = rows(store, "SELECT COUNT(*) FROM story_dependency")
    with pytest.raises(DridhError):
        with store.transaction() as c:
            PlanningRepository(c, info.workspace_id).create_parallel_group("rollback", ["ST-1.1", "ST-1.2"])
    assert rows(store, "SELECT COUNT(*) FROM story_dependency") == before
    assert rows(store, "SELECT COUNT(*) FROM story_parallel_group WHERE group_name='rollback'") == [(0,)]




def test_planning_verify_compares_unlocks_with_persisted_reverse_edges(planned) -> None:  # type: ignore[no-untyped-def]
    store, info, ids = planned
    # Remove the existing parallel declaration first: a new prerequisite
    # between these stories would correctly violate that database invariant.
    # The resulting persisted graph remains valid but diverges from the Markdown.
    store.connection.execute(
        "DELETE FROM story_parallel_pair WHERE workspace_id = ? AND story_id = 'ST-1.1' AND other_story_id = 'ST-2.1'",
        (info.workspace_id,),
    )
    # Add a valid persisted dependency from ST-2.1 to ST-1.2. This changes the
    # reverse unlock projection without changing the current Markdown revision.
    store.connection.execute(
        """INSERT INTO story_dependency(id, workspace_id, document_id, source_revision_id, story_id, depends_on_story_id, rationale)
           SELECT 'phase6-unlock-probe', workspace_id, document_id, source_revision_id, 'ST-2.1', 'ST-1.2', NULL
           FROM story WHERE workspace_id = ? AND story_id = 'ST-2.1'""",
        (info.workspace_id,),
    )
    documents = DocumentRepository(store.connection, info.workspace_id)
    revision = documents.current_revision(PATHS["central"])
    assert revision is not None
    findings = PlanningRepository(store.connection, info.workspace_id).findings(
        relative_path=PATHS["central"],
        document_id=ids["central"],
        kind="CENTRAL_PLAN",
        current_revision_id=revision.id,
        current_content=CENTRAL,
    )
    mismatch = next((finding for finding in findings if finding.code == "PLANNING_UNLOCK_MISMATCH"), None)
    assert mismatch is not None
    assert mismatch.entity_id == "ST-1.2"
    assert mismatch.expected == "ST-2.1"
    assert mismatch.actual == "—"
    assert mismatch.line is not None
