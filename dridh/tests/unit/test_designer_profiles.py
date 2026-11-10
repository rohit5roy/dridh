"""Phase 12 agent profile roster and packaged worker permissions."""

from __future__ import annotations

from pathlib import Path

from dridh.orchestration.roster import PROFILES

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "dridh/technical-designer": "opencode/agents/dridh/technical-designer.md",
    "dridh/workers/designer-inquirer": "opencode/agents/dridh/workers/designer-inquirer.md",
    "dridh/workers/designer-doc-generator": "opencode/agents/dridh/workers/designer-doc-generator.md",
    "dridh/workers/designer-handoff": "opencode/agents/dridh/workers/designer-handoff.md",
}


def test_all_phase12_profiles_are_registered_and_packaged() -> None:
    for agent_id, relative_path in EXPECTED.items():
        assert agent_id in PROFILES
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert f"Role identifier: {agent_id}" in content
        assert "mode: subagent" in content
        assert "permissions:" in content
    for worker_id in tuple(EXPECTED)[1:]:
        content = (ROOT / EXPECTED[worker_id]).read_text(encoding="utf-8")
        assert 'action: "*"' in content
        assert "effect: deny" in content
