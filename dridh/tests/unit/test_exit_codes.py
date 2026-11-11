"""Stable exit codes, one per error category, applied at the top level."""

from __future__ import annotations

import subprocess
import sys

from dridh.cli import exit_codes
from dridh.common.errors import ErrorCode


def test_every_error_code_has_a_documented_exit_code():
    assert set(exit_codes.EXIT_CODE_BY_ERROR) == set(ErrorCode)
    assert set(exit_codes.EXIT_CODE_BY_ERROR.values()) <= {2, 3, 4, 5, 6, 7, 8}


def _run(*args, stdin=subprocess.DEVNULL):
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, stdin=stdin, check=False)


def test_a_missing_workspace_is_a_missing_prerequisite(tmp_path):
    assert _run("feature", "list", "--project", str(tmp_path)).returncode == exit_codes.PREREQUISITE_MISSING


def test_confirmation_without_a_terminal_is_approval_required(monkeypatch):
    import io

    import pytest

    from dridh.cli.main import _confirm
    from dridh.common.errors import DridhError

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(DridhError) as caught:
        _confirm("APPROVE X", "summary")
    assert caught.value.code == ErrorCode.AUTHORIZATION_DENIED
    assert exit_codes.exit_code_for(caught.value.code) == exit_codes.APPROVAL_REQUIRED == 5


def test_unknown_feature_is_a_validation_failure(tmp_path):
    from tests.e2e.planner_test_helpers import seed_approved_baseline

    info, _ = seed_approved_baseline(tmp_path / "project")
    assert _run("feature", "status", "ghost", "--project", str(info.project_root)).returncode == exit_codes.VALIDATION_FAILED
