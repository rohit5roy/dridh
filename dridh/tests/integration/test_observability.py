"""`dridh observe`: metrics come from persisted state and never contain prompt, answer or document text."""

from __future__ import annotations

import json
import uuid

import pytest

from dridh.coordinator.lifecycle import FeatureLifecycle
from dridh.observability.metrics import SECTIONS, collect, render
from tests.e2e.planner_test_helpers import seed_approved_baseline
from tests.integration.test_cli import run_module


@pytest.fixture()
def info(tmp_path):
    workspace, _ = seed_approved_baseline(tmp_path / "project")
    return workspace


def _bridge(info, operation, payload):
    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": operation, "workspace_root": str(info.project_root),
               "payload": payload, "caller": {"source": "plugin", "agent": "dridh/coordinator", "session_id": "ses_obs_0001"}}
    return json.loads(run_module("bridge", stdin=json.dumps(request)).stdout)


def test_report_has_every_section_and_reflects_real_activity(info):
    FeatureLifecycle(info).create_feature("Order Intake", actor="t")
    _bridge(info, "coordinator.status", {})
    _bridge(info, "coordinator.query", {"kind": "nonsense"})
    report = collect(info)
    assert set(SECTIONS) <= set(report) and report["schema_version"] >= 14
    assert report["lifecycle"][0]["slug"] == "order-intake" and report["lifecycle"][0]["stage"] == "INITIALISED"
    assert report["bridge"]["sampled"] == 2 and report["bridge"]["failed"] == 1 and "VALIDATION_FAILED" in report["bridge"]["error_codes"]
    assert report["bridge"]["latency_ms"]["p95"] is not None
    assert report["reviews"]["approved"] >= 2 and report["code_reacher"] == {"indexed": False}


def test_a_section_that_cannot_be_computed_is_reported_not_fatal(info, monkeypatch):
    import sqlite3

    def broken(connection, workspace_id, now):
        raise sqlite3.OperationalError("no such table: tool_invocation")

    monkeypatch.setitem(SECTIONS, "bridge", broken)
    report = collect(info)
    assert "no such table" in report["bridge"]["unavailable"] and report["lifecycle"] is not None
    assert "unavailable" in render(report)


def test_the_report_never_contains_bodies_or_secrets(info):
    secret = "sk" + "-" + "canary" + "-value-1234567890"  # built at runtime: a fake key that no scanner should see in source
    _bridge(info, "coordinator.validate", {"kind": "use-cases", "path": secret})
    text = json.dumps(collect(info), default=str) + render(collect(info))
    assert secret not in text


def test_cli_prints_a_summary_and_json(info):
    summary = run_module("observe", "--project", str(info.project_root))
    assert summary.returncode == 0 and "Lifecycle" in summary.stdout and "Bridge" in summary.stdout
    parsed = json.loads(run_module("observe", "--json", "--project", str(info.project_root)).stdout)
    assert parsed["workspace"]["id"] == info.workspace_id


def test_a_stale_code_index_is_flagged(info):
    import subprocess

    from dridh.code_reacher.indexer import CodeIndexer

    root = info.project_root
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / ".gitignore").write_text(".opencode/\n")
    (root / "shop.py").write_text("def tax(x):\n    return x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
    CodeIndexer(info).index(embed=False)
    assert collect(info)["code_reacher"]["stale"] is False
    (root / "shop.py").write_text("def tax(x):\n    return x * 2\n")
    section = collect(info)["code_reacher"]
    assert section["stale"] is True and "dridh index" in section["recovery"]


def test_usage_is_reported_from_the_usage_log(info):
    from dridh.observability.usage import record_usage

    assert collect(info)["usage"]["recorded"] is False and "no agent runs recorded yet" in render(collect(info))
    record_usage(info.project_root, agent="dridh/executor", model="claude-agent/haiku#low", session_id="ses_obs_0001", title="Dridh Executor ST-1.1",
                 outcome="OK", duration_seconds=2.0, usage={"steps": 2, "input": 1200, "output": 300, "reasoning": 0, "cache_read": 5000, "cache_write": 0, "cost": 0.0123},
                 resumed=False)
    usage = collect(info)["usage"]
    assert usage["invocations"] == 1 and usage["total_tokens"] == 6500 and usage["by_stage"]["executor"]["cost"] == 0.0123
    text = render(collect(info))
    assert "6,500 tokens" in text and "claude-agent/haiku#low" in text and "$0.0123" in text
