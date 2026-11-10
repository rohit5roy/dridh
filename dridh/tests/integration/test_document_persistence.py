"""Document persistence against real temporary SQLite databases (Phase 5 document domain).

No mocks: every assertion reads the actual database file through SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.validator import validate_document
from dridh.state.documents import DocumentRepository, compute_sections, content_hash
from dridh.state.migration import CURRENT_SCHEMA_VERSION, migrate_to_current, schema_version
from dridh.state.store import STATE_SCHEMA_VERSION, StateStore, append_ledger, verify_ledger
from dridh.workspace.layout import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
USE_CASES = "docs/001-USE-CASES.md"
V1 = "# Order Intake\n\n## Actors\n\nText one.\n\n## Flows\n\nText two.\n"
V2 = "# Order Intake\n\n## Actors\n\nText one, edited.\n\n## Flows\n\nText two.\n\n## Risks\n\nNew section.\n"
STANDARDS = "2.1.0"


@pytest.fixture
def workspace(tmp_path: Path):  # type: ignore[no-untyped-def]
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project, info


def open_store(info) -> StateStore:  # type: ignore[no-untyped-def]
    return StateStore(info.state_db, info.workspace_id)


def record(store: StateStore, info, content: str, path: str = USE_CASES, standards: str = STANDARDS):  # type: ignore[no-untyped-def]
    with store.transaction() as connection:
        repository = DocumentRepository(connection, info.workspace_id)
        return repository.record_revision(relative_path=path, document_type="use_cases", content=content, standards_version=standards, source_path=path)


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


# ---------------------------------------------------------------------------
# Schema, fresh creation and migration
# ---------------------------------------------------------------------------


def test_fresh_database_creates_all_document_entities(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        tables = table_names(store.connection)  # type: ignore[arg-type]
        triggers = trigger_names(store.connection)  # type: ignore[arg-type]
        version = store.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]  # type: ignore[union-attr]
    assert {"document", "document_revision", "document_section", "document_validation", "document_validation_finding"} <= tables
    assert "document_revision_immutable_update" in triggers
    assert "document_workspace_guard" in triggers
    assert version == str(STATE_SCHEMA_VERSION) == str(CURRENT_SCHEMA_VERSION)


def test_v2_database_is_refused_until_migrated_and_upgrades_with_backup(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        append_ledger(store.connection, None, "test.event", "tester", {"n": 1})  # type: ignore[arg-type]
        ledger_before = verify_ledger(store.connection)  # type: ignore[arg-type]
    # Build a genuine v2 database: drop only the v3 document objects and record version 2.
    raw = sqlite3.connect(info.state_db, isolation_level=None)
    for name in ("document_finding_immutable_delete", "document_finding_immutable_update", "document_validation_immutable_delete", "document_validation_immutable_update",
                 "document_section_immutable_delete", "document_section_immutable_update", "document_revision_immutable_delete", "document_revision_immutable_update",
                 "document_no_delete", "document_workspace_guard"):
        raw.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("document_validation_finding", "document_validation", "document_section", "document_revision", "document"):
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    raw.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    raw.close()

    with pytest.raises(DridhError) as refused:
        open_store(info).open()
    assert refused.value.code is ErrorCode.SCHEMA_UNSUPPORTED
    assert "migrate" in refused.value.recovery

    result = migrate_to_current(info.state_db)
    assert result["migrated"] is True
    assert result["steps"][0]["backup_path"] and Path(result["steps"][0]["backup_path"]).exists()
    assert schema_version(info.state_db) == CURRENT_SCHEMA_VERSION

    with open_store(info) as store:
        ledger_after = verify_ledger(store.connection)  # type: ignore[arg-type]
        assert ledger_after["ok"] is True
        assert ledger_after["head"] == ledger_before["head"], "migration must not rewrite the audit chain"
        assert "document_revision" in table_names(store.connection)  # type: ignore[arg-type]

    again = migrate_to_current(info.state_db)
    assert again["migrated"] is False


def test_v1_to_v3_chain_upgrades_in_order(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    raw = sqlite3.connect(info.state_db, isolation_level=None)
    raw.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    raw.close()
    result = migrate_to_current(info.state_db)
    assert [step["to_version"] for step in result["steps"]] == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert schema_version(info.state_db) == CURRENT_SCHEMA_VERSION


def test_migration_refuses_unknown_version(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    raw = sqlite3.connect(info.state_db, isolation_level=None)
    raw.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    raw.close()
    result = migrate_to_current(info.state_db)
    assert result["migrated"] is False and result["steps"] == []
    with pytest.raises(DridhError) as unsupported:
        open_store(info).open()
    assert unsupported.value.code is ErrorCode.SCHEMA_UNSUPPORTED


# ---------------------------------------------------------------------------
# Revisions, sections and immutability
# ---------------------------------------------------------------------------


def test_document_creation_and_current_revision(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        revision, created = record(store, info, V1)
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        assert created is True
        assert revision.revision_number == 1
        assert revision.content_sha256 == content_hash(V1)
        assert repository.current_revision(USE_CASES) == revision
        assert repository.document_metadata(USE_CASES)["document_type"] == "use_cases"  # type: ignore[index]
        assert repository.document_metadata(USE_CASES)["lifecycle_state"] == "DRAFT"  # type: ignore[index]
        assert repository.stored_content(revision.id) == V1


def test_identical_content_is_a_no_op(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        first, _ = record(store, info, V1)
        second, created = record(store, info, V1)
        assert created is False and second.id == first.id
        assert len(DocumentRepository(store.connection, info.workspace_id).revisions(USE_CASES)) == 1  # type: ignore[arg-type]


def test_multiple_immutable_revisions_keep_history(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        r1, _ = record(store, info, V1)
        r2, created = record(store, info, V2)
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        assert created and r2.revision_number == 2
        assert [r.revision_number for r in repository.revisions(USE_CASES)] == [1, 2]
        assert repository.stored_content(r1.id) == V1, "earlier revision must not be overwritten"
        assert repository.current_revision(USE_CASES).id == r2.id  # type: ignore[union-attr]
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            store.connection.execute("UPDATE document_revision SET content = 'x' WHERE id = ?", (r1.id,))  # type: ignore[union-attr]
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            store.connection.execute("DELETE FROM document_revision WHERE id = ?", (r1.id,))  # type: ignore[union-attr]


def test_sections_are_persisted_and_retrieved_per_revision(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        r1, _ = record(store, info, V1)
        r2, _ = record(store, info, V2)
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        s1 = repository.sections(r1.id)
        s2 = repository.sections(r2.id)
        assert [s.heading for s in s1] == ["Order Intake", "Actors", "Flows"]
        assert [s.heading for s in s2] == ["Order Intake", "Actors", "Flows", "Risks"]
        assert [s.ordinal for s in s2] == [1, 2, 3, 4]
        assert s2 == compute_sections(V2)
        assert s1 == compute_sections(V1)


def test_headings_inside_code_fences_are_not_sections(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    content = "# Real\n\n```\n# not a heading\n```\n\n## Also real\n"
    with open_store(info) as store:
        revision, _ = record(store, info, content)
        headings = [s.heading for s in DocumentRepository(store.connection, info.workspace_id).sections(revision.id)]  # type: ignore[arg-type]
    assert headings == ["Real", "Also real"]


def test_section_and_revision_rows_are_immutable(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        revision, _ = record(store, info, V1)
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            store.connection.execute("UPDATE document_section SET heading = 'x' WHERE revision_id = ?", (revision.id,))  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Validation persistence and staleness
# ---------------------------------------------------------------------------


def test_validation_is_persisted_against_its_revision_with_findings(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    broken = (FIXTURES / "invalid" / "uc-1a-wrong-column-order.md").read_text(encoding="utf-8")
    with open_store(info) as store:
        with store.transaction() as connection:
            repository = DocumentRepository(connection, info.workspace_id)
            revision, _ = repository.record_revision(relative_path=USE_CASES, document_type="use_cases", content=broken, standards_version=STANDARDS, source_path=USE_CASES)
            result = validate_document("001-USE-CASES.md", broken)
            validation_id = repository.record_validation(revision_id=revision.id, validator="validate_document", validator_version="test", result=result)
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        latest = repository.latest_validation(revision.id, "validate_document")
        findings = repository.validation_findings(validation_id)
    assert latest is not None and latest.status == "FAIL" and latest.error_count >= 1
    assert latest.revision_number == 1
    codes = {f["code"] for f in findings}
    assert "TABLE_HEADER_MISMATCH" in codes
    mismatch = next(f for f in findings if f["code"] == "TABLE_HEADER_MISMATCH")
    assert isinstance(mismatch["line"], int) and mismatch["repair_hint"]


def test_stale_validation_is_not_reported_as_current_after_an_edit(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with store.transaction() as connection:
            repository = DocumentRepository(connection, info.workspace_id)
            r1, _ = repository.record_revision(relative_path=USE_CASES, document_type="use_cases", content=V1, standards_version=STANDARDS, source_path=USE_CASES)
            repository.record_validation(revision_id=r1.id, validator="validate_document", validator_version="test", result=validate_document("001-USE-CASES.md", V1))
        with store.transaction() as connection:
            repository = DocumentRepository(connection, info.workspace_id)
            r2, _ = repository.record_revision(relative_path=USE_CASES, document_type="use_cases", content=V2, standards_version=STANDARDS, source_path=USE_CASES)
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        assert repository.latest_validation(r2.id, "validate_document") is None
        assert repository.latest_validation(r1.id, "validate_document") is not None
        stale = repository.synchronization_findings(relative_path=USE_CASES, source_text=V2, expected_standards_version=STANDARDS)
    assert [f.code for f in stale] == ["VALIDATION_STALE"]
    assert stale[0].severity == "WARNING"


def test_missing_validation_is_warning_unless_required(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        record(store, info, V1)
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        lenient = repository.synchronization_findings(relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS)
        strict = repository.synchronization_findings(relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS, require_validation=True)
    assert [(f.code, f.severity) for f in lenient] == [("VALIDATION_NOT_RECORDED", "WARNING")]
    assert [(f.code, f.severity) for f in strict] == [("VALIDATION_NOT_RECORDED", "ERROR")]


def test_validation_must_reference_an_existing_revision(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with pytest.raises(DridhError) as missing:
            with store.transaction() as connection:
                DocumentRepository(connection, info.workspace_id).record_validation(
                    revision_id="does-not-exist", validator="v", validator_version="t", result=validate_document("001-USE-CASES.md", V1)
                )
        assert missing.value.code is ErrorCode.VALIDATION_FAILED


def test_foreign_key_rejects_orphan_validation_rows(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(  # type: ignore[union-attr]
                "INSERT INTO document_validation(id, revision_id, validator, validator_version, status, error_count, warning_count, validated_at) VALUES ('x', 'missing', 'v', 't', 'PASS', 0, 0, 'now')"
            )


# ---------------------------------------------------------------------------
# Synchronisation: PASS, FAIL and tamper detection
# ---------------------------------------------------------------------------


def test_sync_passes_when_file_matches_current_revision(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with store.transaction() as connection:
            repository = DocumentRepository(connection, info.workspace_id)
            r1, _ = repository.record_revision(relative_path=USE_CASES, document_type="use_cases", content=V1, standards_version=STANDARDS, source_path=USE_CASES)
            repository.record_validation(revision_id=r1.id, validator="validate_document", validator_version="test", result=validate_document("001-USE-CASES.md", V1))
        findings = DocumentRepository(store.connection, info.workspace_id).synchronization_findings(  # type: ignore[arg-type]
            relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS
        )
    assert findings == []


def test_sync_detects_edit_without_recorded_revision(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        record(store, info, V1)
        findings = DocumentRepository(store.connection, info.workspace_id).synchronization_findings(  # type: ignore[arg-type]
            relative_path=USE_CASES, source_text=V2, expected_standards_version=STANDARDS
        )
    codes = {f.code for f in findings if f.severity == "ERROR"}
    assert codes == {"SOURCE_CHANGED_SINCE_REVISION"}
    changed = next(f for f in findings if f.code == "SOURCE_CHANGED_SINCE_REVISION")
    assert changed.expected == content_hash(V1) and changed.actual == content_hash(V2)
    assert "dridh documents record" in (changed.repair_hint or "")


def test_sync_does_not_write_to_the_database(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        record(store, info, V1)
        before = store.connection.execute("SELECT COUNT(*) FROM document_revision").fetchone()[0]  # type: ignore[union-attr]
        DocumentRepository(store.connection, info.workspace_id).synchronization_findings(  # type: ignore[arg-type]
            relative_path=USE_CASES, source_text=V2, expected_standards_version=STANDARDS
        )
        after = store.connection.execute("SELECT COUNT(*) FROM document_revision").fetchone()[0]  # type: ignore[union-attr]
    assert before == after == 1


def test_missing_database_record_and_missing_file_are_findings(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        repository = DocumentRepository(store.connection, info.workspace_id)  # type: ignore[arg-type]
        not_recorded = repository.synchronization_findings(relative_path="docs/none.md", source_text=V1, expected_standards_version=STANDARDS)
        record(store, info, V1)
        no_file = repository.synchronization_findings(relative_path=USE_CASES, source_text=None, expected_standards_version=STANDARDS)
    assert [f.code for f in not_recorded] == ["DOCUMENT_NOT_PERSISTED"]
    assert [f.code for f in no_file] == ["SOURCE_FILE_MISSING"]


def test_standards_version_mismatch_is_detected(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        record(store, info, V1, standards="1.0.0")
        findings = DocumentRepository(store.connection, info.workspace_id).synchronization_findings(  # type: ignore[arg-type]
            relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS
        )
    assert "STANDARDS_VERSION_MISMATCH" in {f.code for f in findings}


def test_tampered_revision_text_is_detected_as_corruption(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        revision, _ = record(store, info, V1)
        # Tampering bypasses the application: drop the guard trigger, then edit the stored text.
        store.connection.execute("DROP TRIGGER document_revision_immutable_update")  # type: ignore[union-attr]
        store.connection.execute("UPDATE document_revision SET content = ? WHERE id = ?", (V1.replace("one", "ONE"), revision.id))  # type: ignore[union-attr]
        findings = DocumentRepository(store.connection, info.workspace_id).synchronization_findings(  # type: ignore[arg-type]
            relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS
        )
    assert "REVISION_HASH_MISMATCH" in {f.code for f in findings}


def test_tampered_sections_are_detected(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        revision, _ = record(store, info, V1)
        store.connection.execute("DROP TRIGGER document_section_immutable_update")  # type: ignore[union-attr]
        store.connection.execute("UPDATE document_section SET heading = 'Forged' WHERE revision_id = ? AND ordinal = 2", (revision.id,))  # type: ignore[union-attr]
        findings = DocumentRepository(store.connection, info.workspace_id).synchronization_findings(  # type: ignore[arg-type]
            relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS
        )
    assert "SECTIONS_MISMATCH" in {f.code for f in findings}


# ---------------------------------------------------------------------------
# Transactions, workspace isolation, reopen and recovery
# ---------------------------------------------------------------------------


def test_failed_persistence_rolls_back_the_whole_revision(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with pytest.raises(DridhError):
            with store.transaction() as connection:
                repository = DocumentRepository(connection, info.workspace_id)
                revision, _ = repository.record_revision(relative_path=USE_CASES, document_type="use_cases", content=V1, standards_version=STANDARDS, source_path=USE_CASES)
                # A validation against a revision that does not exist fails the whole unit.
                repository.record_validation(revision_id="missing", validator="v", validator_version="t", result=validate_document("001-USE-CASES.md", V1))
        assert store.connection.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0  # type: ignore[union-attr]
        assert store.connection.execute("SELECT COUNT(*) FROM document_section").fetchone()[0] == 0  # type: ignore[union-attr]


def test_exception_inside_transaction_rolls_back(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with pytest.raises(RuntimeError):
            with store.transaction() as connection:
                DocumentRepository(connection, info.workspace_id).record_revision(relative_path=USE_CASES, document_type="use_cases", content=V1, standards_version=STANDARDS, source_path=USE_CASES)
                raise RuntimeError("interrupted")
        assert store.connection.execute("SELECT COUNT(*) FROM document_revision").fetchone()[0] == 0  # type: ignore[union-attr]


def test_writes_outside_a_transaction_are_refused(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with pytest.raises(DridhError) as outside:
            DocumentRepository(store.connection, info.workspace_id).record_revision(  # type: ignore[arg-type]
                relative_path=USE_CASES, document_type="use_cases", content=V1, standards_version=STANDARDS, source_path=USE_CASES
            )
        assert outside.value.code is ErrorCode.INTERNAL_ERROR


def test_workspace_guard_rejects_documents_for_another_workspace(workspace, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    other_project = tmp_path / "other"
    other_project.mkdir()
    other, _ = init_workspace(other_project)
    with StateStore(other.state_db, other.workspace_id):
        pass
    with open_store(info) as store:
        with pytest.raises(sqlite3.IntegrityError, match="another workspace"):
            store.connection.execute(  # type: ignore[union-attr]
                "INSERT INTO document(id, workspace_id, relative_path, document_type, standards_version, lifecycle_state, created_at, updated_at) VALUES ('d', ?, 'x.md', 'use_cases', '2.1.0', 'DRAFT', 'n', 'n')",
                (other.workspace_id,),
            )


def test_same_relative_path_is_independent_across_workspaces(tmp_path: Path) -> None:
    a_project, b_project = tmp_path / "a", tmp_path / "b"
    a_project.mkdir()
    b_project.mkdir()
    a, _ = init_workspace(a_project)
    b, _ = init_workspace(b_project)
    with open_store(a) as store_a:
        record(store_a, a, V1)
    with open_store(b) as store_b:
        assert DocumentRepository(store_b.connection, b.workspace_id).current_revision(USE_CASES) is None  # type: ignore[arg-type]
        record(store_b, b, V2)
    with open_store(a) as store_a:
        assert DocumentRepository(store_a.connection, a.workspace_id).current_revision(USE_CASES).content_sha256 == content_hash(V1)  # type: ignore[arg-type, union-attr]


def test_persisted_state_survives_reopen_and_sync_still_passes(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        with store.transaction() as connection:
            repository = DocumentRepository(connection, info.workspace_id)
            r1, _ = repository.record_revision(relative_path=USE_CASES, document_type="use_cases", content=V1, standards_version=STANDARDS, source_path=USE_CASES)
            repository.record_validation(revision_id=r1.id, validator="validate_document", validator_version="test", result=validate_document("001-USE-CASES.md", V1))
    with open_store(info) as reopened:
        repository = DocumentRepository(reopened.connection, info.workspace_id)  # type: ignore[arg-type]
        assert repository.current_revision(USE_CASES).id == r1.id  # type: ignore[union-attr]
        assert repository.synchronization_findings(relative_path=USE_CASES, source_text=V1, expected_standards_version=STANDARDS) == []


def test_document_path_must_be_relative() -> None:
    from dridh.state.documents import require_relative_key

    assert require_relative_key("docs/a.md") == "docs/a.md"
    for bad in ("/etc/passwd", "../x.md", "docs/../../x.md", ""):
        with pytest.raises(DridhError):
            require_relative_key(bad)


def test_lifecycle_state_is_constrained(workspace) -> None:  # type: ignore[no-untyped-def]
    _, info = workspace
    with open_store(info) as store:
        record(store, info, V1)
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE document SET lifecycle_state = 'BOGUS'")  # type: ignore[union-attr]
