"""Phase 11 Ideator commands use the durable session and terminal approval boundary."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dridh", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def make_workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project


def test_start_answer_status_and_parity_commands(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    started = run("ideator", "start", "Build an order intake service", "--existing-context", "Existing order flow is manual.", "--project", str(project), cwd=project)
    assert started.returncode == 0, started.stderr
    session_id = json.loads(started.stdout)["session_id"]

    answered = run(
        "ideator", "answer", session_id,
        "--topic", "problem",
        "--question", "What problem needs to be solved?",
        "--answer", "Order intake is manual.",
        "--source", "Product owner",
        "--project", str(project),
        cwd=project,
    )
    assert answered.returncode == 0, answered.stderr
    status = json.loads(run("ideator", "status", session_id, "--project", str(project), cwd=project).stdout)
    assert status["questions_recorded"] == 1
    assert "problem" not in status["topics_missing"]
    parity = run("ideator", "parity", session_id, "--project", str(project), cwd=project)
    assert parity.returncode == 0, parity.stderr
    assert json.loads(parity.stdout)["ok"] is True
    gate = json.loads(run("ideator", "gate", "--session", session_id, "--project", str(project), cwd=project).stdout)
    assert gate["unlocked"] is False


def test_empty_answer_requires_explicit_unresolved_disposition(tmp_path: Path) -> None:
    project = make_workspace(tmp_path)
    started = run("ideator", "start", "A small idea", "--project", str(project), cwd=project)
    session_id = json.loads(started.stdout)["session_id"]
    refused = run(
        "ideator", "answer", session_id,
        "--topic", "problem",
        "--question", "What is the problem?",
        "--answer", "",
        "--project", str(project),
        cwd=project,
    )
    assert refused.returncode == 4
    assert json.loads(refused.stderr)["error"]["code"] == "VALIDATION_FAILED"
