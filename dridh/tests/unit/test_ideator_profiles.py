"""Phase 11 agent roster and profile contract regression tests."""
from __future__ import annotations

from pathlib import Path

from dridh.orchestration.roster import PROFILES

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "dridh/ideator": "opencode/agents/dridh/ideator.md",
    "dridh/workers/ideator-inquirer": "opencode/agents/dridh/workers/ideator-inquirer.md",
    "dridh/workers/ideator-doc-generator": "opencode/agents/dridh/workers/ideator-doc-generator.md",
    "dridh/workers/ideator-handoff": "opencode/agents/dridh/workers/ideator-handoff.md",
}


def test_all_four_phase11_profiles_are_registered_and_packaged() -> None:
    for agent_id, relative_path in EXPECTED.items():
        assert agent_id in PROFILES
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "mode: subagent" in content
        assert "description:" in content
        assert "permissions:" in content
        assert "## Input contract" in content
        assert "## Output contract" in content
    for worker_id in tuple(EXPECTED)[1:]:
        content = (ROOT / EXPECTED[worker_id]).read_text(encoding="utf-8")
        assert "effect: deny" in content
