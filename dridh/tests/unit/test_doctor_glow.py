"""`dridh doctor` reports glow (TUI document rendering) as optional: WARN when absent, PASS with its version."""

from __future__ import annotations

import subprocess

from dridh.diagnostics import PASS, WARN, check_glow


def _runner(stdout: str):  # type: ignore[no-untyped-def]
    return lambda argv: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_missing_glow_is_a_warning_with_a_remedy(monkeypatch) -> None:
    monkeypatch.setattr("dridh.diagnostics.shutil.which", lambda name: None)
    check = check_glow({}, _runner(""))
    assert check.status == WARN and "raw Markdown" in check.detail and "install.sh" in check.remedy


def test_glow_on_path_or_named_by_dridh_glow_passes() -> None:
    check = check_glow({"DRIDH_GLOW": "/opt/glow"}, _runner("glow version 2.1.1 (d37e988)\n"))
    assert check.status == PASS and check.detail == "glow version 2.1.1 (d37e988)"
