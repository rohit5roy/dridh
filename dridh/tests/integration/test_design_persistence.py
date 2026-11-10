"""Design domain persistence against real SQLite (Phase 5 increment 2). No mocks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dridh.common.errors import DridhError
from dridh.state.design import ComponentRecord, DesignRepository, TddModel, parse_tdd_model, validated_master_model, validated_tdd_model
from dridh.state.documents import DocumentRepository
from dridh.state.migration import CURRENT_SCHEMA_VERSION, migrate_to_current, schema_version
from dridh.state.store import STATE_SCHEMA_VERSION, StateStore, append_ledger, verify_ledger
from dridh.state.usecases import UseCaseRepository, validated_model
from dridh.workspace.layout import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
UC_TEXT = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
TDD_TEXT = (FIXTURES / "feature_tdd.md").read_text(encoding="utf-8")
MASTER_TEXT = (FIXTURES / "COMPONENT-MASTER.md").read_text(encoding="utf-8")
UC_PATH, TDD_PATH, MASTER_PATH = "docs/001-USE-CASES.md", "docs/feature_tdd.md", "docs/COMPONENT-MASTER.md"


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


def import_uc(store: StateStore, info) -> None:  # type: ignore[no-untyped-def]
    with store.transaction() as connection:
        documents = DocumentRepository(connection, info.workspace_id)
        revision, _ = documents.record_revision(relative_path=UC_PATH, document_type="use_cases", content=UC_TEXT, standards_version="2.1.0", source_path=UC_PATH)
        UseCaseRepository(connection, info.workspace_id).replace_projection(
            document_id=str(documents.document_metadata(UC_PATH)["id"]), revision_id=revision.id, model=validated_model(UC_TEXT, UC_PATH)  # type: ignore[index]
        )


def import_tdd(store: StateStore, info, text: str = TDD_TEXT, path: str = TDD_PATH):  # type: ignore[no-untyped-def]
    with store.transaction() as connection:
        documents = DocumentRepository(connection, info.workspace_id)
        revision, _ = documents.record_revision(relative_path=path, document_type="tdd", content=text, standards_version="2.1.0", source_path=path)
        document_id = str(documents.document_metadata(path)["id"])  # type: ignore[index]
        DesignRepository(connection, info.workspace_id).replace_tdd(document_id=document_id, revision_id=revision.id, model=validated_tdd_model(text, path))
    return document_id, revision.id


def import_master(store: StateStore, info, text: str = MASTER_TEXT, path: str = MASTER_PATH):  # type: ignore[no-untyped-def]
    with store.transaction() as connection:
        documents = DocumentRepository(connection, info.workspace_id)
        revision, _ = documents.record_revision(relative_path=path, document_type="component_master", content=text, standards_version="2.1.0", source_path=path)
        document_id = str(documents.document_metadata(path)["id"])  # type: ignore[index]
        DesignRepository(connection, info.workspace_id).replace_component_master(document_id=document_id, revision_id=revision.id, model=validated_master_model(text, path))
    return document_id, revision.id


def count(store: StateStore, table: str) -> int:
    return store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[union-attr]


@pytest.fixture
def design(workspace):  # type: ignore[no-untyped-def]
    store = open_store(workspace)
    store.open()
    import_uc(store, workspace)
    tdd_id, tdd_rev = import_tdd(store, workspace)
    master_id, master_rev = import_master(store, workspace)
    yield store, workspace, tdd_id, tdd_rev, master_id, master_rev
    store.close()


# ---------------------------------------------------------------------------
# Schema and imports
# ---------------------------------------------------------------------------


def test_fresh_database_has_design_tables_at_current_version(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}  # type: ignore[union-attr]
    assert {"component", "design_decision", "use_case_component_mapping", "api_event_mapping", "design_import"} <= tables
    assert STATE_SCHEMA_VERSION == CURRENT_SCHEMA_VERSION


def test_tdd_import_projects_components_decisions_mappings_and_api_references(design) -> None:  # type: ignore[no-untyped-def]
    store, _, tdd_id, _, _, _ = design
    model = parse_tdd_model(TDD_TEXT)
    assert len(model.components) == 2 and len(model.decisions) == 1
    # Table 13 row 1: 3 flows x 1 component x 1 decision; row 2: 2 flows x 2 components x no decision.
    assert len(model.mappings) == 3 + 4
    assert sum(1 for a in model.api_events if a.reference_kind == "EVENT") == 4
    assert DesignRepository(store.connection, design[1].workspace_id).stored_tdd(tdd_id) == model  # type: ignore[arg-type]


def test_tdd_import_is_refused_until_its_use_cases_are_imported(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        with pytest.raises(DridhError) as refused:
            import_tdd(store, workspace)
        assert "use cases" in refused.value.message
        assert count(store, "component") == 0 and count(store, "use_case_component_mapping") == 0


def test_master_import_is_refused_until_its_component_is_defined(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        with pytest.raises(DridhError):
            import_master(store, workspace)
        assert count(store, "design_decision") == 0


def test_component_master_imports_cdd_rows_scoped_to_the_component(design) -> None:  # type: ignore[no-untyped-def]
    store, info, _, _, master_id, _ = design
    master = DesignRepository(store.connection, info.workspace_id).stored_master(master_id)  # type: ignore[arg-type]
    assert master.component_id == "CMP-01"
    assert [(d.decision_id, d.scope, d.component_id) for d in master.decisions] == [("CDD-1", "COMPONENT", "CMP-01")]


def test_reimport_of_the_same_tdd_replaces_rows_with_identical_content(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, _, _, _ = design
    before = parse_tdd_model(TDD_TEXT)
    import_tdd(store, info)
    assert DesignRepository(store.connection, info.workspace_id).stored_tdd(tdd_id) == before  # type: ignore[arg-type]
    assert count(store, "component") == 2 and count(store, "use_case_component_mapping") == 7


def test_edited_tdd_produces_stale_projection_until_reimported(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, old_rev, _, _ = design
    edited = TDD_TEXT.replace("Orders must be captured and paid for without duplicate charges.", "Orders must be captured, paid for, and never charged twice.")
    with store.transaction() as connection:
        DocumentRepository(connection, info.workspace_id).record_revision(relative_path=TDD_PATH, document_type="tdd", content=edited, standards_version="2.1.0", source_path=TDD_PATH)
    repository = DesignRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    current = DocumentRepository(store.connection, info.workspace_id).current_revision(TDD_PATH)  # type: ignore[arg-type]
    findings = repository.findings(relative_path=TDD_PATH, document_id=tdd_id, kind="TDD", current_revision_id=current.id, current_content=edited)  # type: ignore[union-attr]
    assert [f.code for f in findings] == ["DESIGN_PROJECTION_STALE"]
    assert old_rev != current.id  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Feature DD-N versus component CDD-N
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision_id", "scope", "component_id"),
    [
        ("CDD-1", "FEATURE", None),
        ("DD-1", "COMPONENT", "CMP-01"),
        ("DD-1", "FEATURE", "CMP-01"),
        ("CDD-1", "COMPONENT", None),
        ("DD-x", "FEATURE", None),
        ("DD-", "FEATURE", None),
        ("CDD-1a", "COMPONENT", "CMP-01"),
        ("XX-1", "FEATURE", None),
    ],
)
def test_decision_scope_and_prefix_cannot_be_mixed(design, decision_id: str, scope: str, component_id: str | None) -> None:  # type: ignore[no-untyped-def]
    store, info, _, tdd_rev, _, _ = design
    document_id = str(DocumentRepository(store.connection, info.workspace_id).document_metadata(MASTER_PATH)["id"])  # type: ignore[arg-type, index]
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO design_decision(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position) VALUES ('bad', ?, ?, ?, ?, ?, ?, 't', 'd', 9)",
            (info.workspace_id, document_id, tdd_rev, decision_id, scope, component_id),
        )


def test_table_13_may_reference_only_feature_decisions(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, _, _, _ = design
    with pytest.raises(sqlite3.IntegrityError, match="feature-level"):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO use_case_component_mapping(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, design_decision_id, position) "
            "SELECT 'bad', workspace_id, document_id, source_revision_id, 'UC-01', 'SF-01.1', 'CMP-01', 'CDD-1', 99 FROM use_case_component_mapping LIMIT 1"
        )


# ---------------------------------------------------------------------------
# Referential integrity of mappings and API references
# ---------------------------------------------------------------------------


def _mapping_sql(**overrides) -> tuple[str, tuple]:  # type: ignore[no-untyped-def]
    row = {"id": "m-x", "workspace_id": None, "document_id": None, "source_revision_id": None, "use_case_id": "UC-01", "flow_id": "SF-01.1", "component_id": "CMP-01", "design_decision_id": None, "position": 99}
    row.update(overrides)
    columns = list(row)
    return f"INSERT INTO use_case_component_mapping({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", tuple(row.values())


def test_mapping_flow_must_belong_to_its_use_case(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    sql, params = _mapping_sql(workspace_id=info.workspace_id, document_id=tdd_id, source_revision_id=tdd_rev, flow_id="SF-02.1", use_case_id="UC-01")
    with pytest.raises(sqlite3.IntegrityError, match="flow must belong"):
        store.connection.execute(sql, params)  # type: ignore[union-attr]


@pytest.mark.parametrize("overrides", [{"use_case_id": "UC-09", "flow_id": None}, {"component_id": "CMP-09"}])
def test_mapping_rejects_unknown_use_case_or_component(design, overrides) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    sql, params = _mapping_sql(workspace_id=info.workspace_id, document_id=tdd_id, source_revision_id=tdd_rev, **overrides)
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(sql, params)  # type: ignore[union-attr]


def test_api_reference_kind_is_constrained(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO api_event_mapping(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, reference_kind, reference_name, position) VALUES ('a', ?, ?, ?, 'UC-01', NULL, 'CMP-01', 'REST', 'x', 9)",
            (info.workspace_id, tdd_id, tdd_rev),
        )


# ---------------------------------------------------------------------------
# Dependency between component master and TDD, reconciliation and tampering
# ---------------------------------------------------------------------------


def test_removing_a_component_leaves_its_cdd_unresolved_and_is_reported(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, master_id, _ = design
    # A revised TDD that no longer defines CMP-01. Table 13 references are dropped in this model.
    reduced = TddModel(components=(ComponentRecord("CMP-02", "Notification Service", "x", 1),), decisions=(), mappings=(), api_events=())
    with store.transaction() as connection:
        revision, _ = DocumentRepository(connection, info.workspace_id).record_revision(relative_path=TDD_PATH, document_type="tdd", content=TDD_TEXT + "\n<!-- r2 -->\n", standards_version="2.1.0", source_path=TDD_PATH)
        DesignRepository(connection, info.workspace_id).replace_tdd(document_id=tdd_id, revision_id=revision.id, model=reduced)
    repository = DesignRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    findings = repository.findings(relative_path=MASTER_PATH, document_id=master_id, kind="COMPONENT_MASTER", current_revision_id=DocumentRepository(store.connection, info.workspace_id).current_revision(MASTER_PATH).id, current_content=MASTER_TEXT)  # type: ignore[arg-type, union-attr]
    assert "CDD_COMPONENT_UNRESOLVED" in {f.code for f in findings}


def test_verify_passes_after_import_and_detects_tampering(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    current = DocumentRepository(store.connection, info.workspace_id).current_revision(TDD_PATH)  # type: ignore[arg-type]
    repository = DesignRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
    assert repository.findings(relative_path=TDD_PATH, document_id=tdd_id, kind="TDD", current_revision_id=current.id, current_content=TDD_TEXT) == []  # type: ignore[union-attr]
    store.connection.execute("DROP TRIGGER component_immutable_update")  # type: ignore[union-attr]
    store.connection.execute("UPDATE component SET definition_text='tampered' WHERE component_id='CMP-01'")  # type: ignore[union-attr]
    findings = repository.findings(relative_path=TDD_PATH, document_id=tdd_id, kind="TDD", current_revision_id=current.id, current_content=TDD_TEXT)  # type: ignore[union-attr]
    assert [f.code for f in findings] == ["DESIGN_PROJECTION_MISMATCH"]


def test_never_imported_document_is_reported(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        with store.transaction() as connection:
            revision, _ = DocumentRepository(connection, workspace.workspace_id).record_revision(relative_path=TDD_PATH, document_type="tdd", content=TDD_TEXT, standards_version="2.1.0", source_path=TDD_PATH)
            document_id = str(DocumentRepository(connection, workspace.workspace_id).document_metadata(TDD_PATH)["id"])  # type: ignore[index]
        findings = DesignRepository(store.connection, workspace.workspace_id).findings(relative_path=TDD_PATH, document_id=document_id, kind="TDD", current_revision_id=revision.id, current_content=TDD_TEXT)
    assert [f.code for f in findings] == ["DESIGN_NEVER_IMPORTED"]


def test_workspace_isolation_for_components(design, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    other_project = tmp_path / "other"
    other_project.mkdir()
    other, _ = init_workspace(other_project)
    with StateStore(other.state_db, other.workspace_id):
        pass
    with pytest.raises(sqlite3.IntegrityError, match="another workspace|component must belong"):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO component(id, workspace_id, document_id, source_revision_id, component_id, component_name, definition_text, position) VALUES ('x', ?, ?, ?, 'CMP-07', 'n', 'd', 9)",
            (other.workspace_id, tdd_id, tdd_rev),
        )
    with open_store(other) as other_store:
        assert DesignRepository(other_store.connection, other.workspace_id).stored_tdd(tdd_id).components == ()  # type: ignore[arg-type]


def test_reopen_preserves_design_rows(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, _, _, _ = design
    store.close()
    with open_store(info) as reopened:
        assert count(reopened, "component") == 2
        assert verify_ledger(reopened.connection)["ok"] is True  # type: ignore[arg-type]


def test_v4_workspace_upgrades_keeping_use_cases_and_ledger(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        import_uc(store, workspace)
        append_ledger(store.connection, None, "test.event", "tester", {"n": 1})  # type: ignore[arg-type]
        head = verify_ledger(store.connection)["head"]  # type: ignore[arg-type]
    raw = sqlite3.connect(workspace.state_db, isolation_level=None)
    for name in [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'component%' OR name LIKE 'design%' OR name LIKE 'mapping%' OR name LIKE 'api_event%')").fetchall()]:
        raw.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("api_event_mapping", "use_case_component_mapping", "design_import", "design_decision", "component"):
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    raw.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    raw.close()
    result = migrate_to_current(workspace.state_db)
    assert [s["to_version"] for s in result["steps"]] == [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert schema_version(workspace.state_db) == CURRENT_SCHEMA_VERSION
    with open_store(workspace) as store:
        assert count(store, "use_case_flow") == 8
        verdict = verify_ledger(store.connection)  # type: ignore[arg-type]
        assert verdict["ok"] is True and verdict["head"] == head


# ---------------------------------------------------------------------------
# CDD-N uniqueness is per component; DD-N is feature-wide (TABLE-STANDARDS 2, 13.1)
# ---------------------------------------------------------------------------

MASTER_CMP2 = MASTER_TEXT.replace("CMP-01", "CMP-02")
MASTER_CMP2_PATH = "docs/COMPONENT-MASTER-2.md"


def _cdd(connection: sqlite3.Connection, component: str) -> list[tuple]:
    return [tuple(r) for r in connection.execute(
        "SELECT component_id, decision_id FROM design_decision WHERE scope='COMPONENT' AND component_id=? ORDER BY decision_id", (component,)
    )]


def test_two_components_can_each_define_cdd_1(design) -> None:  # type: ignore[no-untyped-def]
    store, info, _, _, _, _ = design
    import_master(store, info, MASTER_CMP2, MASTER_CMP2_PATH)
    both = store.connection.execute(  # type: ignore[union-attr]
        "SELECT component_id, decision_id FROM design_decision WHERE scope='COMPONENT' AND decision_id='CDD-1' ORDER BY component_id"
    ).fetchall()
    assert [tuple(r) for r in both] == [("CMP-01", "CDD-1"), ("CMP-02", "CDD-1")]


def test_the_same_component_cannot_define_cdd_1_twice_across_documents(design) -> None:  # type: ignore[no-untyped-def]
    store, info, _, _, _, _ = design
    with pytest.raises(DridhError):
        import_master(store, info, MASTER_TEXT, "docs/COMPONENT-MASTER-duplicate.md")
    assert [tuple(r) for r in store.connection.execute("SELECT component_id, decision_id FROM design_decision WHERE scope='COMPONENT'")] == [("CMP-01", "CDD-1")]  # type: ignore[union-attr]


def test_the_same_component_cannot_define_cdd_1_twice_in_one_document(design) -> None:  # type: ignore[no-untyped-def]
    store, info, _, tdd_rev, master_id, master_rev = design
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO design_decision(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position) "
            "VALUES ('dup', ?, ?, ?, 'CDD-1', 'COMPONENT', 'CMP-01', 'again', 'd', 9)",
            (info.workspace_id, master_id, master_rev),
        )


def test_feature_dd_stays_unique_per_workspace(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO design_decision(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position) "
            "VALUES ('dd-dup', ?, ?, ?, 'DD-1', 'FEATURE', NULL, 'again', 'd', 9)",
            (info.workspace_id, tdd_id, tdd_rev),
        )


def test_feature_dd_and_component_cdd_with_the_same_number_coexist(design) -> None:  # type: ignore[no-untyped-def]
    store, _, _, _, _, _ = design
    rows = {tuple(r) for r in store.connection.execute("SELECT decision_id, scope FROM design_decision")}  # type: ignore[union-attr]
    assert ("DD-1", "FEATURE") in rows and ("CDD-1", "COMPONENT") in rows
    assert store.connection.execute("SELECT COUNT(*) FROM design_decision WHERE decision_id IN ('DD-1','CDD-1')").fetchone()[0] == 2  # type: ignore[union-attr]


def test_table_13_still_accepts_only_feature_decisions(design) -> None:  # type: ignore[no-untyped-def]
    store, info, tdd_id, tdd_rev, _, _ = design
    store.connection.execute(  # type: ignore[union-attr]
        "INSERT INTO use_case_component_mapping(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, design_decision_id, position) "
        "VALUES ('ok-dd', ?, ?, ?, 'UC-02', 'SF-02.1', 'CMP-01', 'DD-1', 90)",
        (info.workspace_id, tdd_id, tdd_rev),
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO use_case_component_mapping(id, workspace_id, document_id, source_revision_id, use_case_id, flow_id, component_id, design_decision_id, position) "
            "VALUES ('bad-cdd', ?, ?, ?, 'UC-02', 'SF-02.1', 'CMP-01', 'CDD-1', 91)",
            (info.workspace_id, tdd_id, tdd_rev),
        )


def _raw(info) -> sqlite3.Connection:  # type: ignore[no-untyped-def]
    connection = sqlite3.connect(info.state_db, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        "design_decision": [tuple(r) for r in connection.execute("SELECT id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position FROM design_decision ORDER BY id")],
        "use_case_component_mapping": [tuple(r) for r in connection.execute("SELECT id, document_id, use_case_id, flow_id, component_id, design_decision_id, position FROM use_case_component_mapping ORDER BY id")],
    }


def test_v5_database_refuses_cdd_1_for_a_second_component_then_migrates_without_loss(workspace) -> None:  # type: ignore[no-untyped-def]
    from dridh.state.migration import _DESIGN_V5_STATEMENTS

    with open_store(workspace) as store:
        import_uc(store, workspace)
    # Rebuild the design objects in their shipped v5 shape, then write real rows through the repositories.
    raw = _raw(workspace)
    for name in [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'component%' OR name LIKE 'design%' OR name LIKE 'mapping%' OR name LIKE 'api_event%' OR name LIKE 'use_case_component%')").fetchall()]:
        raw.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("api_event_mapping", "use_case_component_mapping", "design_import", "design_decision", "component"):
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    for statement in _DESIGN_V5_STATEMENTS:
        raw.execute(statement)
    raw.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    raw.execute("BEGIN IMMEDIATE")
    tdd_id, tdd_rev = _import_tdd_raw(raw, workspace)
    _import_master_raw(raw, workspace, MASTER_TEXT, MASTER_PATH)
    raw.execute("COMMIT")
    raw.execute("BEGIN IMMEDIATE")
    with pytest.raises(DridhError):
        _import_master_raw(raw, workspace, MASTER_CMP2, MASTER_CMP2_PATH)
    raw.execute("ROLLBACK")
    before = _snapshot(raw)
    raw.close()
    assert len(before["design_decision"]) == 2 and len(before["use_case_component_mapping"]) == 7

    from dridh.state.migration import migrate_to_current
    result = migrate_to_current(workspace.state_db)
    assert [s["to_version"] for s in result["steps"]] == [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    step = result["steps"][0]
    assert step["rows_preserved"] == {"design_decision": 2, "use_case_component_mapping": 7}
    assert Path(step["backup_path"]).exists()

    raw = _raw(workspace)
    assert _snapshot(raw) == before, "migration must not change any persisted decision or mapping"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    raw.execute("BEGIN IMMEDIATE")
    _import_master_raw(raw, workspace, MASTER_CMP2, MASTER_CMP2_PATH)
    raw.execute("COMMIT")
    assert [tuple(r) for r in raw.execute("SELECT component_id, decision_id FROM design_decision WHERE decision_id='CDD-1' ORDER BY component_id")] == [("CMP-01", "CDD-1"), ("CMP-02", "CDD-1")]
    raw.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO design_decision(id, workspace_id, document_id, source_revision_id, decision_id, scope, component_id, title, definition_text, position) VALUES ('f2', ?, ?, ?, 'DD-1', 'FEATURE', NULL, 't', 'd', 99)", (workspace.workspace_id, tdd_id, tdd_rev))
    raw.execute("ROLLBACK")
    raw.close()
    with open_store(workspace) as store:
        assert verify_ledger(store.connection)["ok"] is True  # type: ignore[arg-type]
    assert schema_version(workspace.state_db) == CURRENT_SCHEMA_VERSION


def _import_tdd_raw(raw: sqlite3.Connection, info) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    documents = DocumentRepository(raw, info.workspace_id)
    revision, _ = documents.record_revision(relative_path=TDD_PATH, document_type="tdd", content=TDD_TEXT, standards_version="2.1.0", source_path=TDD_PATH)
    document_id = str(documents.document_metadata(TDD_PATH)["id"])  # type: ignore[index]
    DesignRepository(raw, info.workspace_id).replace_tdd(document_id=document_id, revision_id=revision.id, model=validated_tdd_model(TDD_TEXT, TDD_PATH))
    return document_id, revision.id


def _import_master_raw(raw: sqlite3.Connection, info, text: str, path: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    documents = DocumentRepository(raw, info.workspace_id)
    revision, _ = documents.record_revision(relative_path=path, document_type="component_master", content=text, standards_version="2.1.0", source_path=path)
    document_id = str(documents.document_metadata(path)["id"])  # type: ignore[index]
    DesignRepository(raw, info.workspace_id).replace_component_master(document_id=document_id, revision_id=revision.id, model=validated_master_model(text, path))
    return document_id, revision.id
