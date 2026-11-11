"""Workers that need no tools still offer one inert `read` rule and nothing else."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKERS = Path(__file__).resolve().parents[2] / "opencode" / "agents" / "dridh" / "workers"
NO_TOOL_WORKERS = ("ideator-inquirer", "ideator-doc-generator", "ideator-handoff", "designer-inquirer", "designer-doc-generator", "designer-handoff")


def _permissions(name: str) -> list[dict[str, str]]:
    text = (WORKERS / f"{name}.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---")[1])["permissions"]


def test_tool_free_workers_deny_everything_except_a_read_of_a_path_that_does_not_exist() -> None:
    for name in NO_TOOL_WORKERS:
        rules = _permissions(name)
        assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}, name
        allows = [r for r in rules if r["effect"] == "allow"]
        assert allows == [{"action": "read", "resource": "/dridh-no-access/*", "effect": "allow"}], name
        assert not Path("/dridh-no-access").exists()
