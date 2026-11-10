"""Real CLI path for `dridh documents` and `dridh validate document-sync` (Phase 5 document domain).

Each test runs the installed command in a child process against a real workspace database.
Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED. A BLOCKED result requires a genuinely missing prerequisite.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace

V1 = "# Order Intake\n\n## Actors\n\nText one.\n\n## Flows\n\nText two.\n"
V2 = "# Order Intake\n\n## Actors\n\nText one, edited.\n\n## Flows\n\nText two.\n"
DOC = "001-USE-CASES.md"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, cwd=cwd, stdin=subprocess.DEVNULL, timeout=180)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project


def write(project: Path, text: str, name: str = DOC) -> Path:
    path = project / name
    path.write_text(text, encoding="utf-8")
    return path


def revision_count(project: Path) -> int:
    from dridh.workspace.layout import open_workspace

    info = open_workspace(project)
    connection = sqlite3.connect(info.state_db)
    try:
        return connection.execute("SELECT COUNT(*) FROM document_revision").fetchone()[0]
    finally:
        connection.close()


def test_record_is_idempotent_and_creates_new_revisions_only_on_change(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    first = json.loads(run("documents", "record", str(path), "--project", str(project), cwd=project).stdout)
    assert first["created"] is True and first["revision_number"] == 1
    again = json.loads(run("documents", "record", str(path), "--project", str(project), cwd=project).stdout)
    assert again["created"] is False and again["revision_number"] == 1
    write(project, V2)
    edited = json.loads(run("documents", "record", str(path), "--project", str(project), cwd=project).stdout)
    assert edited["created"] is True and edited["revision_number"] == 2
    assert revision_count(project) == 2


def test_record_with_validate_persists_the_result_against_the_revision(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    outcome = json.loads(run("documents", "record", str(path), "--validate", "--project", str(project), cwd=project).stdout)
    assert outcome["validation"]["id"]
    shown = json.loads(run("documents", "show", str(path), "--project", str(project), cwd=project).stdout)
    assert shown["current_revision"] == 1
    assert shown["validations"][0]["revision_number"] == 1


def test_sync_passes_after_record_and_human_output_says_so(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    run("documents", "record", str(path), "--validate", "--project", str(project), cwd=project)
    result = run("validate", "document-sync", str(path), "--project", str(project), cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("PASS  dridh validate document-sync")


def test_sync_fails_on_unrecorded_edit_and_never_writes(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    run("documents", "record", str(path), "--project", str(project), cwd=project)
    write(project, V2)
    file_before, revisions_before = sha(path), revision_count(project)
    result = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert result.returncode == 1
    document = json.loads(result.stdout)
    assert document["status"] == "FAIL"
    changed = next(f for f in document["findings"] if f["code"] == "SOURCE_CHANGED_SINCE_REVISION")
    assert changed["expected"] and changed["actual"] and changed["expected"] != changed["actual"]
    assert "dridh documents record" in changed["repair_hint"]
    assert sha(path) == file_before, "sync must not modify the Markdown"
    assert revision_count(project) == revisions_before, "sync must not modify the database"


def test_sync_fails_for_a_document_never_recorded(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    result = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert result.returncode == 1
    assert "DOCUMENT_NOT_PERSISTED" in {f["code"] for f in json.loads(result.stdout)["findings"]}


def test_sync_fails_when_the_source_file_is_missing(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    run("documents", "record", str(path), "--project", str(project), cwd=project)
    path.unlink()
    result = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert result.returncode == 1
    assert "SOURCE_FILE_MISSING" in {f["code"] for f in json.loads(result.stdout)["findings"]}


def test_stale_validation_is_a_warning_unless_required(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    run("documents", "record", str(path), "--validate", "--project", str(project), cwd=project)
    write(project, V2)
    run("documents", "record", str(path), "--project", str(project), cwd=project)
    lenient = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert lenient.returncode == 0
    assert {f["code"] for f in json.loads(lenient.stdout)["findings"]} == {"VALIDATION_STALE"}
    strict = run("validate", "document-sync", str(path), "--require-validation", "--json", "--project", str(project), cwd=project)
    assert strict.returncode == 1


def test_no_workspace_is_blocked_with_exit_two(tmp_path: Path) -> None:
    project = tmp_path / "bare"
    project.mkdir()
    path = write(project, V1)
    result = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert result.returncode == 2
    document = json.loads(result.stdout)
    assert document["status"] == "BLOCKED"
    assert "PREREQUISITE_WORKSPACE_NOT_FOUND" in {f["code"] for f in document["findings"]}


def test_v2_workspace_is_blocked_until_dridh_migrate_runs(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    from dridh.workspace.layout import open_workspace

    info = open_workspace(project)
    raw = sqlite3.connect(info.state_db, isolation_level=None)
    for trigger in ("document_finding_immutable_delete", "document_finding_immutable_update", "document_validation_immutable_delete", "document_validation_immutable_update",
                    "document_section_immutable_delete", "document_section_immutable_update", "document_revision_immutable_delete", "document_revision_immutable_update",
                    "document_no_delete", "document_workspace_guard"):
        raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in ("document_validation_finding", "document_validation", "document_section", "document_revision", "document"):
        raw.execute(f"DROP TABLE IF EXISTS {table}")
    raw.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    raw.close()

    blocked = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert blocked.returncode == 2
    assert "PREREQUISITE_SCHEMA_UNSUPPORTED" in {f["code"] for f in json.loads(blocked.stdout)["findings"]}

    migrated = run("migrate", "--project", str(project), cwd=project)
    assert migrated.returncode == 0, migrated.stderr
    assert json.loads(migrated.stdout)["migrated"] is True

    run("documents", "record", str(path), "--project", str(project), cwd=project)
    passed = run("validate", "document-sync", str(path), "--project", str(project), cwd=project)
    assert passed.returncode == 0


def test_path_outside_the_project_is_rejected(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    outside = write(tmp_path, V1, name="outside.md")
    result = run("documents", "record", str(outside), "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert json.loads(result.stderr)["error"]["code"] == "VALIDATION_FAILED"


def test_unknown_document_type_is_not_persisted(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, "Meeting notes with no schema.\n", name="notes.md")
    result = run("documents", "record", str(path), "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert revision_count(project) == 0


def test_documents_show_reports_history_without_writing(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, V1)
    run("documents", "record", str(path), "--project", str(project), cwd=project)
    write(project, V2)
    run("documents", "record", str(path), "--project", str(project), cwd=project)
    shown = json.loads(run("documents", "show", str(path), "--project", str(project), cwd=project).stdout)
    assert shown["current_revision"] == 2
    assert [r["revision_number"] for r in shown["revisions"]] == [1, 2]
    assert shown["metadata"]["document_type"] == "use_cases"
