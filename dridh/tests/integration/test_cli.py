from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import dridh

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def run_module(*args: str, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "dridh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(cwd or SOURCE_ROOT),
        env=env,
    )


def test_help_works_without_heavy_dependencies() -> None:
    result = run_module("--help")
    assert result.returncode == 0
    assert "Usage" in result.stdout
    assert "bridge" in result.stdout
    assert "fastembed" not in result.stdout.lower()


def test_version_reports_package_python_protocol_and_location() -> None:
    result = run_module("--version")
    assert result.returncode == 0
    assert f"dridh {dridh.__version__}" in result.stdout
    assert "Python: 3.13" in result.stdout
    assert "Bridge protocol: 1" in result.stdout
    assert "Runtime location:" in result.stdout
    assert result.stderr == ""


def test_lightweight_imports_do_not_load_heavy_modules() -> None:
    code = (
        "import sys, json; "
        "import dridh, dridh.cli.main, dridh.common.protocol, dridh.common.bridge; "
        "print(json.dumps(sorted(m for m in ('fastembed','onnxruntime','tree_sitter') if m in sys.modules)))"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(SOURCE_ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
