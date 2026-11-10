"""Use-case domain persistence against real SQLite (Phase 5 increment 1).

No mocks. Constraints are asserted through SQLite itself, so the tests prove the database
enforces the canonical rules rather than the Python code.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.documents import DocumentRepository
from dridh.state.migration import CURRENT_SCHEMA_VERSION, migrate_to_current, schema_version
from dridh.state.store import STATE_SCHEMA_VERSION, StateStore, append_ledger, verify_ledger
from dridh.state.usecases import UseCaseRepository, parse_use_case_model, validated_model
from dridh.workspace.layout import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
VALID = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
EDITED = VALID.replace("| Order request accepted |", "| Order request is accepted |")
INVALID = (FIXTURES / "invalid" / "uc-1a-wrong-column-order.md").read_text(encoding="utf-8")
PATH = "docs/001-USE-CASES.md"


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


def import_document(store: StateStore, info, content: str, path: str = PATH):  # type: ignore[no-untyped-def]
    """Record the revision and replace the projection in one transaction, as the CLI does."""

    with store.transaction() as connection:
        documents = DocumentRepository(connection, info.workspace_id)
        revision, _ = documents.record_revision(relative_path=path, document_type="use_cases", content=content, standards_version="2.1.0", source_path=path)
        document_id = documents.document_metadata(path)["id"]  # type: ignore[index]
        model = validated_model(content, path)
        counts = UseCaseRepository(connection, info.workspace_id).replace_projection(document_id=document_id, revision_id=revision.id, model=model)
    return revision, document_id, counts


def count(store: StateStore, table: str) -> int:
    return store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[union-attr]


def expect_constraint(store: StateStore, sql: str, params: tuple = ()) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(sql, params)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


def test_fresh_database_has_use_case_tables_and_triggers(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}  # type: ignore[union-attr]
        triggers = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}  # type: ignore[union-attr]
        version = store.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]  # type: ignore[union-attr]
    assert {"actor", "use_case", "use_case_flow"} <= tables
    assert {"use_case_flow_origin_check", "use_case_flow_parent_prefix", "use_case_immutable_update"} <= triggers
    assert version == str(STATE_SCHEMA_VERSION) == str(CURRENT_SCHEMA_VERSION)


def test_v3_workspace_upgrades_and_keeps_document_history_and_ledger(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        with store.transaction() as connection:
            DocumentRepository(connection, workspace.workspace_id).record_revision(relative_path=PATH, document_type="use_cases", content=VALID, standards_version="2.1.0", source_path=PATH)
        append_ledger(store.connection, None, "test.event", "tester", {"n": 1})  # type: ignore[arg-type]
        head_before = verify_ledger(store.connection)["head"]  # type: ignore[arg-type]
    raw = sqlite3.connect(workspace.state_db, isolation_level=None)
    for name in [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'actor%' OR name LIKE 'use_case%')").fetchall()]:
        raw.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("use_case_flow", "use_case", "actor"):
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    raw.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    raw.close()

    with pytest.raises(DridhError) as refused:
        open_store(workspace).open()
    assert refused.value.code is ErrorCode.SCHEMA_UNSUPPORTED

    result = migrate_to_current(workspace.state_db)
    assert result["migrated"] is True and [s["to_version"] for s in result["steps"]] == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert schema_version(workspace.state_db) == CURRENT_SCHEMA_VERSION
    with open_store(workspace) as store:
        assert DocumentRepository(store.connection, workspace.workspace_id).current_revision(PATH) is not None  # type: ignore[arg-type]
        verdict = verify_ledger(store.connection)  # type: ignore[arg-type]
        assert verdict["ok"] is True and verdict["head"] == head_before


# ---------------------------------------------------------------------------
# Import, projection and reconciliation
# ---------------------------------------------------------------------------


def test_valid_document_imports_actors_use_cases_and_flows(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        _, _, counts = import_document(store, workspace, VALID)
        assert counts == {"actors": 2, "use_cases": 2, "flows": 8}
        assert count(store, "actor") == 2 and count(store, "use_case") == 2 and count(store, "use_case_flow") == 8
        flows = store.connection.execute(  # type: ignore[union-attr]
            "SELECT flow_id, flow_type, origin_step_flow_id FROM use_case_flow WHERE parent_use_case_id='UC-01' ORDER BY step_order"
        ).fetchall()
    assert [r["flow_id"] for r in flows][:2] == ["SF-01.1", "SF-01.2"]
    assert flows[0]["origin_step_flow_id"] is None
    assert {r["flow_id"]: r["origin_step_flow_id"] for r in flows}["EF-01.1"] == "SF-01.2"


def test_reimport_of_same_revision_is_idempotent(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        _, document_id, _ = import_document(store, workspace, VALID)
        first = parse_use_case_model(VALID)
        import_document(store, workspace, VALID)
        assert count(store, "use_case_flow") == 8 and count(store, "use_case") == 2 and count(store, "actor") == 2
        stored = UseCaseRepository(store.connection, workspace.workspace_id).stored_model(document_id)  # type: ignore[arg-type]
    assert stored == first


def test_edit_produces_new_projection_and_old_rows_are_replaced(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        old_revision, document_id, _ = import_document(store, workspace, VALID)
        repository = UseCaseRepository(store.connection, workspace.workspace_id)  # type: ignore[arg-type]
        stale = repository.projection_findings(relative_path=PATH, document_id=document_id, current_revision_id=old_revision.id, current_content=EDITED)
        assert [f.code for f in stale] == ["PROJECTION_MISMATCH"]
        new_revision, _, _ = import_document(store, workspace, EDITED)
        assert repository.projection_revision_ids(document_id) == {new_revision.id}
        assert repository.projection_findings(relative_path=PATH, document_id=document_id, current_revision_id=new_revision.id, current_content=EDITED) == []
        result_text = store.connection.execute("SELECT result_text FROM use_case_flow WHERE flow_id='SF-01.1'").fetchone()[0]  # type: ignore[union-attr]
    assert result_text == "Order request is accepted"
    assert "Order request accepted" not in str(parse_use_case_model(EDITED))


def test_stale_projection_is_reported_when_revision_moves_on(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        old_revision, document_id, _ = import_document(store, workspace, VALID)
        with store.transaction() as connection:
            DocumentRepository(connection, workspace.workspace_id).record_revision(relative_path=PATH, document_type="use_cases", content=EDITED, standards_version="2.1.0", source_path=PATH)
        findings = UseCaseRepository(store.connection, workspace.workspace_id).projection_findings(  # type: ignore[arg-type]
            relative_path=PATH, document_id=document_id, current_revision_id="new-revision", current_content=EDITED
        )
    assert "PROJECTION_STALE" in {f.code for f in findings}
    assert old_revision.id != "new-revision"


def test_missing_projection_is_reported(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        with store.transaction() as connection:
            revision, _ = DocumentRepository(connection, workspace.workspace_id).record_revision(relative_path=PATH, document_type="use_cases", content=VALID, standards_version="2.1.0", source_path=PATH)
            document_id = DocumentRepository(connection, workspace.workspace_id).document_metadata(PATH)["id"]  # type: ignore[index]
        findings = UseCaseRepository(store.connection, workspace.workspace_id).projection_findings(  # type: ignore[arg-type]
            relative_path=PATH, document_id=document_id, current_revision_id=revision.id, current_content=VALID
        )
    assert [f.code for f in findings] == ["PROJECTION_MISSING"]


def test_invalid_document_is_refused_and_leaves_projection_untouched(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        import_document(store, workspace, VALID)
        before = count(store, "use_case_flow")
        with pytest.raises(DridhError) as refused:
            import_document(store, workspace, INVALID)
        assert refused.value.code is ErrorCode.VALIDATION_FAILED
        assert count(store, "use_case_flow") == before == 8


def test_failed_projection_rolls_back_the_previous_projection(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        _, document_id, _ = import_document(store, workspace, VALID)
        revision_id = store.connection.execute("SELECT source_revision_id FROM use_case LIMIT 1").fetchone()[0]  # type: ignore[union-attr]
        model = parse_use_case_model(EDITED)
        duplicated = type(model)(actors=model.actors, use_cases=model.use_cases, flows=model.flows + model.flows[:1])
        with pytest.raises(DridhError):
            with store.transaction() as connection:
                UseCaseRepository(connection, workspace.workspace_id).replace_projection(document_id=document_id, revision_id=revision_id, model=duplicated)
        assert count(store, "use_case_flow") == 8
        assert store.connection.execute("SELECT result_text FROM use_case_flow WHERE flow_id='SF-01.1'").fetchone()[0] == "Order request accepted"  # type: ignore[union-attr]


def test_tampered_projection_is_detected_by_reconciliation(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        revision, document_id, _ = import_document(store, workspace, VALID)
        store.connection.execute("DROP TRIGGER use_case_flow_immutable_update")  # type: ignore[union-attr]
        store.connection.execute("UPDATE use_case_flow SET action_text='Something else' WHERE flow_id='SF-01.1'")  # type: ignore[union-attr]
        findings = UseCaseRepository(store.connection, workspace.workspace_id).projection_findings(  # type: ignore[arg-type]
            relative_path=PATH, document_id=document_id, current_revision_id=revision.id, current_content=VALID
        )
    assert [f.code for f in findings] == ["PROJECTION_MISMATCH"]


# ---------------------------------------------------------------------------
# Canonical constraints, enforced by SQLite
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(workspace):  # type: ignore[no-untyped-def]
    store = open_store(workspace)
    store.open()
    revision, document_id, _ = import_document(store, workspace, VALID)
    yield store, workspace, document_id, revision.id
    store.close()


def _insert_flow(store: StateStore, scope: tuple[str, str, str], **overrides: object) -> None:
    workspace_id, document_id, revision_id = scope
    row: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "document_id": document_id,
        "source_revision_id": revision_id,
        "flow_id": "SF-01.9",
        "parent_use_case_id": "UC-01",
        "flow_type": "SUCCESS",
        "step_order": 9,
        "origin_step_flow_id": None,
        "subject": "s",
        "actor_id": "ACT-01",
        "action_text": "a",
        "result_text": "r",
        "priority": "HIGH",
    }
    row.update(overrides)
    columns = list(row)
    store.connection.execute(  # type: ignore[union-attr]
        f"INSERT INTO use_case_flow({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        tuple(row[c] for c in columns),
    )


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"flow_type": "ALTERNATIVE"}, "SF-prefixed flow with a non-success type"),
        ({"flow_id": "AF-01.9", "flow_type": "ALTERNATIVE"}, "branch without an origin step"),
        ({"flow_id": "EF-01.9", "flow_type": "EXCEPTION", "origin_step_flow_id": "SF-01.1", "parent_use_case_id": "UC-02"}, "flow number does not match its parent use case"),
        ({"flow_id": "BR-01.9", "flow_type": "BUSINESS_RULE", "origin_step_flow_id": "SF-02.1"}, "origin step belongs to another use case"),
        ({"flow_id": "BE-01.9", "flow_type": "BOUNDARY", "origin_step_flow_id": "EF-01.1"}, "origin step is not a success step"),
        ({"flow_id": "SF-01.2"}, "duplicate canonical flow ID"),
        ({"actor_id": "ACT-99"}, "actor is not registered in this workspace"),
        ({"parent_use_case_id": "UC-09"}, "parent use case does not exist"),
        ({"flow_id": "xx-01.9"}, "malformed flow ID"),
    ],
)
def test_flow_constraints_are_enforced_by_the_database(seeded, overrides: dict[str, object], why: str) -> None:  # type: ignore[no-untyped-def]
    store, info, document_id, revision_id = seeded
    with pytest.raises(sqlite3.IntegrityError):
        _insert_flow(store, (info.workspace_id, document_id, revision_id), **overrides)


def test_actor_and_use_case_identifiers_are_canonical(seeded) -> None:  # type: ignore[no-untyped-def]
    store, info, document_id, revision_id = seeded
    base = {"workspace_id": info.workspace_id, "document_id": document_id, "source_revision_id": revision_id}
    expect_constraint(store, "INSERT INTO actor(id, workspace_id, document_id, source_revision_id, actor_id, actor_name, actor_type, actor_status, description, position) VALUES ('a1', ?, ?, ?, 'ACT-1', 'n', 'USER', 'NEW', 'd', 9)", (base["workspace_id"], document_id, revision_id))
    expect_constraint(store, "INSERT INTO actor(id, workspace_id, document_id, source_revision_id, actor_id, actor_name, actor_type, actor_status, description, position) VALUES ('a2', ?, ?, ?, 'ACT-09', 'n', 'ROBOT', 'NEW', 'd', 9)", (base["workspace_id"], document_id, revision_id))
    expect_constraint(store, "INSERT INTO use_case(id, workspace_id, document_id, source_revision_id, use_case_id, title, primary_actor_id, trigger_text, category, position) VALUES ('u1', ?, ?, ?, 'UC-1', 't', 'ACT-01', 'tr', 'c', 9)", (base["workspace_id"], document_id, revision_id))
    expect_constraint(store, "INSERT INTO use_case(id, workspace_id, document_id, source_revision_id, use_case_id, title, primary_actor_id, trigger_text, category, position) VALUES ('u2', ?, ?, ?, 'UC-01', 't', 'ACT-99', 'tr', 'c', 9)", (base["workspace_id"], document_id, revision_id))


def test_projection_rows_cannot_be_updated_in_place(seeded) -> None:  # type: ignore[no-untyped-def]
    store, _, _, _ = seeded
    with pytest.raises(sqlite3.DatabaseError, match="replaced by import"):
        store.connection.execute("UPDATE use_case SET title='changed' WHERE use_case_id='UC-01'")  # type: ignore[union-attr]
    with pytest.raises(sqlite3.DatabaseError, match="replaced by import"):
        store.connection.execute("UPDATE actor SET actor_name='changed' WHERE actor_id='ACT-01'")  # type: ignore[union-attr]


def test_use_case_rows_belong_to_their_workspace(seeded, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, info, document_id, revision_id = seeded
    other_project = tmp_path / "other"
    other_project.mkdir()
    other, _ = init_workspace(other_project)
    with StateStore(other.state_db, other.workspace_id):
        pass
    expect_constraint(
        store,
        "INSERT INTO actor(id, workspace_id, document_id, source_revision_id, actor_id, actor_name, actor_type, actor_status, description, position) VALUES ('cross', ?, ?, ?, 'ACT-07', 'n', 'USER', 'NEW', 'd', 9)",
        (other.workspace_id, document_id, revision_id),
    )
    with open_store(other) as other_store:
        assert UseCaseRepository(other_store.connection, other.workspace_id).stored_model(document_id).use_cases == ()  # type: ignore[arg-type]


def test_reopen_preserves_projection(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        import_document(store, workspace, VALID)
    with open_store(workspace) as reopened:
        assert count(reopened, "use_case_flow") == 8
        assert verify_ledger(reopened.connection)["ok"] is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Review findings (PR #6): exact flow IDs, document/revision pairing, empty imports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flow_id", ["SF-01.abc", "SF-01.", "SF-01.1x", "SF-01.1.2", "SF-01.1 ", "SF-01.-1", "SF-01 1", "SF-01.1\n"])
def test_flow_suffix_must_be_digits(seeded, flow_id: str) -> None:  # type: ignore[no-untyped-def]
    store, info, document_id, revision_id = seeded
    with pytest.raises(sqlite3.IntegrityError):
        _insert_flow(store, (info.workspace_id, document_id, revision_id), flow_id=flow_id)


def test_canonical_multi_digit_flow_suffix_is_accepted(seeded) -> None:  # type: ignore[no-untyped-def]
    store, info, document_id, revision_id = seeded
    _insert_flow(store, (info.workspace_id, document_id, revision_id), flow_id="SF-01.12", step_order=12)
    assert store.connection.execute("SELECT COUNT(*) FROM use_case_flow WHERE flow_id='SF-01.12'").fetchone()[0] == 1  # type: ignore[union-attr]


def test_flow_cannot_reference_a_revision_of_another_document(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        _, doc_a, _ = import_document(store, workspace, VALID, path="docs/a.md")
        with store.transaction() as connection:
            revision_b, _ = DocumentRepository(connection, workspace.workspace_id).record_revision(
                relative_path="docs/b.md", document_type="use_cases", content=VALID, standards_version="2.1.0", source_path="docs/b.md"
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_flow(store, (workspace.workspace_id, doc_a, revision_b.id), flow_id="SF-01.9")
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(  # type: ignore[union-attr]
                "INSERT INTO use_case_import(workspace_id, document_id, source_revision_id, actor_count, use_case_count, flow_count, imported_at) VALUES (?, ?, ?, 0, 0, 0, 'n')",
                (workspace.workspace_id, doc_a, revision_b.id),
            )


def test_actor_cannot_reference_a_document_in_another_workspace(seeded, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, info, document_id, revision_id = seeded
    store.connection.execute("DROP TRIGGER actor_workspace_guard")  # type: ignore[union-attr]
    other_project = tmp_path / "other"
    other_project.mkdir()
    other, _ = init_workspace(other_project)
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(  # type: ignore[union-attr]
            "INSERT INTO actor(id, workspace_id, document_id, source_revision_id, actor_id, actor_name, actor_type, actor_status, description, position) VALUES ('w', ?, ?, ?, 'ACT-07', 'n', 'USER', 'NEW', 'd', 9)",
            (other.workspace_id, document_id, revision_id),
        )


EMPTY = (
    "# Empty Feature\n\n> **Standards Version:** 2.1.0  \n\n## Actors Registry\n\n"
    "| Actor ID | Actor Name | Type | Status | Description |\n|---|---|---|---|---|\n| — | — | — | — | — |\n\n"
    "## Use Case Summary Matrix\n\n| ID | Use Case | Primary Actor | Trigger | Category |\n|---|---|---|---|---|\n| — | — | — | — | — |\n"
)


def test_empty_valid_document_is_distinct_from_a_missing_projection(workspace) -> None:  # type: ignore[no-untyped-def]
    from dridh.state.usecases import validated_model as _validated

    assert _validated(EMPTY, "empty.md").use_cases == ()
    with open_store(workspace) as store:
        revision, document_id, counts = import_document(store, workspace, EMPTY, path="docs/empty.md")
        assert counts == {"actors": 0, "use_cases": 0, "flows": 0}
        repository = UseCaseRepository(store.connection, workspace.workspace_id)  # type: ignore[arg-type]
        assert repository.import_marker(document_id)["use_case_count"] == 0  # type: ignore[index]
        assert repository.projection_findings(relative_path="docs/empty.md", document_id=document_id, current_revision_id=revision.id, current_content=EMPTY) == []
        with store.transaction() as connection:
            DocumentRepository(connection, workspace.workspace_id).record_revision(relative_path="docs/never.md", document_type="use_cases", content=EMPTY, standards_version="2.1.0", source_path="docs/never.md")
            never_id = DocumentRepository(connection, workspace.workspace_id).document_metadata("docs/never.md")["id"]  # type: ignore[index]
        never = repository.projection_findings(relative_path="docs/never.md", document_id=str(never_id), current_revision_id="x", current_content=EMPTY)
    assert [f.code for f in never] == ["PROJECTION_MISSING"]


def test_repeated_import_of_an_unchanged_model_is_detected_as_current(workspace) -> None:  # type: ignore[no-untyped-def]
    with open_store(workspace) as store:
        revision, document_id, _ = import_document(store, workspace, VALID)
        repository = UseCaseRepository(store.connection, workspace.workspace_id)  # type: ignore[arg-type]
        assert repository.projection_is_current(document_id=document_id, revision_id=revision.id, model=parse_use_case_model(VALID))
        assert not repository.projection_is_current(document_id=document_id, revision_id="other", model=parse_use_case_model(VALID))
        assert not repository.projection_is_current(document_id=document_id, revision_id=revision.id, model=parse_use_case_model(EDITED))
