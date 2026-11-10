"""CLI polish: workspace discovery and --workspace, text vs JSON output, and `dridh start`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dridh.cli.render import render, render_error

FAKE_OPENCODE = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "opencode v2.2.5"; exit 0; fi
printf '%s\\n' "$OPENCODE_CONFIG_CONTENT" > "$DRIDH_FAKE_LOG"
exit 0
"""


def _dridh(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "dridh", *args], capture_output=True, text=True, cwd=cwd, timeout=120,
                          env={**os.environ, **(env or {})})


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    assert _dridh(["init", "--mode", "existing"], cwd=root).returncode == 0
    assert _dridh(["feature", "create", "Order cancellation"], cwd=root).returncode == 0
    return root


def test_commands_find_the_workspace_from_a_subdirectory_and_accept_workspace(project: Path, tmp_path: Path) -> None:
    listed = _dridh(["feature", "list"], cwd=project / "src" / "deep")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["features"][0]["slug"] == "order-cancellation"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for flag in ("--workspace", "--project"):
        result = _dridh(["feature", "list", flag, str(project)], cwd=elsewhere)
        assert result.returncode == 0 and json.loads(result.stdout)["features"], result.stderr
    missing = _dridh(["feature", "list"], cwd=elsewhere)
    assert missing.returncode != 0 and "WORKSPACE_NOT_FOUND" in missing.stderr


def test_output_is_json_when_piped_and_text_when_asked(project: Path) -> None:
    piped = _dridh(["feature", "status"], cwd=project)
    assert json.loads(piped.stdout)["feature"]["stage"] == "INITIALISED"
    text = _dridh(["feature", "status"], cwd=project, env={"DRIDH_OUTPUT": "text"})
    assert text.stdout.startswith("Feature: Order cancellation (order-cancellation)") and "Stage: INITIALISED" in text.stdout
    forced = _dridh(["feature", "status", "--json"], cwd=project, env={"DRIDH_OUTPUT": "text"})
    assert json.loads(forced.stdout)["feature"]["slug"] == "order-cancellation"
    forced_first = _dridh(["--json", "feature", "list"], cwd=project, env={"DRIDH_OUTPUT": "text"})
    assert json.loads(forced_first.stdout)["features"]
    # A command with its own --json keeps it.
    doctor = _dridh(["doctor", "--json"], cwd=project, env={"DRIDH_OUTPUT": "text"})
    assert "checks" in json.loads(doctor.stdout)


def test_generic_and_error_rendering() -> None:
    text = render({"run_id": "r1", "workspace_id": "hidden", "stories": [{"story_id": "ST-1", "status": "DONE"}], "ok": True, "empty": []})
    assert "run id: r1" in text and "story id: ST-1, status: DONE" in text and "ok: yes" in text and "empty: none" in text
    assert "hidden" not in text
    assert render({"unexpected": 1}, "feature-status").startswith("Feature:")  # a view tolerates missing keys
    error = render_error({"code": "VALIDATION_FAILED", "message": "Unknown feature 'x'.", "details": ["a"], "recovery": "List features."})
    assert error.splitlines() == ["Error (VALIDATION_FAILED): Unknown feature 'x'.", "  - a", "What to do: List features."]


def _installed_config(tmp_path: Path) -> Path:
    config = tmp_path / "opencode-config"
    config.mkdir()
    (config / "opencode.json").write_text("{}\n")
    assert _dridh(["install", "--config-dir", str(config)], cwd=tmp_path).returncode == 0
    return config


def test_start_dry_run_selects_the_coordinator_without_touching_the_global_config(project: Path, tmp_path: Path) -> None:
    config = _installed_config(tmp_path)
    fake = tmp_path / "opencode"
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(0o755)
    before = (config / "opencode.json").read_text()
    result = _dridh(["start", "--dry-run", "--config-dir", str(config), "--prompt", "Add cancellation"], cwd=project / "src",
                    env={"DRIDH_OPENCODE_BIN": str(fake), "OPENCODE_CONFIG_CONTENT": json.dumps({"theme": "dark"})})
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["workspace"] == str(project.resolve()) and report["agent"] == "dridh/coordinator"
    assert report["argv"] == [str(fake), str(project.resolve()), "--prompt", "Add cancellation"]
    assert report["features"] == [{"slug": "order-cancellation", "stage": "INITIALISED", "status": "ACTIVE"}]
    assert (config / "opencode.json").read_text() == before and "default_agent" not in before


def test_start_refuses_without_a_terminal_a_workspace_or_an_installation(project: Path, tmp_path: Path) -> None:
    config = _installed_config(tmp_path)
    fake = tmp_path / "opencode"
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(0o755)
    env = {"DRIDH_OPENCODE_BIN": str(fake), "DRIDH_FAKE_LOG": str(tmp_path / "launched")}
    no_tty = _dridh(["start", "--config-dir", str(config)], cwd=project, env=env)
    assert no_tty.returncode != 0 and "needs a terminal" in no_tty.stderr and not (tmp_path / "launched").exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    no_workspace = _dridh(["start", "--dry-run", "--config-dir", str(config)], cwd=empty, env=env)
    assert no_workspace.returncode == 3 and "WORKSPACE_NOT_FOUND" in no_workspace.stderr
    bare = tmp_path / "bare-config"
    bare.mkdir()
    not_installed = _dridh(["start", "--dry-run", "--config-dir", str(bare)], cwd=project, env=env)
    assert not_installed.returncode == 3 and "dridh install" in not_installed.stderr


def test_start_launches_opencode_with_the_coordinator_layered_over_existing_content(project: Path, tmp_path: Path) -> None:
    pty = pytest.importorskip("pty")
    config = _installed_config(tmp_path)
    fake = tmp_path / "opencode"
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(0o755)
    log = tmp_path / "launched"
    env = {**os.environ, "DRIDH_OPENCODE_BIN": str(fake), "DRIDH_FAKE_LOG": str(log), "OPENCODE_CONFIG_CONTENT": json.dumps({"theme": "dark"})}
    pid, fd = pty.fork()
    if pid == 0:  # child: stdin/stdout are the pseudo-terminal
        os.chdir(project)
        os.execve(sys.executable, [sys.executable, "-m", "dridh", "start", "--config-dir", str(config)], env)
    output = b""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors="replace")
    assert json.loads(log.read_text()) == {"default_agent": "dridh/coordinator", "theme": "dark"}
    assert b"dridh/coordinator selected" in output
