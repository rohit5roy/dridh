"""Project-local workspace layout, state database and hash-chained ledger."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import STATE_SCHEMA_VERSION, StateStore, append_ledger, verify_ledger
from dridh.workspace.layout import WORKSPACE_DIR, init_workspace, open_workspace


def test_init_creates_versioned_layout_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project-a"
    project.mkdir()
    info, created = init_workspace(project)
    assert created is True
    assert info.root == project / WORKSPACE_DIR
    descriptor = json.loads((info.root / "workspace.json").read_text())
    assert descriptor["schema"] == "dridh.workspace" and descriptor["schema_version"] == 1
    assert descriptor["project_root"] == str(project.resolve())
    for name in ("state", "artifacts", "reports", "evidence", "logs"):
        assert (info.root / name).is_dir()
    assert "state/" in (info.root / ".gitignore").read_text()
    again, created_again = init_workspace(project)
    assert created_again is False
    assert again.workspace_id == info.workspace_id


def test_open_requires_initialisation(tmp_path: Path) -> None:
    project = tmp_path / "bare"
    project.mkdir()
    with pytest.raises(DridhError) as missing:
        open_workspace(project)
    assert missing.value.code is ErrorCode.WORKSPACE_NOT_FOUND
    assert not (project / WORKSPACE_DIR).exists()


def test_two_projects_have_distinct_workspaces_and_databases(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    a, _ = init_workspace(first)
    b, _ = init_workspace(second)
    assert a.workspace_id != b.workspace_id
    assert a.state_db != b.state_db
    assert str(second.resolve()) not in str(a.root)


def test_copied_workspace_is_rejected_for_another_project(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    info, _ = init_workspace(source)
    shutil.copytree(info.root, target / WORKSPACE_DIR)
    with pytest.raises(DridhError) as copied:
        open_workspace(target)
    assert copied.value.code is ErrorCode.STATE_CORRUPT


def test_unsupported_descriptor_schema_and_unreadable_descriptor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    descriptor = info.root / "workspace.json"
    payload = json.loads(descriptor.read_text())
    payload["schema_version"] = 99
    descriptor.write_text(json.dumps(payload))
    with pytest.raises(DridhError) as unsupported:
        open_workspace(project)
    assert unsupported.value.code is ErrorCode.SCHEMA_UNSUPPORTED
    descriptor.write_text("{not json")
    with pytest.raises(DridhError) as unreadable:
        open_workspace(project)
    assert unreadable.value.code is ErrorCode.STATE_CORRUPT


def test_store_creates_schema_and_reopens(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id) as store:
        assert store.read().execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(STATE_SCHEMA_VERSION)
    with StateStore(info.state_db, info.workspace_id) as reopened:
        assert reopened.read().execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0


def test_corrupt_or_foreign_database_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    other_project = tmp_path / "other"
    other_project.mkdir()
    other, _ = init_workspace(other_project)
    shutil.copy(info.state_db, other.state_db)
    with pytest.raises(DridhError) as foreign:
        StateStore(other.state_db, other.workspace_id).open()
    assert foreign.value.code is ErrorCode.STATE_CORRUPT

    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"this is not a database" * 200)
    with pytest.raises(DridhError) as corrupt:
        StateStore(garbage, info.workspace_id).open()
    assert corrupt.value.code is ErrorCode.STATE_CORRUPT


def test_store_refuses_unsupported_schema_version(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    connection = sqlite3.connect(info.state_db)
    connection.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    connection.commit()
    connection.close()
    with pytest.raises(DridhError) as unsupported:
        StateStore(info.state_db, info.workspace_id).open()
    assert unsupported.value.code is ErrorCode.SCHEMA_UNSUPPORTED


def test_ledger_is_hash_chained_and_detects_tampering(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id) as store:
        with store.transaction() as conn:
            for index in range(3):
                append_ledger(conn, "wf-1", "EVENT", "tester", {"index": index})
        assert verify_ledger(store.read())["ok"] is True
        with store.transaction() as conn:
            conn.execute("UPDATE ledger SET payload='{\"index\":99}' WHERE seq=2")
        tampered = verify_ledger(store.read())
        assert tampered["ok"] is False and tampered["first_bad_seq"] == 2
    with StateStore(info.state_db, info.workspace_id) as fresh:
        with fresh.transaction() as conn:
            conn.execute("UPDATE ledger SET payload='{\"index\":1}' WHERE seq=2")
            conn.execute("DELETE FROM ledger WHERE seq=2")
        assert verify_ledger(fresh.read())["ok"] is False


def test_ledger_payloads_are_redacted_before_storage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    secret = "synthetic-secret-value-4242"
    with StateStore(info.state_db, info.workspace_id) as store:
        with store.transaction() as conn:
            append_ledger(conn, None, "CONFIG", "tester", {"api_key": secret, "note": "kept"})
        stored = store.read().execute("SELECT payload FROM ledger").fetchone()[0]
    assert secret not in stored
    assert "kept" in stored


def test_failed_transaction_leaves_no_partial_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id) as store:
        with pytest.raises(RuntimeError):
            with store.transaction() as conn:
                append_ledger(conn, None, "PARTIAL", "tester", {"step": 1})
                raise RuntimeError("boom")
        assert store.read().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
