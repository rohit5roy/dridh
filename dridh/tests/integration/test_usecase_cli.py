"""Real CLI path for `dridh usecases` (Phase 5 increment 1). Subprocess tests, real SQLite."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace, open_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
VALID = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
INVALID = (FIXTURES / "invalid" / "uc-1a-wrong-column-order.md").read_text(encoding="utf-8")
EDITED = VALID.replace("| Order request accepted |", "| Order request is accepted |")
EMPTY = (
    "# Empty Feature\n\n> **Standards Version:** 2.1.0  \n\n## Actors Registry\n\n"
    "| Actor ID | Actor Name | Type | Status | Description |\n|---|---|---|---|---|\n| — | — | — | — | — |\n\n"
    "## Use Case Summary Matrix\n\n| ID | Use Case | Primary Actor | Trigger | Category |\n|---|---|---|---|---|\n| — | — | — | — | — |\n"
)


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, cwd=cwd, stdin=subprocess.DEVNULL, timeout=180)


def make_workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project


def write(project: Path, text: str, name: str = "001-USE-CASES.md") -> Path:
    path = project / name
    path.write_text(text, encoding="utf-8")
    return path


def table_count(project: Path, table: str) -> int:
    info = open_workspace(project)
    connection = sqlite3.connect(info.state_db)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def test_import_stores_the_canonical_rows_and_show_returns_them(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    imported = run("usecases", "import", str(path), "--project", str(project), cwd=project)
    assert imported.returncode == 0, imported.stderr
    payload = json.loads(imported.stdout)
    assert payload["counts"] == {"actors": 2, "use_cases": 2, "flows": 8}
    shown = json.loads(run("usecases", "show", str(path), "--project", str(project), cwd=project).stdout)
    assert [a["actor_id"] for a in shown["actors"]] == ["ACT-01", "ACT-02"]
    assert [u["use_case_id"] for u in shown["use_cases"]] == ["UC-01", "UC-02"]
    flow = next(f for f in shown["flows"] if f["flow_id"] == "EF-01.1")
    assert flow["origin_step_flow_id"] == "SF-01.2"


def test_reimport_is_idempotent(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    run("usecases", "import", str(path), "--project", str(project), cwd=project)
    again = run("usecases", "import", str(path), "--project", str(project), cwd=project)
    assert again.returncode == 0
    assert table_count(project, "use_case_flow") == 8
    assert table_count(project, "use_case") == 2


def test_invalid_document_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, INVALID)
    result = run("usecases", "import", str(path), "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert json.loads(result.stderr)["error"]["code"] == "VALIDATION_FAILED"
    assert table_count(project, "use_case_flow") == 0
    assert table_count(project, "document_revision") == 0


def test_verify_passes_after_import_and_fails_before(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    before = run("usecases", "verify", str(path), "--json", "--project", str(project), cwd=project)
    assert before.returncode == 1
    assert "PROJECTION_MISSING" in {f["code"] for f in json.loads(before.stdout)["findings"]}
    run("usecases", "import", str(path), "--project", str(project), cwd=project)
    after = run("usecases", "verify", str(path), "--project", str(project), cwd=project)
    assert after.returncode == 0, after.stdout
    assert after.stdout.startswith("PASS  dridh validate usecases verify")


def test_verify_detects_a_tampered_projection(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    run("usecases", "import", str(path), "--project", str(project), cwd=project)
    info = open_workspace(project)
    raw = sqlite3.connect(info.state_db, isolation_level=None)
    raw.execute("DROP TRIGGER use_case_flow_immutable_update")
    raw.execute("UPDATE use_case_flow SET action_text='tampered' WHERE flow_id='SF-01.1'")
    raw.close()
    result = run("usecases", "verify", str(path), "--json", "--project", str(project), cwd=project)
    assert result.returncode == 1
    assert "PROJECTION_MISMATCH" in {f["code"] for f in json.loads(result.stdout)["findings"]}


def test_v3_workspace_is_blocked_until_migrated_then_imports(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    info = open_workspace(project)
    raw = sqlite3.connect(info.state_db, isolation_level=None)
    for name in [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'actor%' OR name LIKE 'use_case%')").fetchall()]:
        raw.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("use_case_flow", "use_case", "actor"):
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    raw.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    raw.close()
    path = write(project, VALID)
    blocked = run("validate", "document-sync", str(path), "--json", "--project", str(project), cwd=project)
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout)["status"] == "BLOCKED"
    migrated = run("migrate", "--project", str(project), cwd=project)
    assert migrated.returncode == 0, migrated.stderr
    assert [s["to_version"] for s in json.loads(migrated.stdout)["steps"]] == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    imported = run("usecases", "import", str(path), "--project", str(project), cwd=project)
    assert imported.returncode == 0, imported.stderr


def _rows(project: Path, table: str, column: str) -> list[str]:
    info = open_workspace(project)
    connection = sqlite3.connect(info.state_db)
    try:
        return sorted(r[0] for r in connection.execute(f"SELECT {column} FROM {table}"))
    finally:
        connection.close()


def test_repeated_import_creates_no_revision_and_keeps_internal_ids(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    first = json.loads(run("usecases", "import", str(path), "--project", str(project), cwd=project).stdout)
    ids_before = _rows(project, "use_case_flow", "id")
    second = json.loads(run("usecases", "import", str(path), "--project", str(project), cwd=project).stdout)
    assert first["revision_created"] is True and first["projection_changed"] is True
    assert second["revision_created"] is False and second["projection_changed"] is False
    assert _rows(project, "use_case_flow", "id") == ids_before, "unchanged re-import must not rewrite rows"
    assert table_count(project, "document_revision") == 1


def test_edit_then_import_creates_one_revision_and_replaces_rows(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    run("usecases", "import", str(path), "--project", str(project), cwd=project)
    ids_before = _rows(project, "use_case_flow", "id")
    write(project, EDITED)
    changed = json.loads(run("usecases", "import", str(path), "--project", str(project), cwd=project).stdout)
    assert changed["revision_created"] is True and changed["projection_changed"] is True
    assert table_count(project, "document_revision") == 2
    assert not set(ids_before) & set(_rows(project, "use_case_flow", "id"))


def test_empty_valid_document_imports_and_verifies_clean(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, EMPTY)
    imported = run("usecases", "import", str(path), "--project", str(project), cwd=project)
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["counts"] == {"actors": 0, "use_cases": 0, "flows": 0}
    verified = run("usecases", "verify", str(path), "--project", str(project), cwd=project)
    assert verified.returncode == 0, verified.stdout


def test_verify_declares_that_it_does_not_read_the_source_file(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    path = write(project, VALID)
    run("usecases", "import", str(path), "--project", str(project), cwd=project)
    write(project, EDITED)
    result = run("usecases", "verify", str(path), "--json", "--project", str(project), cwd=project)
    document = json.loads(result.stdout)
    assert document["details"]["reads_source_file"] is False
    assert result.returncode == 0, "the file was edited but the stored revision is unchanged"
