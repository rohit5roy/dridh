from __future__ import annotations
# ruff: noqa: E701,E702,I001 — compact SQL persistence declarations and focused integration fixtures
import json
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.config.dridh_yaml import load_config
from dridh.state.backup import backup_workspace, integrity_check, restore_workspace
from dridh.state.execution import ExecutionRepository, JevRepository
from dridh.state.indexing import CodeIndexWriter
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace, open_workspace


def workspace(tmp_path: Path):
    project=tmp_path/"project"; project.mkdir()
    info,_=init_workspace(project)
    # init_workspace creates the workspace layout/config; StateStore creates SQLite.
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project,info


def test_execution_and_jev_records_are_workspace_scoped(tmp_path: Path):
    project,info=workspace(tmp_path)
    with StateStore(info.state_db,info.workspace_id) as store:
        with store.transaction():
            r=ExecutionRepository(store.connection,info.workspace_id)
            r.record_branch(branch_id="b1",name="feature/story-1",story_id="ST-1")
            r.record_worktree(worktree_id="w1",path="/tmp/w1",story_id="ST-1",branch_id="b1")
            r.record_pull_request(pr_id="pr1",title="Story",head_ref="feature/story-1",head_sha="abc",base_ref="main",story_id="ST-1",number=12)
            r.record_check(check_id="check1",pr_id="pr1",name="pytest",head_sha="abc",status="completed",conclusion="success")
            r.record_gate(gate_id="g1",story_id="ST-1",gate="GHA_PASSED",status="PASS",revision_sha="abc")
            r.record_integration(integration_id="i1",branch_name="integration",commit_sha="def",predecessor_story_ids=["ST-1"],index_revision="def")
            r.record_docker_environment(environment_id="d1",compose_project="dridh-story1",network_name="dridh-story1",status="PASSED")
            JevRepository(store.connection,info.workspace_id).record_evaluation(evaluation_id="j1",subject_type="STORY",subject_id="ST-1",provider="jev",model="test",input_sha="0"*64,status="SUCCEEDED",decision="PASS",score=.9)
        assert r.story_delivery("ST-1")["pull_requests"][0]["head_sha"]=="abc"
        assert r.list_worktrees()[0]["status"]=="ACTIVE"


def test_code_index_writers_and_fts(tmp_path: Path):
    _,info=workspace(tmp_path)
    with StateStore(info.state_db,info.workspace_id) as store:
        with store.transaction():
            w=CodeIndexWriter(store.connection,info.workspace_id)
            w.start_run(run_id="run1",repository_path="/repo",source_sha="sha1")
            w.add_file(file_id="f1",run_id="run1",path="src/A.py",language="python",content=b"class A: pass",source_sha="sha1")
            w.add_entity(entity_id="e1",file_id="f1",entity_type="class",name="A",qualified_name="pkg.A",start_line=1,end_line=1,source_sha="sha1")
            w.add_summary(summary_id="s1",entity_id="e1",summary="handles payment workflow",model="local",source_sha="sha1")
            w.finish_run("run1",status="SUCCEEDED",files_seen=1,files_indexed=1)
        assert w.search("payment")[0]["qualified_name"]=="pkg.A"


def test_backup_restore_integrity(tmp_path: Path):
    project,info=workspace(tmp_path)
    archive=tmp_path/"backup.zip"; restored=tmp_path/"restored"
    result=backup_workspace(workspace_root=project,db_path=info.state_db,destination=archive,dridh_version="0.1.0")
    assert result["integrity"]=="ok"
    restored_result=restore_workspace(backup_path=archive,destination=restored)
    assert restored_result["integrity"]=="ok"
    assert integrity_check(restored/".opencode"/"dridh"/"state"/"dridh.sqlite")["ok"]
    restored_info = open_workspace(restored)
    assert restored_info.workspace_id == info.workspace_id
    restored_config = load_config(restored_info.root / "dridh.yaml")
    assert restored_config.workspace.root == str(restored.resolve())


def test_explicit_v8_to_v10_migration_is_backed_up(tmp_path: Path):
    from dridh.state.migration import migrate_to_current, schema_version
    project,info=workspace(tmp_path)
    with StateStore(info.state_db,info.workspace_id): pass
    conn=sqlite3.connect(info.state_db)
    tables=["code_search_fts","jev_provider_health","jev_evaluation_criterion","jev_evaluation","code_embedding","code_summary","code_repository_mapping","code_api_endpoint","code_relation","code_entity_parameter","code_entity","code_file","code_index_run","docker_test_environment","integration_commit","story_gate","pull_request_check","pull_request","worktree","git_branch"]
    for name in tables: conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.execute("UPDATE meta SET value='8' WHERE key='schema_version'"); conn.commit(); conn.close()
    result=migrate_to_current(info.state_db)
    assert result["to_version"]==15 and result["migrated"]
    assert schema_version(info.state_db)==15
    assert all(step["backup_path"] and Path(step["backup_path"]).exists() for step in result["steps"])
    assert integrity_check(info.state_db)["ok"]


def test_state_repositories_reject_a_mismatched_workspace_id(tmp_path: Path):
    _, info = workspace(tmp_path)
    with StateStore(info.state_db, info.workspace_id) as store:
        with pytest.raises(DridhError) as execution_error:
            ExecutionRepository(store.connection, "another-workspace")
        with pytest.raises(DridhError) as index_error:
            CodeIndexWriter(store.connection, "another-workspace")
        with pytest.raises(DridhError) as jev_error:
            JevRepository(store.connection, "another-workspace")
    assert execution_error.value.code is ErrorCode.AUTHORIZATION_DENIED
    assert index_error.value.code is ErrorCode.AUTHORIZATION_DENIED
    assert jev_error.value.code is ErrorCode.AUTHORIZATION_DENIED


def test_backup_includes_workspace_artifacts_and_restores_atomically(tmp_path: Path):
    project, info = workspace(tmp_path)
    artifact = project / ".opencode" / "dridh" / "documents" / "notes.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Saved workspace note\n", encoding="utf-8")
    archive = tmp_path / "backup.zip"
    restored = tmp_path / "restored"
    restored.mkdir()

    result = backup_workspace(workspace_root=project, db_path=info.state_db, destination=archive, dridh_version="0.1.0")
    restored_result = restore_workspace(backup_path=archive, destination=restored)

    assert result["integrity"] == "ok" and result["files"] >= 1
    assert restored_result["integrity"] == "ok"
    assert (restored / ".opencode" / "dridh" / "documents" / "notes.md").read_text(encoding="utf-8") == "# Saved workspace note\n"
    assert integrity_check(restored / ".opencode" / "dridh" / "state" / "dridh.sqlite")["ok"]


def test_restore_rejects_zip_slip_without_writing_outside_destination(tmp_path: Path):
    _, info = workspace(tmp_path)
    archive = tmp_path / "malicious.zip"
    destination = tmp_path / "restored"
    metadata = {"format": "dridh-workspace-backup-v1", "schema_version": 10}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("backup.json", json.dumps(metadata))
        bundle.write(info.state_db, "state.sqlite")
        bundle.writestr("../outside.txt", "must not be written")

    with pytest.raises(DridhError) as refused:
        restore_workspace(backup_path=archive, destination=destination)

    assert refused.value.code is ErrorCode.AUTHORIZATION_DENIED
    assert not destination.exists()
    assert not (tmp_path / "outside.txt").exists()


def test_restore_rejects_duplicate_archive_paths_and_keeps_destination_empty(tmp_path: Path):
    _, info = workspace(tmp_path)
    archive = tmp_path / "duplicate.zip"
    destination = tmp_path / "restored"
    metadata = {"format": "dridh-workspace-backup-v1", "schema_version": 10}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("backup.json", json.dumps(metadata))
        bundle.write(info.state_db, "state.sqlite")
        with pytest.warns(UserWarning, match="Duplicate name"):
            bundle.writestr("state.sqlite", b"overwritten database")

    with pytest.raises(DridhError) as refused:
        restore_workspace(backup_path=archive, destination=destination)

    assert refused.value.code is ErrorCode.STATE_CORRUPT
    assert not destination.exists()


def test_restore_failure_cleans_staging_and_preserves_empty_destination(tmp_path: Path):
    archive = tmp_path / "invalid-db.zip"
    destination = tmp_path / "restored"
    destination.mkdir()
    metadata = {"format": "dridh-workspace-backup-v1", "schema_version": 10}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("backup.json", json.dumps(metadata))
        bundle.writestr("state.sqlite", b"not a sqlite database")

    with pytest.raises(DridhError) as refused:
        restore_workspace(backup_path=archive, destination=destination)

    assert refused.value.code is ErrorCode.STATE_CORRUPT
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".restored.dridh-restore-*"))


def test_backup_rejects_destination_inside_workspace(tmp_path: Path):
    project, info = workspace(tmp_path)
    with pytest.raises(DridhError) as refused:
        backup_workspace(
            workspace_root=project,
            db_path=info.state_db,
            destination=project / ".opencode" / "dridh" / "backup.zip",
            dridh_version="0.1.0",
        )
    assert refused.value.code is ErrorCode.VALIDATION_FAILED


def test_v8_to_v9_migration_rolls_back_ddl_on_failure(tmp_path: Path):
    from dridh.state.migration import migrate_to_current, schema_version

    _, info = workspace(tmp_path)
    with StateStore(info.state_db, info.workspace_id):
        pass

    connection = sqlite3.connect(info.state_db)
    tables = [
        "code_search_fts", "jev_provider_health", "jev_evaluation_criterion", "jev_evaluation",
        "code_embedding", "code_summary", "code_repository_mapping", "code_api_endpoint",
        "code_relation", "code_entity_parameter", "code_entity", "code_file", "code_index_run",
        "docker_test_environment", "integration_commit", "story_gate", "pull_request_check",
        "pull_request", "worktree", "git_branch",
    ]
    for table in tables:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    # An incompatible pre-existing table makes the subsequent migration index fail.
    connection.execute("CREATE TABLE worktree (id TEXT PRIMARY KEY)")
    connection.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(DridhError):
        migrate_to_current(info.state_db)

    assert schema_version(info.state_db) == 8
    connection = sqlite3.connect(info.state_db)
    remaining = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert "git_branch" not in remaining
    assert "worktree" in remaining
    assert list(info.state_db.parent.glob("dridh.sqlite.v8-execution-backup-*"))


def test_restore_rejects_symbolic_link_members(tmp_path: Path):
    _, info = workspace(tmp_path)
    archive = tmp_path / "symlink.zip"
    destination = tmp_path / "restored"
    metadata = {"format": "dridh-workspace-backup-v1", "schema_version": 10}
    link = zipfile.ZipInfo(".opencode/dridh/documents/external-link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("backup.json", json.dumps(metadata))
        bundle.write(info.state_db, "state.sqlite")
        bundle.writestr(link, "/outside/target")

    with pytest.raises(DridhError) as refused:
        restore_workspace(backup_path=archive, destination=destination)

    assert refused.value.code is ErrorCode.AUTHORIZATION_DENIED
    assert not destination.exists()
