"""Phase 13 Planner worker registration and policy tests."""

from __future__ import annotations

from pathlib import Path

from dridh.orchestration.roster import PROFILES

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "dridh/workers/planner-indexer": "opencode/agents/dridh/workers/planner-indexer.md",
    "dridh/workers/planner-component": "opencode/agents/dridh/workers/planner-component.md",
    "dridh/workers/planner-testing": "opencode/agents/dridh/workers/planner-testing.md",
}


def test_all_phase13_workers_are_registered_and_packaged() -> None:
    for agent_id, relative_path in EXPECTED.items():
        assert agent_id in PROFILES
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert f"Role identifier: {agent_id}" in content
        assert "mode: subagent" in content
        assert "permissions:" in content
        assert 'action: "*"' in content
        assert "effect: deny" in content


def test_indexer_and_testing_workers_require_traceability_and_real_dependencies() -> None:
    indexer = (ROOT / EXPECTED["dridh/workers/planner-indexer"]).read_text(encoding="utf-8")
    testing = (ROOT / EXPECTED["dridh/workers/planner-testing"]).read_text(encoding="utf-8")
    assert "resource_ownership" in indexer
    assert "Every approved flow" in indexer
    assert "TESTCONTAINERS first" in testing
    assert "WIREMOCK_EXCEPTION" in testing
    assert "Do not propose routine mocks" in testing


def test_the_coordinator_indexes_an_existing_codebase_itself():
    """In an existing codebase the Coordinator builds the Code Reacher index with code_reacher.index; it does not send
    the person to a terminal (a user was told to run `dridh index` although the Coordinator may run it)."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "opencode" / "agents" / "dridh" / "coordinator.md").read_text(encoding="utf-8")
    rule = next(line for line in text.splitlines() if "check `code_reacher.index_status` before the Ideator" in line)
    assert "build it yourself with `code_reacher.index`" in rule
    assert "never send the user to a terminal for it" in rule
