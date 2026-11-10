"""`dridh doctor` is read-only, evidence-based and exits 3 only when a mandatory prerequisite is missing."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from dridh import diagnostics
from tests.integration.test_cli import SOURCE_ROOT, run_module

SECRET = "sk-or-v1-" + "d" * 40


def _fake_opencode(tmp_path: Path, version: str = "opencode v2.2.5", code: int = 0) -> Path:
    script = tmp_path / "fake-opencode"
    script.write_text(f"#!/bin/sh\necho '{version}'\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _doctor(*args: str, opencode: Path | None = None, extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT / "src")
    env.pop("DRIDH_OPENCODE_BIN", None)
    if opencode is not None:
        env["DRIDH_OPENCODE_BIN"] = str(opencode)
    env.update(extra_env or {})
    return subprocess.run([os.sys.executable, "-m", "dridh", "doctor", *args], capture_output=True, text=True, timeout=60, env=env)


def _by_id(result) -> dict[str, dict]:
    return {item["id"]: item for item in json.loads(result.stdout)["checks"]}


def _installed(tmp_path: Path) -> Path:
    config = tmp_path / "cfg"
    assert run_module("install", "--config-dir", str(config)).returncode == 0
    return config


def test_missing_prerequisites_exit_3_with_remedies_and_valid_json(tmp_path):
    result = _doctor("--json", "--config-dir", str(tmp_path / "cfg"))
    assert result.returncode == 3
    document = json.loads(result.stdout)
    checks = _by_id(result)
    assert document["ok"] is False and document["counts"]["FAIL"] >= 1
    assert checks["runtime"]["status"] == "FAIL" and "dridh install" in checks["runtime"]["remedy"]
    assert all(item["remedy"] for item in checks.values() if item["status"] == "FAIL")


def test_healthy_install_passes_and_warnings_do_not_fail_the_exit_code(tmp_path):
    config = _installed(tmp_path)
    result = _doctor("--json", "--config-dir", str(config), opencode=_fake_opencode(tmp_path))
    assert result.returncode == 0, result.stdout
    checks = _by_id(result)
    assert checks["opencode"]["status"] == "PASS" and checks["runtime"]["status"] == "PASS"
    assert checks["default-agent"]["status"] == "PASS"
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize(("version", "code"), [("opencode v1.9.0", 0), ("opencode v2.2.5", 1), ("", 0), ("opencode v3.0.0", 0)])
def test_opencode_must_be_v2_and_runnable(tmp_path, version, code):
    check = diagnostics.check_opencode({"DRIDH_OPENCODE_BIN": str(_fake_opencode(tmp_path, version, code))}, diagnostics._run)
    assert check.status == "FAIL" and "OpenCode V2" in (check.detail + check.remedy)


@pytest.mark.parametrize("version", ["opencode v2.2.5", "opencode v2.2.6", "opencode v2.3.0", "opencode v2.10.1"])
def test_opencode_2_2_5_or_any_later_v2_release_passes(tmp_path, version):
    check = diagnostics.check_opencode({"DRIDH_OPENCODE_BIN": str(_fake_opencode(tmp_path, version, 0))}, diagnostics._run)
    assert check.status == "PASS", check.detail


@pytest.mark.parametrize("version", ["opencode v2.2.4", "opencode v2.1.9", "opencode v2.0.0"])
def test_opencode_older_than_2_2_5_fails_with_an_upgrade_remedy(tmp_path, version):
    check = diagnostics.check_opencode({"DRIDH_OPENCODE_BIN": str(_fake_opencode(tmp_path, version, 0))}, diagnostics._run)
    assert check.status == "FAIL" and "2.2.5 or later" in check.detail and "npm install -g @opencode/cli" in check.remedy


def test_a_modified_managed_file_is_reported_with_the_upgrade_remedy(tmp_path):
    config = _installed(tmp_path)
    (config / "agents" / "dridh" / "coordinator.md").write_text("tampered\n")
    result = _doctor("--json", "--config-dir", str(config), opencode=_fake_opencode(tmp_path))
    runtime = _by_id(result)["runtime"]
    assert result.returncode == 3 and runtime["status"] == "FAIL"
    assert "coordinator.md (modified)" in runtime["detail"] and "dridh upgrade" in runtime["remedy"]


def test_the_coordinator_as_global_default_agent_is_a_failure(tmp_path):
    config = _installed(tmp_path)
    config_file = config / "opencode.json"
    document = json.loads(config_file.read_text())
    document["default_agent"] = "dridh/coordinator"
    config_file.write_text(json.dumps(document))
    result = _doctor("--json", "--config-dir", str(config), opencode=_fake_opencode(tmp_path))
    assert result.returncode == 3 and _by_id(result)["default-agent"]["status"] == "FAIL"


def test_workspace_states(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = _installed(tmp_path)
    args = ("--json", "--config-dir", str(config), "--project", str(project))
    opencode = _fake_opencode(tmp_path)

    assert _by_id(_doctor(*args, opencode=opencode))["workspace"]["status"] == "WARN"
    assert not (project / ".opencode").exists()  # doctor never creates a workspace

    assert run_module("init", "--project", str(project), "--mode", "new", stdin="").returncode == 0
    assert _by_id(_doctor(*args, opencode=opencode))["workspace"]["status"] == "PASS"

    database = project / ".opencode" / "dridh" / "state" / "dridh.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    connection.commit()
    connection.close()
    newer = _doctor(*args, opencode=opencode)
    assert newer.returncode == 3 and _by_id(newer)["workspace"]["status"] == "FAIL" and "999" in _by_id(newer)["workspace"]["detail"]

    database.write_bytes(b"this is not a database" * 50)
    broken = _doctor(*args, opencode=opencode)
    assert broken.returncode == 3 and _by_id(broken)["workspace"]["status"] == "FAIL"


def test_doctor_is_read_only(tmp_path):
    config = tmp_path / "cfg-never-created"
    project = tmp_path / "project"
    project.mkdir()
    opencode = _fake_opencode(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    _doctor("--config-dir", str(config), "--project", str(project), opencode=opencode)
    assert not config.exists() and not (project / ".opencode").exists()
    assert sorted(tmp_path.rglob("*")) == before  # nothing created, nothing modified


def test_credential_names_are_reported_but_never_values(tmp_path):
    config = _installed(tmp_path)
    result = _doctor("--config-dir", str(config), opencode=_fake_opencode(tmp_path), extra_env={"OPENROUTER_API_KEY": SECRET})
    assert "OPENROUTER_API_KEY" in result.stdout and SECRET not in result.stdout + result.stderr
    as_json = _doctor("--json", "--config-dir", str(config), opencode=_fake_opencode(tmp_path), extra_env={"OPENROUTER_API_KEY": SECRET})
    assert SECRET not in as_json.stdout + as_json.stderr


def test_human_output_marks_status_and_gives_remedies(tmp_path):
    result = _doctor("--config-dir", str(tmp_path / "cfg"))
    assert "[FAIL]" in result.stdout and "->" in result.stdout and "failed" in result.stdout.splitlines()[-1]


def test_doctor_stays_lightweight():
    code = ("import sys, dridh.diagnostics; "
            "print(sorted(m for m in ('fastembed','onnxruntime','tree_sitter','sqlite_vec') if m in sys.modules))")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT / "src")
    out = subprocess.run([os.sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert out.stdout.strip() == "[]", out.stderr
