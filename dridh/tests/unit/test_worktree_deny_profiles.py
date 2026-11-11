"""Profiles that may edit must deny edits to OpenCode-managed worktrees; the tool-name deny is a no-op."""
from __future__ import annotations

from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "opencode" / "agents" / "dridh"
DENY = '  - action: edit\n    resource: "*/opencode/worktree/*"\n    effect: deny'


def _profiles() -> list[Path]:
    return sorted(AGENTS.rglob("*.md"))


def test_every_profile_allowing_edit_denies_opencode_worktrees_last() -> None:
    checked = 0
    for path in _profiles():
        text = path.read_text()
        front = text.split("---")[1] if text.startswith("---") else ""
        if "action: edit" not in front or "effect: allow" not in front:
            continue
        if "action: edit\n    resource: \"*\"\n    effect: allow" not in front and "action: edit" not in front:
            continue
        checked += 1
        assert DENY in front, f"{path.name} lacks the worktree edit-deny"
        assert front.rstrip().endswith(DENY.strip()) or front.rindex(DENY) > front.rindex("effect: allow"), (
            f"{path.name}: the deny must come after every allow (last match wins)"
        )
    assert checked >= 4


def test_no_profile_relies_on_the_noop_tool_name_rule() -> None:
    for path in _profiles():
        assert "action: opencode_worktree_remove" not in path.read_text(), path.name
