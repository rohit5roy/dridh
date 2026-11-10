"""Real CLI path for `dridh design` and the use-case prerequisite (Phase 5 increment 2). Subprocess tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace, open_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
UC = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
TDD = (FIXTURES / "feature_tdd.md").read_text(encoding="utf-8")
TDD_RESIDUE = (FIXTURES / "feature_tdd_residue.md").read_text(encoding="utf-8")
MASTER = (FIXTURES / "COMPONENT-MASTER.md").read_text(encoding="utf-8")


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, cwd=cwd, stdin=subprocess.DEVNULL, timeout=180)


def make_workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project


def write(project: Path, text: str, name: str) -> Path:
    path = project / name
    path.write_text(text, encoding="utf-8")
    return path


def rows(project: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(open_workspace(project).state_db)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_design_import_requires_the_use_cases_first(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    tdd = write(project, TDD, "feature_tdd.md")
    result = run("design", "import", str(tdd), "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert "use cases" in json.loads(result.stderr)["error"]["message"]
    assert rows(project, "SELECT COUNT(*) FROM component") == [(0,)]


def test_component_master_requires_its_tdd_first(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    master = write(project, MASTER, "COMPONENT-MASTER.md")
    result = run("design", "import", str(master), "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert "not defined by an imported TDD" in json.loads(result.stderr)["error"]["message"]


def test_full_import_sequence_and_show(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    uc, tdd, master = write(project, UC, "001-USE-CASES.md"), write(project, TDD, "feature_tdd.md"), write(project, MASTER, "COMPONENT-MASTER.md")
    assert run("usecases", "import", str(uc), "--project", str(project), cwd=project).returncode == 0
    imported = run("design", "import", str(tdd), "--project", str(project), cwd=project)
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["counts"] == {"components": 2, "decisions": 1, "mappings": 7, "api_events": 7}
    assert run("design", "import", str(master), "--project", str(project), cwd=project).returncode == 0
    shown = json.loads(run("design", "show", str(tdd), "--project", str(project), cwd=project).stdout)
    assert [c["component_id"] for c in shown["components"]] == ["CMP-01", "CMP-02"]
    assert [d["decision_id"] for d in shown["decisions"]] == ["DD-1"]
    master_shown = json.loads(run("design", "show", str(master), "--project", str(project), cwd=project).stdout)
    assert master_shown["component_id"] == "CMP-01"
    assert [d["decision_id"] for d in master_shown["decisions"]] == ["CDD-1"]


def test_unchanged_reimport_writes_nothing(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    uc, tdd = write(project, UC, "001-USE-CASES.md"), write(project, TDD, "feature_tdd.md")
    run("usecases", "import", str(uc), "--project", str(project), cwd=project)
    run("design", "import", str(tdd), "--project", str(project), cwd=project)
    ids = rows(project, "SELECT id FROM component ORDER BY id")
    again = json.loads(run("design", "import", str(tdd), "--project", str(project), cwd=project).stdout)
    assert again["revision_created"] is False and again["projection_changed"] is False
    assert rows(project, "SELECT id FROM component ORDER BY id") == ids
    assert rows(project, "SELECT COUNT(*) FROM document_revision") == [(2,)]  # one use-case revision, one TDD revision


def test_invalid_tdd_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    uc, tdd = write(project, UC, "001-USE-CASES.md"), write(project, TDD_RESIDUE, "feature_tdd.md")
    run("usecases", "import", str(uc), "--project", str(project), cwd=project)
    result = run("design", "import", str(tdd), "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert rows(project, "SELECT COUNT(*) FROM component") == [(0,)]


def test_design_verify_reports_and_declares_scope(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    uc, tdd = write(project, UC, "001-USE-CASES.md"), write(project, TDD, "feature_tdd.md")
    before = run("design", "verify", str(tdd), "--json", "--project", str(project), cwd=project)
    assert before.returncode == 1
    assert "DESIGN_NEVER_IMPORTED" in {f["code"] for f in json.loads(before.stdout)["findings"]}
    run("usecases", "import", str(uc), "--project", str(project), cwd=project)
    run("design", "import", str(tdd), "--project", str(project), cwd=project)
    passed = run("design", "verify", str(tdd), "--json", "--project", str(project), cwd=project)
    assert passed.returncode == 0
    assert json.loads(passed.stdout)["details"]["reads_source_file"] is False


def test_design_verify_detects_tampered_components(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    uc, tdd = write(project, UC, "001-USE-CASES.md"), write(project, TDD, "feature_tdd.md")
    run("usecases", "import", str(uc), "--project", str(project), cwd=project)
    run("design", "import", str(tdd), "--project", str(project), cwd=project)
    connection = sqlite3.connect(open_workspace(project).state_db, isolation_level=None)
    connection.execute("DROP TRIGGER component_immutable_update")
    connection.execute("UPDATE component SET definition_text='tampered' WHERE component_id='CMP-01'")
    connection.close()
    result = run("design", "verify", str(tdd), "--json", "--project", str(project), cwd=project)
    assert result.returncode == 1
    assert "DESIGN_PROJECTION_MISMATCH" in {f["code"] for f in json.loads(result.stdout)["findings"]}
