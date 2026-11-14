"""The completion matrix: every approved flow with its story, state, proof or deviation, and code."""

from __future__ import annotations

import json

from dridh.coordinator.coverage import coverage, render, tables
from tests.integration.test_cli import run_module
from tests.integration.test_flow_coverage import _agent, _drive, _set_status, env  # noqa: F401 - env is a fixture


def _run(info, feature, rest):
    reports = {"ST-1.1": [{"id": "SF-01.1", "status": "IMPLEMENTED"}, {"id": "EF-01.1", "status": "IMPLEMENTED"},
                          {"id": "BE-01.1", "status": "NOT_DONE", "note": "later release"}],
               "ST-1.2": [{"id": f, "status": "IMPLEMENTED"} for f in rest]}
    tests = {"ST-1.1": ["test_sf_01_1_a", "test_ef_01_1_b"], "ST-1.2": [f"test_{f.lower().replace('-', '_').replace('.', '_')}_x" for f in rest]}
    _drive(info, feature, _agent(reports, tests), ["ST-1.1", "ST-1.2"])


def test_every_approved_flow_is_a_row_with_its_story_state_and_proof(env):  # noqa: F811
    info, feature, rest = env
    before = coverage(info)
    assert before["total"] == 8 and before["done"] == 0
    assert {row["state"] for g in before["use_cases"] for row in g["rows"]} == {"not_started"}  # planned, nothing run yet

    _run(info, feature, rest)
    _set_status(info, ST_1_1="COMPLETED", ST_1_2="IN_REVIEW")
    report = coverage(info)
    rows = {row["flow"]: row for g in report["use_cases"] for row in g["rows"]}
    assert rows["SF-01.1"]["state"] == "proven" and rows["SF-01.1"]["story"] == "ST-1.1" and rows["SF-01.1"]["proof"] == "tests/test_sf_01_1_a.py"
    assert rows["BE-01.1"]["state"] == "awaiting" and rows["BE-01.1"]["proof"] == "LDG-01 not done, awaits approval"
    assert rows["BE-01.1"]["detail"]["awaiting"] is True and rows["BE-01.1"]["detail"]["ledger"] == "LDG-01"
    assert rows[rest[0]]["state"] == "in_review"  # its story is not complete yet
    assert rows["SF-01.1"]["detail"]["text"].startswith("Customer submits basket")
    assert report["summary"] == "2 of 8 flows proven or explained · 1 await you"
    uc01 = next(g for g in report["use_cases"] if g["use_case"] == "UC-01")
    assert uc01["title"] and uc01["done"] == 2

    text = render(report)
    assert "✓ proven" in text and "! deviation awaits you" in text and "… in review" in text
    assert tables(report)[0]["lines"][0].split() == ["Flow", "Type", "Story", "Status", "Proof", "or", "deviation", "Code"]


def test_the_panel_and_the_terminal_get_the_same_matrix(env):  # noqa: F811
    from dridh.ui.state import workspace_state

    info, feature, rest = env
    _run(info, feature, rest)
    _set_status(info, ST_1_1="COMPLETED", ST_1_2="COMPLETED")
    served = json.loads(run_module("ui", "coverage", "--project", str(info.project_root)).stdout)
    assert served["total"] == 8 and len(served["widths"]) == 6 and served["summary"].startswith("7 of 8 flows")
    printed = run_module("coverage", "--project", str(info.project_root)).stdout
    assert served["summary"] in printed and "SF-01.1" in printed
    assert workspace_state(info, "f")["coverage"] == served["summary"]


def test_before_any_story_exists_there_is_no_coverage_line(tmp_path):
    from dridh.ui.state import workspace_state
    from tests.e2e.planner_test_helpers import seed_approved_baseline

    info, _ = seed_approved_baseline(tmp_path / "project")
    report = coverage(info)
    assert report["counts"] == {"no_story": 8} and report["summary"].startswith("0 of 8 flows")  # approved, not yet planned
    assert workspace_state(info).get("coverage", "") == ""  # the overview line appears once stories exist
