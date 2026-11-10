"""The TypeScript plugin and the Python bridge must expose the same operations."""

from __future__ import annotations

import re
from pathlib import Path

from dridh.common.protocol import ALLOWED_OPERATIONS, DESIGNER_OPERATIONS, JEV_OPERATIONS, ORCHESTRATION_OPERATIONS, PLANNER_OPERATIONS, RUNTIME_OPERATIONS

CLIENT = Path(__file__).resolve().parents[2] / "opencode" / "plugins" / "dridh-bridge" / "src" / "bridge-client.ts"


def _array(name: str) -> set[str]:
    source = CLIENT.read_text()
    match = re.search(rf"export const {name} = \[(.*?)\] as const", source, re.DOTALL)
    assert match, f"{name} not found in bridge-client.ts"
    return set(re.findall(r'"([a-z_.]+)"', match.group(1)))


def test_plugin_runtime_operations_match_the_bridge() -> None:
    assert _array("RUNTIME_OPERATIONS") == set(RUNTIME_OPERATIONS)


def test_plugin_workflow_operations_match_the_bridge() -> None:
    plugin_orchestration = _array("WORKFLOW_OPERATIONS") | _array("IDEATOR_OPERATIONS") | _array("DESIGNER_OPERATIONS") | _array("PLANNER_OPERATIONS") | _array("COORDINATOR_OPERATIONS")
    assert plugin_orchestration == set(ORCHESTRATION_OPERATIONS)


def test_plugin_jev_operations_match_the_bridge() -> None:
    assert _array("JEV_OPERATIONS") == set(JEV_OPERATIONS) == {"jev.decide"}



def test_plugin_designer_operations_match_the_bridge() -> None:
    assert _array("DESIGNER_OPERATIONS") == set(DESIGNER_OPERATIONS) == {
        "designer.start",
        "designer.answer",
        "designer.status",
        "designer.generate",
        "designer.parity",
        "designer.gate",
    }


def test_plugin_never_exposes_approval_or_rejection() -> None:
    exposed = _array("WORKFLOW_OPERATIONS") | _array("IDEATOR_OPERATIONS") | _array("DESIGNER_OPERATIONS") | _array("PLANNER_OPERATIONS") | _array("COORDINATOR_OPERATIONS") | _array("CODE_REACHER_OPERATIONS") | _array("RUNTIME_OPERATIONS") | _array("JEV_OPERATIONS")
    assert not {op for op in exposed if "approve" in op or "reject" in op or "waive" in op or "complete" in op}
    from dridh.common.protocol import CODE_REACHER_OPERATIONS, HOOK_OPERATIONS

    assert not {op for op in _array("HOOK_OPERATIONS") if "approve" in op or "reject" in op or "waive" in op or "complete" in op}
    assert ALLOWED_OPERATIONS == set(RUNTIME_OPERATIONS) | set(JEV_OPERATIONS) | set(ORCHESTRATION_OPERATIONS) | set(CODE_REACHER_OPERATIONS) | set(HOOK_OPERATIONS)



def test_plugin_planner_operations_match_the_bridge() -> None:
    assert _array("PLANNER_OPERATIONS") == set(PLANNER_OPERATIONS) == {
        "planner.start",
        "planner.clarify",
        "planner.status",
        "planner.generate",
        "planner.sync",
        "planner.gate",
    }


def test_plugin_coordinator_operations_match_the_bridge() -> None:
    from dridh.common.protocol import COORDINATOR_OPERATIONS

    assert _array("COORDINATOR_OPERATIONS") == set(COORDINATOR_OPERATIONS) == {
        "coordinator.create_feature",
        "coordinator.status",
        "coordinator.sync",
        "coordinator.transition",
        "coordinator.get_context",
        "coordinator.query",
        "coordinator.validate",
        "coordinator.list_reviews",
        "coordinator.review_checklist",
    }


def test_plugin_code_reacher_operations_match_the_bridge() -> None:
    from dridh.common.protocol import CODE_REACHER_OPERATIONS

    assert _array("CODE_REACHER_OPERATIONS") == set(CODE_REACHER_OPERATIONS) == {
        "code_reacher.lookup",
        "code_reacher.symbol",
        "code_reacher.dependencies",
        "code_reacher.index_status",
        "code_reacher.index",
        "code_reacher.code_for",
        "code_reacher.owners",
    }


def test_hook_operations_match_the_bridge_and_are_not_offered_to_agents() -> None:
    from dridh.common.protocol import HOOK_OPERATIONS

    assert _array("HOOK_OPERATIONS") == set(HOOK_OPERATIONS) == {"hook.context", "hook.tool_call", "hook.session_event"}
    index = (CLIENT.parent / "index.ts").read_text()
    tool_list = re.search(r"const ALL_OPERATIONS = \[(.*?)\]", index, re.DOTALL)
    assert tool_list and "HOOK_OPERATIONS" not in tool_list.group(1)
    for hook in ('ctx.session.hook("context"', 'ctx.tool.hook("execute.before"', 'ctx.tool.hook("execute.after"', "ctx.event.subscribe"):
        assert hook in index
