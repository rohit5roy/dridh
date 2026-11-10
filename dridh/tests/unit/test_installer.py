"""Installer and configuration merge: dry run, safe merge, backup, conflicts and rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.install.installer import (
    EXECUTABLE_FILE,
    PLUGIN_NAME,
    apply_install,
    apply_uninstall,
    plan_install,
    plan_uninstall,
    resolve_layout,
    status,
)


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    plugin = tmp_path / "src-plugin"
    (plugin / "dist").mkdir(parents=True)
    (plugin / "package.json").write_text(json.dumps({"name": "dridh-bridge", "exports": {".": "./dist/index.js"}}))
    (plugin / "dist" / "index.js").write_text("export default {}\n")
    (plugin / "dist" / "bridge-client.js").write_text("export {}\n")
    agents = tmp_path / "src-agents"
    agents.mkdir()
    (agents / "coordinator.md").write_text("---\ndescription: coordinator\nmode: primary\n---\nBody\n")
    (agents / "planner.md").write_text("---\ndescription: planner\nmode: subagent\n---\nBody\n")
    return plugin, agents


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def install(config: Path, sources: tuple[Path, Path], **kwargs):
    layout = resolve_layout(config)
    plan = plan_install(layout, plugin_source=sources[0], agent_source=sources[1], **kwargs)
    return layout, plan


def test_dry_run_writes_nothing(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "opencode.json").write_text('{"model": "x/y"}\n')
    before = snapshot(config)
    _, plan = install(config, sources)
    assert plan.changes
    assert snapshot(config) == before


def test_fresh_install_creates_managed_layout(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    layout, plan = install(config, sources)
    assert plan.config_action == "create"
    result = apply_install(plan)
    assert result["status"] == "changed"
    assert (config / "agents" / "dridh" / "coordinator.md").is_file()
    assert (config / "dridh" / "plugin" / "dridh-bridge" / "dist" / "index.js").is_file()
    document = json.loads((config / "opencode.json").read_text())
    assert document["$schema"] == "https://opencode.ai/config.json"
    assert document["plugins"] == [str(layout.plugin_dir)]
    assert "default_agent" not in document
    current = status(layout)
    assert current["installed"] and current["plugin_entry_present"]
    assert set(current["files"].values()) == {"ok"}
    assert current["default_agent"] is None


def test_nested_worker_profiles_are_installed_and_idempotent(tmp_path: Path, sources) -> None:
    plugin, agents = sources
    workers = agents / "workers"
    workers.mkdir()
    expected = (
        "designer-inquirer.md",
        "designer-doc-generator.md",
        "designer-handoff.md",
        "ideator-inquirer.md",
        "ideator-doc-generator.md",
        "ideator-handoff.md",
        "planner-indexer.md",
        "planner-component.md",
        "planner-testing.md",
    )
    for name in expected:
        (workers / name).write_text(
            f"---\\ndescription: {name}\\nmode: subagent\\n---\\nWorker body\\n",
            encoding="utf-8",
        )

    layout = resolve_layout(tmp_path / "config")
    first = apply_install(plan_install(layout, plugin_source=plugin, agent_source=agents))
    assert first["status"] == "changed"
    for name in expected:
        installed = layout.agents_dir / "workers" / name
        assert installed.is_file()
        assert "Worker body" in installed.read_text(encoding="utf-8")

    second_plan = plan_install(layout, plugin_source=plugin, agent_source=agents)
    assert second_plan.to_dict()["conflicts"] == []
    assert second_plan.changes == []
    assert apply_install(second_plan)["status"] == "unchanged"


def test_second_install_is_idempotent(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    layout, plan = install(config, sources)
    apply_install(plan)
    before = snapshot(config)
    again = plan_install(layout, plugin_source=sources[0], agent_source=sources[1])
    assert again.changes == []
    assert apply_install(again)["status"] == "unchanged"
    assert snapshot(config) == before


def test_existing_configuration_is_preserved(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    original = {
        "$schema": "https://opencode.ai/config.json",
        "model": "provider/model",
        "providers": {"acme": {"settings": {"baseURL": "https://llm.example/v1"}}},
        "x_custom": {"keep": [1, 2, 3]},
        "plugins": ["other-plugin"],
    }
    (config / "opencode.json").write_text(json.dumps(original, indent=2) + "\n")
    layout, plan = install(config, sources)
    apply_install(plan)
    updated = json.loads((config / "opencode.json").read_text())
    assert list(updated) == list(original)
    assert updated["providers"] == original["providers"]
    assert updated["x_custom"] == original["x_custom"]
    assert updated["plugins"] == ["other-plugin", str(layout.plugin_dir)]


def test_comments_require_explicit_stripping_and_are_backed_up(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    text = '{\n  // user preference\n  "model": "p/m", // trailing comment\n}\n'
    (config / "opencode.jsonc").write_text(text)
    with pytest.raises(DridhError) as refused:
        install(config, sources)
    assert refused.value.code is ErrorCode.CONFIGURATION_CONFLICT
    layout, plan = install(config, sources, strip_comments=True)
    result = apply_install(plan)
    assert result["backup"] is not None
    backup = config / result["backup"] / "opencode.jsonc"
    assert backup.read_text() == text
    assert json.loads((config / "opencode.jsonc").read_text())["model"] == "p/m"


def test_default_agent_is_never_written_or_set_to_coordinator(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "opencode.json").write_text(json.dumps({"default_agent": "dridh/coordinator"}))
    with pytest.raises(DridhError) as refused:
        install(config, sources)
    assert refused.value.code is ErrorCode.CONFIGURATION_CONFLICT

    other = tmp_path / "other"
    other.mkdir()
    (other / "opencode.json").write_text(json.dumps({"default_agent": "build"}))
    layout, plan = install(other, sources)
    apply_install(plan)
    document = json.loads((other / "opencode.json").read_text())
    assert document["default_agent"] == "build"
    assert status(layout)["default_agent_is_coordinator"] is False


@pytest.mark.parametrize("text", ['{"a": }', '{"a": 1, "a": 2}', "[]", '{"plugins": "not-a-list"}'])
def test_malformed_or_ambiguous_configuration_fails_closed(tmp_path: Path, sources, text: str) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "opencode.json").write_text(text)
    with pytest.raises(DridhError) as refused:
        install(config, sources)
    assert refused.value.code is ErrorCode.CONFIGURATION_ERROR
    assert (config / "opencode.json").read_text() == text


def test_unmanaged_file_blocks_install_until_forced(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    (config / "agents" / "dridh").mkdir(parents=True)
    user_file = config / "agents" / "dridh" / "coordinator.md"
    user_file.write_text("my own coordinator\n")
    layout, plan = install(config, sources)
    assert plan.conflicts
    with pytest.raises(DridhError) as refused:
        apply_install(plan)
    assert refused.value.code is ErrorCode.CONFIGURATION_CONFLICT
    assert user_file.read_text() == "my own coordinator\n"
    apply_install(plan, force=True)
    backups = list((layout.backups).iterdir())
    assert backups
    assert (backups[0] / "files" / "agents" / "dridh" / "coordinator.md").read_text() == "my own coordinator\n"
    assert "description: coordinator" in user_file.read_text()


def test_uninstall_restores_exact_original_configuration(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    original = '{"model":"provider/model","providers":{}}'
    (config / "opencode.json").write_text(original)
    layout, plan = install(config, sources)
    apply_install(plan)
    assert (config / "opencode.json").read_text() != original
    removal = plan_uninstall(layout)
    assert removal.config_action == "restore"
    apply_uninstall(removal)
    assert (config / "opencode.json").read_text() == original
    assert not layout.manifest.exists()
    assert not (config / "agents" / "dridh" / "coordinator.md").exists()
    assert not layout.plugin_dir.exists()


def test_uninstall_keeps_user_edits_made_after_installation(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "opencode.json").write_text('{"model": "a/b"}\n')
    layout, plan = install(config, sources)
    apply_install(plan)
    edited = json.loads((config / "opencode.json").read_text())
    edited["model"] = "user/changed"
    (config / "opencode.json").write_text(json.dumps(edited, indent=2) + "\n")
    removal = plan_uninstall(layout)
    assert removal.config_action == "update"
    apply_uninstall(removal)
    remaining = json.loads((config / "opencode.json").read_text())
    assert remaining == {"model": "user/changed", "plugins": []}


def test_uninstall_refuses_modified_managed_file_until_forced(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    layout, plan = install(config, sources)
    apply_install(plan)
    (config / "agents" / "dridh" / "planner.md").write_text("edited by a person\n")
    removal = plan_uninstall(layout)
    assert removal.conflicts
    with pytest.raises(DridhError):
        apply_uninstall(removal)
    apply_uninstall(removal, force=True)
    assert not (config / "agents" / "dridh" / "planner.md").exists()


def test_uninstall_removes_configuration_file_that_installer_created(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    layout, plan = install(config, sources)
    apply_install(plan)
    apply_uninstall(plan_uninstall(layout))
    assert not layout.config_file.exists()


def test_both_configuration_files_prefer_opencode_json_and_explicit_choice_still_wins(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "opencode.json").write_text("{}")
    (config / "opencode.jsonc").write_text("{}")
    assert resolve_layout(config).config_file.name == "opencode.json"
    assert resolve_layout(config, "opencode.jsonc").config_file.name == "opencode.jsonc"


def test_only_a_jsonc_file_is_updated_when_it_is_the_sole_config(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "opencode.jsonc").write_text("{}")
    assert resolve_layout(config).config_file.name == "opencode.jsonc"


def test_install_records_the_dridh_executable_for_the_plugin(tmp_path: Path, sources) -> None:
    plugin, agents = sources
    layout = resolve_layout(tmp_path / "cfg")
    plan = plan_install(layout, plugin_source=plugin, agent_source=agents)
    recorded = plan.desired[f"dridh/plugin/{PLUGIN_NAME}/{EXECUTABLE_FILE}"].decode().strip()
    assert recorded and Path(recorded).name == "dridh"


def test_config_dir_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(DridhError) as relative:
        resolve_layout("relative/path")
    assert relative.value.code is ErrorCode.CONFIGURATION_ERROR


def test_environment_and_default_select_the_configuration_directory(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom))
    assert resolve_layout().config_dir == custom.resolve()
    monkeypatch.delenv("OPENCODE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_layout().config_dir == (tmp_path / "home" / ".config" / "opencode").resolve()


def test_incomplete_plugin_artifact_is_rejected(tmp_path: Path, sources) -> None:
    plugin, agents = sources
    (plugin / "dist" / "index.js").unlink()
    with pytest.raises(DridhError) as incomplete:
        plan_install(resolve_layout(tmp_path / "cfg"), plugin_source=plugin, agent_source=agents)
    assert incomplete.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE


def test_no_temporary_files_remain_after_apply(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    _, plan = install(config, sources)
    apply_install(plan)
    assert not [path for path in config.rglob("*") if path.name.endswith(".tmp")]


def test_upgrade_removes_profiles_that_are_no_longer_shipped(tmp_path: Path, sources) -> None:
    config = tmp_path / "cfg"
    layout, plan = install(config, sources)
    apply_install(plan)
    smaller = tmp_path / "smaller-agents"
    smaller.mkdir()
    (smaller / "coordinator.md").write_text("---\ndescription: coordinator\nmode: primary\n---\nBody v2\n")
    upgrade = plan_install(layout, plugin_source=sources[0], agent_source=smaller)
    assert any(action.action == "remove" and action.path.endswith("planner.md") for action in upgrade.actions)
    apply_install(upgrade)
    assert not (config / "agents" / "dridh" / "planner.md").exists()
    assert "Body v2" in (config / "agents" / "dridh" / "coordinator.md").read_text()


def test_install_refreshes_manifest_when_file_already_matches_the_new_release(tmp_path: Path, sources) -> None:
    """A managed file whose bytes already equal the shipped ones, but whose manifest hash is older, must not stay
    'modified' forever: the next install rewrites the manifest entry, so the startup integrity check passes again."""
    config = tmp_path / "cfg"
    layout, plan = install(config, sources)
    apply_install(plan)
    (sources[0] / "dist" / "index.js").write_text("export default { v: 2 }\n")
    (config / "dridh" / "plugin" / "dridh-bridge" / "dist" / "index.js").write_text("export default { v: 2 }\n")
    assert status(layout)["files"]["dridh/plugin/dridh-bridge/dist/index.js"] == "modified"

    upgrade = plan_install(layout, plugin_source=sources[0], agent_source=sources[1])
    assert [a.action for a in upgrade.actions if a.path.endswith("dist/index.js")] == ["update"]
    assert not upgrade.conflicts
    assert apply_install(upgrade)["status"] == "changed"
    assert status(layout)["files"]["dridh/plugin/dridh-bridge/dist/index.js"] != "modified"
