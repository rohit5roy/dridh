"""`dridh upgrade` updates only what Dridh owns, keeps user configuration and is idempotent (plan section 2.7)."""

from __future__ import annotations

import json
from pathlib import Path

import dridh
from tests.integration.test_cli import run_module


def _install(tmp_path: Path) -> Path:
    config = tmp_path / "cfg"
    config.mkdir()
    # A pre-existing user configuration and a user-owned agent that Dridh must never touch.
    (config / "opencode.json").write_text(json.dumps({"model": "my/model", "default_agent": "build", "mcp": {"x": {"url": "u"}}}))
    (config / "agents").mkdir()
    (config / "agents" / "mine.md").write_text("my own agent\n")
    assert run_module("install", "--config-dir", str(config)).returncode == 0
    return config


def _manifest(config: Path) -> dict:
    return json.loads((config / ".dridh" / "install-manifest.json").read_text()) if (config / ".dridh" / "install-manifest.json").is_file() else _find_manifest(config)


def _find_manifest(config: Path) -> dict:
    candidates = [p for p in config.rglob("*manifest*.json")]
    assert candidates, "no install manifest found"
    return json.loads(candidates[0].read_text())


def _manifest_path(config: Path) -> Path:
    return next(config.rglob("*manifest*.json"))


def _upgrade(config: Path, *extra: str):
    return run_module("upgrade", "--config-dir", str(config), *extra)


def test_upgrade_requires_an_existing_install_and_creates_nothing(tmp_path):
    config = tmp_path / "cfg"
    result = _upgrade(config)
    assert result.returncode == 3
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "NOT_INSTALLED" and "dridh install" in error["recovery"]
    assert not config.exists()


def test_upgrade_of_a_current_install_is_a_no_op_and_idempotent(tmp_path):
    config = _install(tmp_path)
    before = {p: p.read_bytes() for p in config.rglob("*") if p.is_file() and "manifest" not in p.name}
    first = _upgrade(config)
    assert first.returncode == 0, first.stderr
    document = json.loads(first.stdout)
    assert document["status"] == "unchanged" and document["from_version"] == document["to_version"]
    second = _upgrade(config)
    assert json.loads(second.stdout)["status"] == "unchanged"
    assert {p: p.read_bytes() for p in config.rglob("*") if p.is_file() and "manifest" not in p.name} == before


def test_upgrade_restores_missing_managed_files_and_keeps_the_users_own_configuration(tmp_path):
    config = _install(tmp_path)
    (config / "agents" / "dridh" / "planner.md").unlink()
    manifest_path = _manifest_path(config)
    manifest = json.loads(manifest_path.read_text())
    manifest["dridh_version"] = "0.0.1"  # as if installed by an older release
    manifest_path.write_text(json.dumps(manifest))

    result = _upgrade(config)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["from_version"] == "0.0.1" and document["to_version"] == dridh.__version__ and document["status"] == "changed"
    assert (config / "agents" / "dridh" / "planner.md").is_file()
    assert (config / "agents" / "mine.md").read_text() == "my own agent\n"
    user_config = json.loads((config / "opencode.json").read_text())
    assert user_config["model"] == "my/model" and user_config["default_agent"] == "build" and user_config["mcp"] == {"x": {"url": "u"}}
    assert json.loads(_manifest_path(config).read_text())["dridh_version"] == dridh.__version__


def test_a_modified_managed_file_blocks_the_upgrade_until_forced_and_is_backed_up(tmp_path):
    config = _install(tmp_path)
    target = config / "agents" / "dridh" / "coordinator.md"
    target.write_text("I edited this\n")

    blocked = _upgrade(config)
    assert blocked.returncode != 0 and "not manage" in blocked.stderr
    assert target.read_text() == "I edited this\n"

    forced = _upgrade(config, "--force")
    assert forced.returncode == 0, forced.stderr
    backup = json.loads(forced.stdout)["backup"]
    assert backup and (config / backup / "files" / "agents" / "dridh" / "coordinator.md").read_text() == "I edited this\n"
    assert "I edited this" not in target.read_text()


def test_dry_run_changes_nothing(tmp_path):
    config = _install(tmp_path)
    (config / "agents" / "dridh" / "planner.md").unlink()
    result = _upgrade(config, "--dry-run")
    assert json.loads(result.stdout)["mode"] == "dry-run" and json.loads(result.stdout)["changed"] is True
    assert not (config / "agents" / "dridh" / "planner.md").exists()


def test_manifest_records_provenance_required_by_the_plan(tmp_path):
    manifest = json.loads(_manifest_path(_install(tmp_path)).read_text())
    shipped = json.loads((Path(__file__).resolve().parents[2] / "opencode" / "plugins" / "dridh-bridge" / "package.json").read_text())
    assert manifest["plugin_sdk_version"] == shipped["dependencies"]["@opencode/plugin"] and manifest["plugin_package"] == "dridh-bridge" and manifest["plugin_package_version"]
    assert manifest["python_executable"] and manifest["python_version"].startswith("3.13")
    assert manifest["owned_config_entries"] and manifest["files"]
