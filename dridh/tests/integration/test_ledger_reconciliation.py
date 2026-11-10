"""Ledger import and reconciliation (plan 17.3)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

import pytest

from dridh.common.errors import DridhError
from dridh.document_contracts.ledger_store import record_ledger_decision
from dridh.document_contracts.validator import content_revision, ledger_entry_revision
from dridh.ledger.service import LedgerService, parse_entries
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline
from tests.integration.test_cli import run_module

HEADER = (
    "# Order Intake — Coordinator Ledger\n\n> **Standards Version:** 2.1.0  \n> **Document Status:** DRAFT  \n> **Last Updated:** 2026-11-08\n\n"
    "| Ledger ID | UC / Flow ID | Deviation Status | Linked Story ID | Original Requirement | Actual / Deviated Behavior | Justification | Approved By |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def ledger(*rows: tuple[str, str, str, str, str]) -> str:
    """rows: (id, origin, status, story, approver). Definition headings are generated."""
    table = "".join(f"| {i} | {o} | {st} | {s} | Original {i} | Actual {i} | Because {i} | {a} |\n" for i, o, st, s, a in rows)
    blocks = "".join(f"\n#### {i}: Entry {i}\n\n- **Disposition:** {st}.\n" for i, _o, st, _s, _a in rows)
    return HEADER + table + blocks


@pytest.fixture()
def world(tmp_path):
    info, _ = seed_approved_baseline(tmp_path / "project")
    return info, LedgerService(info), info.project_root / "LEDGER.md"


_OPEN: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_raw_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def raw(info):
    connection = sqlite3.connect(info.state_db)
    connection.row_factory = sqlite3.Row
    _OPEN.append(connection)  # closed after each test, so a failing assertion cannot leak it
    return connection


def decide(info, content, ledger_id, decision="approve", reviewer="Rohit Roy"):
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        record_ledger_decision(connection, ledger_id=ledger_id, decision=decision, reviewer=reviewer, reason="", actor="human:test",
                               entry_sha256=ledger_entry_revision(content, ledger_id) or "", document_sha256=content_revision(content))


def add_stories(info, covered):
    """Minimal story rows (foreign keys off: only the columns the ledger reads matter). covered: {story: [(uc, flow|None)]}."""
    connection = raw(info)
    connection.execute("PRAGMA foreign_keys=OFF")
    for number, (story, cover) in enumerate(covered.items(), start=1):
        connection.execute(
            "INSERT INTO story(id,workspace_id,document_id,source_revision_id,story_id,title,component_id,phase_number,status,position) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), info.workspace_id, "d", "r", story, f"Story {story}", "CMP-01", 1, "NOT_STARTED", number))
        for uc, flow in cover:
            connection.execute("DROP TRIGGER IF EXISTS story_use_case_flow_parent")
            connection.execute("INSERT INTO story_use_case(id,workspace_id,document_id,source_revision_id,story_id,use_case_id,flow_id) VALUES(?,?,?,?,?,?,?)",
                               (str(uuid.uuid4()), info.workspace_id, "d", "r", story, uc, flow))
    connection.commit()
    connection.close()


def use_case_digest(info):
    connection = raw(info)
    digest = hashlib.sha256()
    for table in ("use_case", "use_case_flow", "actor"):
        try:
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                digest.update(repr(tuple(row)).encode())
        except sqlite3.Error:
            pass
    return digest.hexdigest()


def codes(report):
    return {f["code"] for f in report["findings"]}


def test_import_stores_rows_with_hashes_and_reports_missing_approvals(world):
    info, service, path = world
    path.write_text(ledger(("LDG-01", "BE-01.1", "DEVIATED", "—", "—"), ("LDG-02", "UC-02", "DONE", "—", "—")))
    result = service.import_ledger("LEDGER.md")
    assert result["imported"] == 2 and result["awaiting_human_decision"] == ["LDG-01"]
    rows = raw(info).execute("SELECT ledger_id,title,origin_id,deviation_status,entry_sha256 FROM ledger_entry ORDER BY position").fetchall()
    assert [r["ledger_id"] for r in rows] == ["LDG-01", "LDG-02"] and rows[0]["title"] == "Entry LDG-01"
    assert rows[0]["entry_sha256"] == ledger_entry_revision(path.read_text(), "LDG-01")
    with pytest.raises(sqlite3.DatabaseError, match="replaced by import"):
        raw(info).execute("UPDATE ledger_entry SET deviation_status='DONE'")


def test_an_invalid_ledger_is_refused_and_nothing_is_imported(world):
    info, service, path = world
    path.write_text(ledger(("LDG-01", "NOT-A-REF", "DEVIATED", "—", "—")))
    with pytest.raises(DridhError, match="structural validation"):
        service.import_ledger("LEDGER.md")
    assert raw(info).execute("SELECT COUNT(*) FROM ledger_entry").fetchone()[0] == 0


def test_a_ledger_outside_the_project_is_refused(world, tmp_path):
    _, service, _ = world
    (tmp_path / "outside.md").write_text(ledger(("LDG-01", "UC-02", "DONE", "—", "—")))
    with pytest.raises(DridhError):
        service.import_ledger(str(tmp_path / "outside.md"))


def test_deviations_need_a_matching_human_decision_bound_to_the_entry(world):
    info, service, path = world
    content = ledger(("LDG-01", "BE-01.1", "DEVIATED", "—", "Rohit Roy"))
    path.write_text(content)
    service.import_ledger("LEDGER.md")
    assert "LEDGER_APPROVAL_MISSING" in codes(service.reconcile("LEDGER.md"))
    decide(info, content, "LDG-01")
    assert not codes(service.reconcile("LEDGER.md")) & {"LEDGER_APPROVAL_MISSING", "LEDGER_APPROVAL_STALE"}
    path.write_text(content.replace("Because LDG-01", "A different reason"))
    stale = service.reconcile("LEDGER.md")
    assert {"LEDGER_DRIFT", "LEDGER_APPROVAL_STALE"} <= codes(stale) and stale["ok"] is False


def test_drift_is_detected_and_repaired_from_the_file_without_touching_markdown_or_approvals(world):
    info, service, path = world
    content = ledger(("LDG-02", "UC-02", "DONE", "—", "—"))
    path.write_text(content)
    service.import_ledger("LEDGER.md")
    path.write_text(content.replace("Actual LDG-02", "Actual LDG-02 changed"))
    assert "LEDGER_DRIFT" in codes(service.reconcile("LEDGER.md"))
    before_file, before_ledger = path.read_text(), raw(info).execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    report = service.reconcile("LEDGER.md", repair=True)
    assert report["repaired"] is True and report["ok"] is True and "LEDGER_DRIFT" not in codes(report)
    assert path.read_text() == before_file and raw(info).execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == before_ledger
    assert raw(info).execute("SELECT actual_behavior FROM ledger_entry").fetchone()[0] == "Actual LDG-02 changed"


def test_tampered_rows_are_detected_and_repaired(world):
    info, service, path = world
    path.write_text(ledger(("LDG-02", "UC-02", "DONE", "—", "—")))
    service.import_ledger("LEDGER.md")
    connection = raw(info)
    connection.execute("DROP TRIGGER ledger_entry_immutable_update")
    connection.execute("UPDATE ledger_entry SET deviation_status='WONT_DO'")
    connection.commit()
    assert "LEDGER_ROWS_TAMPERED" in codes(service.reconcile("LEDGER.md"))
    fixed = service.reconcile("LEDGER.md", repair=True)
    assert fixed["repaired"] is True and "LEDGER_ROWS_TAMPERED" not in codes(fixed)
    assert raw(info).execute("SELECT deviation_status FROM ledger_entry").fetchone()[0] == "DONE"


def test_unknown_references_are_reported(world):
    info, service, path = world
    path.write_text(ledger(("LDG-01", "UC-09", "DONE", "—", "—"), ("LDG-02", "UC-02", "DONE", "ST-9.9", "—")))
    service.import_ledger("LEDGER.md")
    add_stories(info, {"ST-1.1": [("UC-01", None)]})
    report = service.reconcile("LEDGER.md")
    unknown = {f["entity"] for f in report["findings"] if f["code"] == "LEDGER_UNKNOWN_REFERENCE"}
    assert unknown == {"LDG-01", "LDG-02"}


def test_an_unimplemented_flow_needs_a_ledger_entry(world):
    info, service, path = world
    add_stories(info, {"ST-1.1": [("UC-01", "SF-01.1")]})
    path.write_text(ledger(("LDG-02", "UC-02", "DONE", "—", "—")))
    service.import_ledger("LEDGER.md")
    missing = {f["entity"] for f in service.reconcile("LEDGER.md")["findings"] if f["code"] == "LEDGER_DEVIATION_MISSING"}
    assert {"SF-01.2", "AF-01.1", "EF-01.1", "BE-01.1"} <= missing and "SF-01.1" not in missing
    content = ledger(("LDG-01", "UC-01", "WONT_DO", "—", "Rohit Roy"), ("LDG-02", "UC-02", "DONE", "—", "—"))
    path.write_text(content)
    service.import_ledger("LEDGER.md")
    decide(info, content, "LDG-01")
    covered = service.reconcile("LEDGER.md")
    assert "LEDGER_DEVIATION_MISSING" not in codes(covered)  # the UC-01 entry explains all of its flows


def test_story_status_contradicting_the_ledger_is_flagged(world):
    info, service, path = world
    add_stories(info, {"ST-1.1": [("UC-01", None)]})
    raw_connection = raw(info)
    raw_connection.execute("DROP TRIGGER IF EXISTS story_definition_immutable_update")
    raw_connection.execute("UPDATE story SET status='COMPLETED' WHERE story_id='ST-1.1'")
    raw_connection.commit()
    content = ledger(("LDG-01", "BE-01.1", "NOT_DONE", "ST-1.1", "Rohit Roy"))
    path.write_text(content)
    service.import_ledger("LEDGER.md")
    decide(info, content, "LDG-01")
    assert "LEDGER_STATUS_MISMATCH" in codes(service.reconcile("LEDGER.md"))


def test_importing_and_repairing_never_touch_the_approved_use_cases(world):
    info, service, path = world
    before = use_case_digest(info)
    path.write_text(ledger(("LDG-01", "BE-01.1", "DEVIATED", "—", "—")))
    service.import_ledger("LEDGER.md")
    path.write_text(path.read_text().replace("Because LDG-01", "changed"))
    service.reconcile("LEDGER.md", repair=True)
    assert use_case_digest(info) == before


def test_changed_use_cases_after_import_raise_a_warning(world):
    info, service, path = world
    path.write_text(ledger(("LDG-02", "UC-02", "DONE", "—", "—")))
    service.import_ledger("LEDGER.md")
    connection = raw(info)
    connection.execute("UPDATE ledger_import SET use_case_revision_id='an-older-revision'")
    connection.commit()
    warning = next(f for f in service.reconcile("LEDGER.md")["findings"] if f["code"] == "LEDGER_USE_CASE_CHANGED")
    assert warning["severity"] == "WARNING"


def test_an_unimported_ledger_is_reported(world):
    info, service, path = world
    path.write_text(ledger(("LDG-02", "UC-02", "DONE", "—", "—")))
    assert "LEDGER_NOT_IMPORTED" in codes(service.reconcile("LEDGER.md"))


def test_parse_entries_reads_every_column():
    entries = parse_entries(ledger(("LDG-01", "BE-01.1", "DEVIATED", "ST-1.2", "Rohit Roy")))
    assert entries[0]["linked_story_id"] == "ST-1.2" and entries[0]["approved_by"] == "Rohit Roy" and entries[0]["title"] == "Entry LDG-01"


def test_the_cli_imports_lists_reconciles_and_exits_with_the_validation_code(world):
    info, _, path = world
    content = ledger(("LDG-01", "BE-01.1", "DEVIATED", "—", "Rohit Roy"))
    path.write_text(content)
    args = ("--project", str(info.project_root))
    assert run_module("ledger", "import", "LEDGER.md", *args, cwd=info.project_root).returncode == 0
    listed = json.loads(run_module("ledger", "list", *args, cwd=info.project_root).stdout)
    assert [e["ledger_id"] for e in listed["entries"]] == ["LDG-01"]
    blocked = run_module("ledger", "reconcile", "LEDGER.md", *args, cwd=info.project_root)
    assert blocked.returncode == 4 and "LEDGER_APPROVAL_MISSING" in blocked.stdout  # a deviation without a human decision
    decide(info, content, "LDG-01")
    assert run_module("ledger", "reconcile", "LEDGER.md", *args, cwd=info.project_root).returncode == 0


def test_the_read_only_bridge_query_and_observe_report_the_ledger(world):
    from dridh.coordinator import queries
    from dridh.observability.metrics import collect

    info, service, path = world
    path.write_text(ledger(("LDG-01", "BE-01.1", "DEVIATED", "—", "—"), ("LDG-02", "UC-02", "DONE", "—", "—")))
    service.import_ledger("LEDGER.md")
    assert [r["ledger_id"] for r in queries.query(info, "ledger")["rows"]] == ["LDG-01", "LDG-02"]
    assert queries.query(info, "ledger", status="DEVIATED")["count"] == 1
    section = collect(info)["ledger"]
    assert section["imported"] is True and section["deviations"] == 1 and section["entries"] == {"DEVIATED": 1, "DONE": 1}
