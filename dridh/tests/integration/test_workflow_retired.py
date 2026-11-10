"""The generic workflow planner is retired: `dridh workflow create/plan` are refused and hidden (dridh.orchestration.legacy)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _dridh(args: list[str], cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if k != "DRIDH_LEGACY_WORKFLOW"}
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, cwd=cwd, timeout=120, env={**clean, **env})


def test_workflow_create_and_plan_are_refused_and_hidden(tmp_path: Path) -> None:
    assert _dridh(["init", "--mode", "new"], tmp_path).returncode == 0
    for args in (["workflow", "create", "Build a greeting"], ["workflow", "plan", "wf-0123456789ab"]):
        refused = _dridh(args, tmp_path)
        assert refused.returncode == 2, refused.stderr
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert error["code"] == "CONFIGURATION_ERROR" and "retired" in error["message"]
    assert " workflow " not in _dridh(["--help"], tmp_path).stdout
    created = _dridh(["workflow", "create", "Build a greeting"], tmp_path, DRIDH_LEGACY_WORKFLOW="1")
    assert created.returncode == 0 and json.loads(created.stdout)["state"] == "CREATED"  # the test-only switch still works
