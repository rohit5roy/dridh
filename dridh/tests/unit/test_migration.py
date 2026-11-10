"""Workspace migration v1 -> v2."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dridh.state.migration import CURRENT_SCHEMA_VERSION, PHASE2_SCHEMA_VERSION, PHASE3_SCHEMA_VERSION, migrate_v1_to_v2, schema_version
from dridh.workspace.layout import init_workspace


def test_fresh_workspace_is_v3(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    from dridh.state.store import StateStore
    with StateStore(info.state_db, info.workspace_id):
        pass
    assert schema_version(info.state_db) == CURRENT_SCHEMA_VERSION


def test_migrate_v1_to_v2(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    from dridh.state.store import StateStore
    with StateStore(info.state_db, info.workspace_id):
        pass
    # Downgrade to v1 manually.
    conn = sqlite3.connect(info.state_db)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert schema_version(info.state_db) == PHASE2_SCHEMA_VERSION

    result = migrate_v1_to_v2(info.state_db)
    assert result["migrated"] is True
    assert result["from_version"] == 1
    assert result["to_version"] == 2
    assert result["backup_path"] is not None
    assert Path(result["backup_path"]).exists()
    assert schema_version(info.state_db) == PHASE3_SCHEMA_VERSION

    # Verify parent_task_id column exists.
    conn = sqlite3.connect(info.state_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    assert "parent_task_id" in cols
    conn.close()


def test_migration_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    from dridh.state.store import StateStore
    with StateStore(info.state_db, info.workspace_id):
        pass
    result = migrate_v1_to_v2(info.state_db)
    assert result["migrated"] is False
    assert result["from_version"] == CURRENT_SCHEMA_VERSION


def test_migration_refuses_unsupported_version(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    from dridh.state.store import StateStore
    with StateStore(info.state_db, info.workspace_id):
        pass
    conn = sqlite3.connect(info.state_db)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    from dridh.common.errors import DridhError

    with pytest.raises(DridhError):
        migrate_v1_to_v2(info.state_db)
