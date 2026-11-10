"""Delivery rules: check evaluation (16.3), base selection (16.5) and the GitHub adapter."""

from __future__ import annotations

import json

import pytest

from dridh.common.errors import DridhError
from dridh.delivery.github import GhCli, parse_remote
from dridh.delivery.policy import evaluate_checks, select_base


def _check(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_all_reported_checks_must_succeed_by_default():
    assert evaluate_checks([_check("a"), _check("b")], required=[], optional=[])["state"] == "PASS"
    failed = evaluate_checks([_check("a"), _check("b", conclusion="failure")], required=[], optional=[])
    assert failed["state"] == "FAIL" and "b" in failed["reasons"][0]


def test_missing_pending_and_empty_results_are_never_success():
    assert evaluate_checks([], required=["a"], optional=[])["state"] == "PENDING"
    assert evaluate_checks([_check("a")], required=["a", "b"], optional=[])["state"] == "PENDING"
    assert evaluate_checks([_check("a", status="in_progress", conclusion=None)], required=[], optional=[])["state"] == "PENDING"
    assert evaluate_checks([_check("a", status="queued", conclusion=None), _check("b", conclusion="failure")], required=[], optional=[])["state"] == "FAIL"


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", "stale", "startup_failure"])
def test_non_success_conclusions_fail(conclusion):
    assert evaluate_checks([_check("a", conclusion=conclusion)], required=["a"], optional=[])["state"] == "FAIL"


def test_skipped_or_neutral_passes_only_for_an_optional_check():
    assert evaluate_checks([_check("a"), _check("lint", conclusion="skipped")], required=[], optional=[])["state"] == "FAIL"
    assert evaluate_checks([_check("a"), _check("lint", conclusion="skipped")], required=[], optional=["lint"])["state"] == "PASS"
    assert evaluate_checks([_check("a"), _check("x", conclusion="failure")], required=["a"], optional=[])["state"] == "PASS"  # x is not required


def _base(direct, transitive, *, branch="dep-branch", ancestry=True):
    return select_base(direct=direct, transitive=set(transitive), target="main", integration_branch="dridh/integration/f",
                       predecessor_branch=lambda dep: branch, contains_ancestry=lambda ref, needed: ancestry)["base"]


def test_pr_base_selection_is_deterministic():
    assert _base([], []) == "main"
    assert _base(["A"], ["A"]) == "dep-branch"
    assert _base(["A"], ["A", "Z"], ancestry=False) == "dridh/integration/f"
    assert _base(["A"], ["A"], branch=None) == "dridh/integration/f"
    assert _base(["A", "B"], ["A", "B"]) == "dridh/integration/f"


def test_remote_urls_are_parsed_and_non_github_remotes_refused():
    assert parse_remote("git@github.com:acme/app.git\n") == ("acme", "app")
    assert parse_remote("https://github.com/acme/app") == ("acme", "app")
    with pytest.raises(DridhError):
        parse_remote("https://example.com/acme/app.git")


def test_gh_adapter_uses_rest_endpoints_and_parses_results():
    calls = []

    def runner(argv, stdin):
        calls.append((argv, stdin))
        path = argv[3]
        if path.endswith("/pulls"):
            return json.dumps({"number": 7, "html_url": "https://github.com/o/r/pull/7", "head": {"sha": "h"}, "base": {"sha": "b"}})
        if "/pulls/7" in path:
            return json.dumps({"state": "closed", "merged": True, "head": {"sha": "h"}, "base": {"ref": "main"}})
        return json.dumps({"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success", "html_url": "u", "id": 3}]})

    gh = GhCli("o", "r", runner)
    assert gh.create_pr(title="t", body="b", head="x", base="main")["number"] == 7
    assert json.loads(calls[0][1])["head"] == "x" and calls[0][0][:3] == ["api", "-X", "POST"]
    assert gh.pr_view(7) == {"state": "MERGED", "head_sha": "h", "base_ref": "main", "merged": True}
    assert gh.check_runs("abc")[0]["name"] == "ci" and "graphql" not in " ".join(calls[2][0]).lower()


def test_delivery_is_off_by_default_and_cannot_run_without_a_terminal(tmp_path):
    import subprocess
    import sys

    from tests.e2e.planner_test_helpers import seed_approved_baseline

    info, _ = seed_approved_baseline(tmp_path / "project")
    done = subprocess.run([sys.executable, "-m", "dridh", "deliver", "run", "nothing", "--project", str(info.project_root)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    assert done.returncode != 0 and "Delivery is disabled" in (done.stdout + done.stderr)
    from dridh.common.protocol import ALLOWED_OPERATIONS

    assert not [op for op in ALLOWED_OPERATIONS if "deliver" in op or "push" in op or "pull_request" in op]


def _fixture(name):
    import json
    from pathlib import Path

    return json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "github" / name).read_text())


def test_real_github_replies_are_parsed_and_evaluated():
    """Fixtures are trimmed copies of real replies from a throwaway repository (job reported once per triggering event)."""
    calls = []

    def runner(argv, stdin):
        calls.append(argv)
        path = argv[3]
        if path.endswith("/pulls/1"):
            return json.dumps(_fixture("pull.json"))
        return json.dumps(_fixture("check_runs_pass.json" if "pass" in runner.mode else "check_runs_fail.json"))

    gh = GhCli("o", "r", runner)
    runner.mode = "pass"
    view = gh.pr_view(1)
    assert view["state"] == "CLOSED" and len(view["head_sha"]) == 40 and view["base_ref"] == "main"
    passing = gh.check_runs("abc")
    assert len(passing) == 4 and {c["name"] for c in passing} == {"build", "gate"}  # duplicates are real
    assert evaluate_checks(passing, required=[], optional=[])["state"] == "PASS"
    runner.mode = "fail"
    failing = gh.check_runs("abc")
    assert evaluate_checks(failing, required=[], optional=[])["state"] == "FAIL"
    assert evaluate_checks(failing, required=["build"], optional=[])["state"] == "PASS"  # only the required name counts


@pytest.mark.parametrize("order", [("failure", "success"), ("success", "failure")])
def test_a_failing_duplicate_is_never_masked_by_a_passing_one(order):
    checks = [{"name": "gate", "status": "completed", "conclusion": value} for value in order]
    result = evaluate_checks(checks, required=[], optional=[])
    assert result["state"] == "FAIL" and "gate" in result["reasons"][0]


def test_a_pending_duplicate_keeps_the_gate_pending():
    checks = [{"name": "ci", "status": "completed", "conclusion": "success"}, {"name": "ci", "status": "in_progress", "conclusion": None}]
    assert evaluate_checks(checks, required=[], optional=[])["state"] == "PENDING"
