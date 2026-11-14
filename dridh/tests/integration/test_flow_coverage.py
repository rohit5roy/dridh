"""Flow coverage and the deviation ledger: decided by scripts, from test results and the approved documents."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from dridh.coordinator.delivery_check import delivery_readiness
from dridh.coordinator.lifecycle import StageEvidence
from dridh.executor import flow_coverage as fc
from dridh.ledger.drafts import LEDGER_PATH, add_drafts, ledger_file
from dridh.ledger.flows import approved_flows, story_flows
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline
from tests.integration.test_cli import run_module
from tests.integration.test_executor_controller import FakePlanner, ScriptedAgent, _controller, _git, _run, _story

PASSING = "import unittest\n\n\nclass T(unittest.TestCase):\n    def {name}(self):\n        self.assertTrue(True)\n"

# ------------------------------------------------------------------ the scripts on their own


def test_flow_ids_are_read_from_test_names_in_the_forms_a_test_name_allows():
    assert fc.flow_ids_in("test_ef_01_1_payment_declined") == {"EF-01.1"}
    assert fc.flow_ids_in('@DisplayName("UC-01 SF-01.2 payment recorded")') == {"SF-01.2"}
    assert fc.flow_ids_in("it('[BE-01.1] empty basket') and AF_01_1") == {"BE-01.1", "AF-01.1"}
    assert fc.flow_ids_in("ef0101 xef_01_1 ef_01_12") == {"EF-01.12"}  # no separators, or glued to a word: not a flow


def test_per_test_results_come_from_the_runner_output_and_fresh_junit_reports(tmp_path):
    pytest_out = "PASSED tests/test_a.py::test_ef_01_1_declined\nFAILED tests/test_a.py::test_sf_01_1_submit - assert 0\n"
    results = {r.name: r.passed for r in fc.parse_results(pytest_out)}
    assert results == {"tests/test_a.py::test_ef_01_1_declined": True, "tests/test_a.py::test_sf_01_1_submit": False}
    assert {r.name: r.passed for r in fc.parse_results("--- PASS: TestEF_01_1Declined (0.00s)\n--- FAIL: TestSF_01_1 (0.01s)\n")} == {
        "TestEF_01_1Declined": True, "TestSF_01_1": False}
    assert {r.name: r.passed for r in fc.parse_results("✔ [EF-01.1] declined (1.2ms)\n✖ [SF-01.1] submit (0.4ms)\n")} == {
        "[EF-01.1] declined": True, "[SF-01.1] submit": False}
    assert fc.parse_results("Ran 3 tests in 0.01s\n\nOK\n") is None  # unittest names no tests

    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    xml = ('<testsuite><testcase classname="OrderTest" name="ef_01_1_declined"/>'
           '<testcase classname="OrderTest" name="sf_01_1_submit"><failure message="x"/></testcase></testsuite>')
    (reports / "TEST-OrderTest.xml").write_text(xml)
    fresh = {r.name: r.passed for r in fc.parse_results("", tmp_path, since=time.time() - 60)}
    assert fresh == {"OrderTest.ef_01_1_declined": True, "OrderTest.sf_01_1_submit": False}
    assert fc.parse_results("", tmp_path, since=time.time() + 60) is None  # a report older than the run is not evidence


def test_a_flow_is_proven_by_a_passing_named_test_or_statically_when_the_runner_names_no_tests(tmp_path):
    results = {"passed": True, "test_results": [{"name": "t::test_ef_01_1_x", "passed": True}, {"name": "t::test_sf_01_1_x", "passed": False}]}
    proof = fc.prove(["EF-01.1", "SF-01.1", "BE-01.1"], results, tmp_path)
    assert proof["EF-01.1"]["proven"] and proof["EF-01.1"]["method"] == "results"
    assert not proof["SF-01.1"]["proven"] and not proof["BE-01.1"]["proven"] and proof["BE-01.1"]["reason"] == "no test named for it ran"

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_order.py").write_text("def test_be_01_1_empty_basket():\n    pass\n")
    static = fc.prove(["BE-01.1", "EF-01.1"], {"passed": True, "test_results": None}, tmp_path)
    assert static["BE-01.1"] == {"proven": True, "method": "static", "tests": ["tests/test_order.py"]}
    assert not static["EF-01.1"]["proven"]
    skipped = fc.prove(["BE-01.1"], {"passed": True, "skipped": 1, "test_results": None}, tmp_path)
    assert not skipped["BE-01.1"]["proven"]  # a skipped test could be the flow's own


def test_the_executor_flow_report_is_checked_against_the_story_flows():
    flows, findings = fc.check_report([{"id": "EF-01.1", "status": "IMPLEMENTED"}, {"id": "BE-01.1", "status": "NOT_DONE", "note": "later"}],
                                      ["EF-01.1", "BE-01.1"])
    assert findings == [] and flows["BE-01.1"] == {"status": "NOT_DONE", "note": "later"}
    _, findings = fc.check_report([{"id": "EF-01.1", "status": "DONE"}, {"id": "XX-01.1", "status": "IMPLEMENTED"},
                                   {"id": "BE-01.1", "status": "CHANGED"}], ["EF-01.1", "BE-01.1", "SF-01.1"])
    assert any("status must be one of" in f for f in findings) and any("XX-01.1 is not a flow" in f for f in findings)
    assert any("say in \"note\"" in f for f in findings) and any("missing from \"flows\": SF-01.1" in f for f in findings)
    assert fc.check_report(None, ["EF-01.1"])[1] and fc.check_report(None, [])[1] == []


# ------------------------------------------------------------------ the story runner, the ledger and the gate


@pytest.fixture()
def env(tmp_path):
    info, _session = seed_approved_baseline(tmp_path / "project")
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        _git(info.project_root, *args)
    (info.project_root / ".gitignore").write_text(".opencode/\n__pycache__/\n")
    _git(info.project_root, "add", "-A")
    _git(info.project_root, "commit", "-q", "-m", "base")
    from dridh.state.lifecycle import LifecycleRepository

    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        feature = LifecycleRepository(connection, info.workspace_id).create(slug="f", name="F", mode="new", actor="t")
    connection = sqlite3.connect(info.state_db)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DROP TRIGGER IF EXISTS story_use_case_flow_parent")
    for position, (story, flows) in enumerate({"ST-1.1": ["SF-01.1", "EF-01.1", "BE-01.1"], "ST-1.2": None}.items(), start=1):
        connection.execute("INSERT INTO story(id,workspace_id,document_id,source_revision_id,story_id,title,component_id,phase_number,status,position) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), info.workspace_id, "d", "r", story, story, "CMP-01", 1, "NOT_STARTED", position))
        for flow in flows or [None]:  # ST-1.2 owns the rest of UC-01 by flow, below
            connection.execute("INSERT INTO story_use_case(id,workspace_id,document_id,source_revision_id,story_id,use_case_id,flow_id) VALUES(?,?,?,?,?,?,?)",
                               (str(uuid.uuid4()), info.workspace_id, "d", "r", story, "UC-01", flow))
    connection.execute("DELETE FROM story_use_case WHERE story_id='ST-1.2'")
    with StateStore(info.state_db, info.workspace_id) as store:
        rest = [f for f in approved_flows(store.read(), info.workspace_id) if f not in ("SF-01.1", "EF-01.1", "BE-01.1")]
    for flow in rest:
        use_case = "UC-02" if flow.split("-")[1].startswith("02") else "UC-01"
        connection.execute("INSERT INTO story_use_case(id,workspace_id,document_id,source_revision_id,story_id,use_case_id,flow_id) VALUES(?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), info.workspace_id, "d", "r", "ST-1.2", use_case, flow))
    connection.commit()
    connection.close()
    return info, feature, rest


def _set_status(info, **statuses):
    connection = sqlite3.connect(info.state_db)
    for story, status in statuses.items():
        connection.execute("UPDATE story SET status=? WHERE story_id=?", (status, "ST-" + story[3:].replace("_", ".")))
    connection.commit()
    connection.close()


def _agent(reports: dict[str, list[dict]], tests: dict[str, list[str]]):
    """Writes code and the named tests, and reports the given flows."""
    def script(story_id: str, call: int, root: Path, kwargs: dict) -> dict:
        (root / "src").mkdir(exist_ok=True)
        (root / "tests").mkdir(exist_ok=True)
        (root / "src" / f"{story_id.lower()}.py").write_text(f"VALUE = {call}\n")
        for name in tests.get(story_id, ["test_plain"]):
            (root / "tests" / f"{name}.py").write_text(PASSING.format(name=name))
        return {"status": "DONE", "summary": "done", "files_changed": [f"src/{story_id.lower()}.py"], "flows": reports.get(story_id, [])}
    return script


def _drive(info, feature, agent, stories):
    planner = FakePlanner([_story(s) for s in stories])
    controller = _controller(info, planner, ScriptedAgent(agent))
    run = _run(info, feature)
    asyncio.run(controller.drive(run["id"]))
    return controller, planner, run


def test_the_executor_is_told_its_flows_and_a_proven_report_passes_while_deviations_become_drafts(env):
    info, feature, _rest = env
    reports = {"ST-1.1": [{"id": "SF-01.1", "status": "IMPLEMENTED"}, {"id": "EF-01.1", "status": "CHANGED", "note": "declines are retried once"},
                          {"id": "BE-01.1", "status": "NOT_DONE", "note": "the basket UI is not in this release"}]}
    agent = ScriptedAgent(_agent(reports, {"ST-1.1": ["test_sf_01_1_submit"]}))
    planner = FakePlanner([_story("ST-1.1")])
    controller = _controller(info, planner, agent)
    asyncio.run(controller.drive(_run(info, feature)["id"]))
    prompt = agent.calls[0]["prompt"]
    assert "SF-01.1 (success): Customer submits basket: Submit basket → Order request accepted" in prompt and '"flows"' in prompt
    assert planner.stories["ST-1.1"]["status"] == "IN_REVIEW"

    with StateStore(info.state_db, info.workspace_id) as store:
        attempt = json.loads(store.read().execute("SELECT evidence_json FROM story_attempt WHERE status='PASSED'").fetchone()[0])
    assert attempt["flow_proof"]["SF-01.1"]["proven"] and attempt["flow_proof"]["SF-01.1"]["method"] == "static"
    assert attempt["ledger_drafts"] == ["LDG-01", "LDG-02"], attempt.get("warnings")
    text = ledger_file(info).read_text()
    assert "| LDG-02 | EF-01.1 | DEVIATED | ST-1.1 | Payment is declined: Return decline outcome → Order is not placed | declines are retried once |" in text
    assert "| LDG-01 | BE-01.1 | NOT_DONE | ST-1.1 |" in text and text.count("| — |") == 2  # no approver yet
    assert add_drafts(info, [{"flow": "BE-01.1", "status": "NOT_DONE", "story": "ST-1.1", "actual": "x", "reason": "y"}])["written"] == []


def test_an_unproven_or_incomplete_report_is_repaired_like_a_failing_check(env):
    info, feature, _rest = env
    calls: list[int] = []

    def script(story_id, call, root, kwargs):
        calls.append(call)
        first = call == 1
        reports = {"ST-1.1": [{"id": "SF-01.1", "status": "IMPLEMENTED"}] + ([] if first else [
            {"id": "EF-01.1", "status": "IMPLEMENTED"}, {"id": "BE-01.1", "status": "IMPLEMENTED"}])}
        tests = {"ST-1.1": ["test_plain"] if call < 3 else ["test_sf_01_1_a", "test_ef_01_1_b", "test_be_01_1_c"]}
        return _agent(reports, tests)(story_id, call, root, kwargs)

    agent = ScriptedAgent(script)
    planner = FakePlanner([_story("ST-1.1")])
    asyncio.run(_controller(info, planner, agent).drive(_run(info, feature)["id"]))
    assert len(agent.calls) == 3 and planner.stories["ST-1.1"]["status"] == "IN_REVIEW"
    assert "These flows are missing from \"flows\": BE-01.1, EF-01.1." in agent.calls[1]["prompt"]
    assert "SF-01.1 is reported IMPLEMENTED but no test file names it" in agent.calls[2]["prompt"]
    assert not ledger_file(info).exists()  # nothing deviated


def test_leaving_execution_needs_every_flow_proven_or_explained_with_an_approved_entry(env, monkeypatch):
    info, feature, rest = env
    reports = {"ST-1.1": [{"id": "SF-01.1", "status": "IMPLEMENTED"}, {"id": "EF-01.1", "status": "IMPLEMENTED"},
                          {"id": "BE-01.1", "status": "NOT_DONE", "note": "later release"}],
               "ST-1.2": [{"id": f, "status": "IMPLEMENTED"} for f in rest]}
    tests = {"ST-1.1": ["test_sf_01_1_a", "test_ef_01_1_b"], "ST-1.2": [f"test_{f.lower().replace('-', '_').replace('.', '_')}_x" for f in rest]}
    _drive(info, feature, _agent(reports, tests), ["ST-1.1", "ST-1.2"])

    _set_status(info, ST_1_1="COMPLETED", ST_1_2="IN_REVIEW")
    check = delivery_readiness(info)
    assert not check["ready"] and any("ST-1.2 is IN_REVIEW" in b for b in check["blockers"])
    _set_status(info, ST_1_2="COMPLETED")
    check = delivery_readiness(info)
    assert check["awaiting_approval"] == ["LDG-01"] and not check["ready"]
    assert check["flows"]["SF-01.1"] == "proven (static)" and check["flows"]["BE-01.1"] == "explained in the ledger"
    readiness = StageEvidence(info).can_leave({"stage": "EXECUTION"}, "EXECUTION")
    assert not readiness.ready and "await a human decision" in readiness.blockers[-1]
    from dridh.ui.state import workspace_state

    connection = sqlite3.connect(info.state_db)  # the feature is in execution (its earlier stages are covered elsewhere)
    connection.execute("UPDATE feature SET stage='EXECUTION'")
    connection.execute("UPDATE execution_run SET status='COMPLETED'")  # its checkpoint lots were reviewed
    connection.commit()
    connection.close()
    action = workspace_state(info, "f")["human_action"]
    assert action["kind"] == "deviations" and "dridh ledger approve LDG-01 --reviewer <name>" in action["text"]

    from dridh.cli import main as cli

    monkeypatch.setattr(cli, "_confirm", lambda expected, summary: None)  # the human typed the confirmation
    monkeypatch.setattr(cli, "_emit", lambda *a, **k: None)
    cli._ledger_decide("LDG-01", "approve", "Rohit Roy", "", "", str(info.project_root))
    assert "| LDG-01 | BE-01.1 | NOT_DONE | ST-1.1 |" in ledger_file(info).read_text() and "| Rohit Roy |" in ledger_file(info).read_text()
    check = delivery_readiness(info)
    assert check["ready"], check["blockers"]
    assert StageEvidence(info).can_leave({"stage": "EXECUTION"}, "EXECUTION").ready

    text = ledger_file(info).read_text()  # an approved entry edited afterwards needs approval again
    ledger_file(info).write_text(text.replace("later release", "never"))
    run_module("ledger", "import", LEDGER_PATH, "--project", str(info.project_root))
    assert not delivery_readiness(info)["ready"]


def test_a_blocked_story_passes_only_when_a_human_records_its_flows_as_not_done(env, monkeypatch):
    info, feature, rest = env
    reports = {"ST-1.2": [{"id": f, "status": "IMPLEMENTED"} for f in rest]}
    tests = {"ST-1.2": [f"test_{f.lower().replace('-', '_').replace('.', '_')}_x" for f in rest]}
    _drive(info, feature, _agent(reports, tests), ["ST-1.2"])
    _set_status(info, ST_1_1="BLOCKED", ST_1_2="COMPLETED")
    check = delivery_readiness(info)
    assert any("ST-1.1 is BLOCKED" in b and "dridh ledger draft --story ST-1.1" in b for b in check["blockers"])

    refused = run_module("ledger", "draft", "--story", "ST-1.2", "--reason", "x", "--project", str(info.project_root))
    assert refused.returncode != 0 and "only a BLOCKED story" in refused.stdout + refused.stderr
    drafted = run_module("ledger", "draft", "--story", "ST-1.1", "--reason", "Payments move to the next release", "--project", str(info.project_root))
    assert drafted.returncode == 0, drafted.stdout + drafted.stderr
    assert json.loads(drafted.stdout)["written"] == ["LDG-01", "LDG-02", "LDG-03"]
    assert delivery_readiness(info)["awaiting_approval"] == ["LDG-01", "LDG-02", "LDG-03"]

    from dridh.cli import main as cli

    monkeypatch.setattr(cli, "_confirm", lambda expected, summary: None)
    monkeypatch.setattr(cli, "_emit", lambda *a, **k: None)
    for ledger_id in ("LDG-01", "LDG-02", "LDG-03"):
        cli._ledger_decide(ledger_id, "approve", "Rohit Roy", "", "", str(info.project_root))
    assert delivery_readiness(info)["ready"]


def test_a_story_without_flow_evidence_blocks_with_a_way_out(env):
    """Stories delivered before flow reports existed have no evidence: the gate says what to do, it never passes silently."""
    info, _feature, _rest = env
    _set_status(info, ST_1_1="COMPLETED", ST_1_2="COMPLETED")
    check = delivery_readiness(info)
    assert not check["ready"] and any("SF-01.1 (ST-1.1): the story has no flow report" in b for b in check["blockers"])
    with StateStore(info.state_db, info.workspace_id) as store:
        assert story_flows(store.read(), info.workspace_id)["ST-1.1"] == ["BE-01.1", "EF-01.1", "SF-01.1"]


def test_check_delivery_and_the_ledger_commands_find_the_workspace_ledger(env):
    info, _feature, _rest = env
    result = run_module("feature", "check-delivery", "--project", str(info.project_root))
    assert result.returncode == 0 and json.loads(result.stdout)["ready"] is False
    assert os.path.relpath(ledger_file(info), info.project_root) == LEDGER_PATH
