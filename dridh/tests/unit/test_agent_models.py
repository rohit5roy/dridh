"""Which model each agent uses, and why."""

from __future__ import annotations

import json

import pytest

from dridh.observability import models
from dridh.observability.models import agent_models, headline, resolve
from dridh.observability.tables import build


@pytest.fixture
def clean(tmp_path, monkeypatch):
    config = tmp_path / "opencode-config"
    config.mkdir()
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config))
    for name in ("OPENCODE_CONFIG_CONTENT", "DRIDH_USAGE_MODEL_LABEL"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project"
    (project / ".opencode" / "dridh").mkdir(parents=True)
    return project, config


def test_nothing_configured_means_opencode_chooses(clean) -> None:
    project, _ = clean
    assert resolve(project, "dridh/planner") == ("OpenCode default", "default")


def test_the_order_is_run_then_profile_then_opencode_config_then_dridh_yaml(clean, monkeypatch) -> None:
    project, config = clean
    from dridh.config.dridh_yaml import create_default_config, save_config

    config_model = create_default_config(project, workspace_id="w")
    save_config(project / ".opencode" / "dridh" / "dridh.yaml",
                config_model.model_copy(update={"models": config_model.models.model_copy(update={"provider": "opencode", "default": "cheap-free"})}))
    assert resolve(project, "dridh/planner") == ("opencode/cheap-free", "dridh")
    (config / "opencode.jsonc").write_text('{\n  // the user\'s choice\n  "model": "openrouter/user-model",\n}\n')
    assert resolve(project, "dridh/planner") == ("openrouter/user-model", "user")
    (project / "opencode.json").write_text(json.dumps({"model": "openrouter/project-model"}))
    assert resolve(project, "dridh/planner") == ("openrouter/project-model", "project")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"default_agent": "dridh/coordinator", "model": "x/layered"}))
    assert resolve(project, "dridh/planner") == ("x/layered", "env")
    agents = config / "agents" / "dridh" / "workers"
    agents.mkdir(parents=True)
    (agents / "planner-indexer.md").write_text("---\ndescription: d\nmodel: anthropic/strong\nmode: subagent\n---\nBody model: no\n")
    assert resolve(project, "dridh/workers/planner-indexer") == ("anthropic/strong", "agent")
    assert resolve(project, "dridh/workers/planner-indexer", "x/passed") == ("x/passed", "run")


def test_every_agent_gets_a_row_with_its_last_run(clean) -> None:
    project, _ = clean
    entries = [{"agent": "dridh/executor", "model": "old/model", "at": "2026-11-20T10:00:00+00:00"},
               {"agent": "dridh/executor", "model": "new/model", "at": "2026-11-21T10:00:00+00:00"}]
    rows = agent_models(project, entries)
    assert rows[0]["agent"] == "dridh/coordinator"
    assert {r["agent"] for r in rows} >= set(models.agents())
    executor = next(r for r in rows if r["agent"] == "dridh/executor")
    assert (executor["runs"], executor["last_model"], executor["model"]) == (2, "new/model", "OpenCode default")
    assert headline(rows) == f"Models: OpenCode default (all {len(rows)} agents)"


def test_the_headline_splits_by_model() -> None:
    rows = [{"agent": a, "model": m} for a, m in (("a", "big"), ("b", "cheap"), ("c", "cheap"))]
    assert headline(rows) == "Models: cheap (2 agents) · big (1 agent)"


def test_the_metrics_tables_show_models_by_agent() -> None:
    report = {"models": [{"agent": "dridh/executor", "stage": "executor", "model": "m/cheap", "source": "dridh.yaml",
                          "runs": 3, "last_model": "m/cheap", "last_at": None}]}
    table = next(t for t in build(report)["tables"] if t["title"] == "Models by agent")
    assert table["rows"] == [["executor", "executor", "m/cheap", "dridh.yaml", "3", "same"]]
    assert table["lines"][0].split() == ["Agent", "Stage", "Model", "Why", "Runs", "Last", "run", "on"]
