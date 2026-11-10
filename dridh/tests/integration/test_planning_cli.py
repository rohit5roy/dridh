"""Real CLI path for `dridh planning` (Phase 5 increment 3). Subprocess tests against real SQLite."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace, open_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
FILES = {
    "uc": "001-USE-CASES.md",
    "tdd": "feature_tdd.md",
    "master": "COMPONENT-MASTER.md",
    "central": "CENTRAL-PLAN-INDEX.md",
    "phase": "phase-1-order-service.md",
    "tests": "TEST-PLAN.md",
}


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, cwd=cwd, stdin=subprocess.DEVNULL, timeout=180)


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    for name in FILES.values():
        (project / name).write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    return project


def import_all(project: Path) -> None:
    assert run("usecases", "import", FILES["uc"], "--project", str(project), cwd=project).returncode == 0
    assert run("design", "import", FILES["tdd"], "--project", str(project), cwd=project).returncode == 0
    assert run("design", "import", FILES["master"], "--project", str(project), cwd=project).returncode == 0
    for key in ("central", "phase", "tests"):
        result = run("planning", "import", FILES[key], "--project", str(project), cwd=project)
        assert result.returncode == 0, result.stderr


def rows(project: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(open_workspace(project).state_db)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_full_planning_chain_imports_and_verifies_clean(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    for key in ("central", "phase", "tests"):
        verified = run("planning", "verify", FILES[key], "--json", "--project", str(project), cwd=project)
        assert verified.returncode == 0, verified.stdout
        assert json.loads(verified.stdout)["details"]["reads_source_file"] is False


def test_graph_and_stories_report_order_unlocks_and_readiness(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    graph = json.loads(run("planning", "graph", "--project", str(project), cwd=project).stdout)
    assert graph["acyclic"] is True
    assert graph["blocked"] == ["ST-1.2"]
    assert graph["ready"] == ["ST-1.1", "ST-2.1"]
    stories = json.loads(run("planning", "stories", "--story", "ST-1.1", "--project", str(project), cwd=project).stdout)
    assert stories["stories"][0]["unlocks"] == ["ST-1.2"]
    assert stories["stories"][0]["parallel_with"] == ["ST-2.1"]
    again = json.loads(run("planning", "graph", "--project", str(project), cwd=project).stdout)
    assert again == graph, "graph output must be deterministic"


def test_repeated_import_writes_nothing(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    before = rows(project, "SELECT id FROM story ORDER BY id")
    again = json.loads(run("planning", "import", FILES["central"], "--project", str(project), cwd=project).stdout)
    assert again["projection_changed"] is False and again["revision_created"] is False
    assert rows(project, "SELECT id FROM story ORDER BY id") == before


def test_invalid_central_plan_is_refused_with_a_validation_exit_code(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    assert run("usecases", "import", FILES["uc"], "--project", str(project), cwd=project).returncode == 0
    assert run("design", "import", FILES["tdd"], "--project", str(project), cwd=project).returncode == 0
    assert run("design", "import", FILES["master"], "--project", str(project), cwd=project).returncode == 0
    broken = (FIXTURES / FILES["central"]).read_text(encoding="utf-8").replace("| Domain model | None |", "| Domain model | ST-1.2 |")
    (project / FILES["central"]).write_text(broken, encoding="utf-8")
    result = run("planning", "import", FILES["central"], "--project", str(project), cwd=project)
    assert result.returncode == 4
    assert json.loads(result.stderr)["error"]["code"] == "VALIDATION_FAILED"
    assert rows(project, "SELECT COUNT(*) FROM story") == [(0,)]


def test_tampered_planning_rows_fail_verification(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    connection = sqlite3.connect(open_workspace(project).state_db, isolation_level=None)
    connection.execute("DROP TRIGGER story_definition_immutable_update")
    connection.execute("UPDATE story SET title='Forged' WHERE story_id='ST-1.1'")
    connection.close()
    result = run("planning", "verify", FILES["central"], "--json", "--project", str(project), cwd=project)
    assert result.returncode == 1
    assert "PLANNING_MISMATCH" in {f["code"] for f in json.loads(result.stdout)["findings"]}


def test_parallel_group_refuses_dependent_stories(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    ok = run("planning", "group-create", "wave-1", "--stories", "ST-1.1,ST-2.1", "--project", str(project), cwd=project)
    assert ok.returncode == 0, ok.stderr
    bad = run("planning", "group-create", "bad", "--stories", "ST-1.1,ST-1.2", "--project", str(project), cwd=project)
    assert bad.returncode == 4
    assert json.loads(bad.stderr)["error"]["code"] == "VALIDATION_FAILED"
    assert rows(project, "SELECT COUNT(*) FROM story_parallel_group WHERE group_name='bad'") == [(0,)]


def test_evidence_is_recorded_listed_and_protects_the_story(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    recorded = run("planning", "evidence-record", "ST-1.1", "S1", "--status", "PASSED", "--ref", "verification/s1/report.xml", "--project", str(project), cwd=project)
    assert recorded.returncode == 0, recorded.stderr
    listed = json.loads(run("planning", "evidence-list", "--story", "ST-1.1", "--project", str(project), cwd=project).stdout)
    assert listed["evidence"][0]["evidence_ref"] == "verification/s1/report.xml"
    invalid = run("planning", "evidence-record", "ST-1.1", "S1", "--status", "MAYBE", "--ref", "x", "--project", str(project), cwd=project)
    assert invalid.returncode == 4
    edited = (FIXTURES / FILES["central"]).read_text(encoding="utf-8").replace("| Domain model |", "| Domain model v2 |")
    (project / FILES["central"]).write_text(edited, encoding="utf-8")
    reimport = run("planning", "import", FILES["central"], "--project", str(project), cwd=project)
    assert reimport.returncode == 4
    assert "discard" in json.loads(reimport.stderr)["error"]["message"]


def test_milestone_link_and_inspection(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    assert run("planning", "milestone-link", "M1", "ST-1.2", "--project", str(project), cwd=project).returncode == 0
    shown = json.loads(run("planning", "milestones", "--project", str(project), cwd=project).stdout)
    assert shown["milestones"][0]["stories"] == ["ST-1.2"]


def test_planning_import_requires_its_prerequisites_and_refuses_unknown_kinds(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    phase = run("planning", "import", FILES["phase"], "--project", str(project), cwd=project)
    assert phase.returncode == 4
    unknown = project / "notes.md"
    unknown.write_text("Plain prose.\n", encoding="utf-8")
    assert run("planning", "import", "notes.md", "--project", str(project), cwd=project).returncode == 4


def test_migrate_upgrades_a_v6_workspace_through_the_cli(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    import_all(project)
    info = open_workspace(project)
    connection = sqlite3.connect(info.state_db, isolation_level=None)
    for name in [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()]:
        if name.startswith(("story", "component_phase", "milestone", "risk", "test_phase", "planning")):
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("test_phase_component", "test_phase_flow", "test_phase_use_case", "test_phase", "risk", "milestone_story", "milestone", "story_test_evidence", "story_parallel_member", "story_parallel_group", "story_parallel_pair", "story_dependency", "story_use_case", "story", "component_phase", "planning_import"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")
    connection.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    connection.close()
    blocked = run("planning", "graph", "--project", str(project), cwd=project)
    assert blocked.returncode == 3
    migrated = run("migrate", "--project", str(project), cwd=project)
    assert migrated.returncode == 0, migrated.stderr
    assert json.loads(migrated.stdout)["steps"][-1]["to_version"] == 15
    assert run("usecases", "show", FILES["uc"], "--project", str(project), cwd=project).returncode == 0
