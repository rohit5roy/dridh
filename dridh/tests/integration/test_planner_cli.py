"""Phase 13 Planner CLI command-surface integration tests."""

from __future__ import annotations

from tests.integration.test_cli import run_module


def test_planner_help_exposes_resumable_lifecycle_commands(tmp_path) -> None:
    result = run_module("planner", "--help", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    for command in ("start", "clarify", "status", "generate", "sync", "gate"):
        assert command in result.stdout
