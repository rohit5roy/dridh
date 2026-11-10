"""Phase files list Unlocks as story IDs, never the model's story keys (found by the first real-model run)."""

from __future__ import annotations

from pathlib import Path

from dridh.document_contracts.validator import validate_phase_file
from dridh.planner.service import PlannerService
from dridh.workspace.layout import init_workspace


def _story(key: str, deps: list[str]) -> dict:
    return {"key": key, "component_id": "CMP-01", "phase": 1, "title": f"Story {key}", "description": "Do it.",
            "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"], "todos": ["build"], "acceptance_criteria": ["works"],
            "definition_of_done": ["tests pass"], "dependencies": deps, "dependency_rationale": {d: "needs it" for d in deps},
            "file_ownership": [f"src/{key}.py"], "resource_ownership": [], "infrastructure": False, "infrastructure_rationale": ""}


def test_a_story_that_unlocks_another_renders_a_valid_phase_file(tmp_path: Path) -> None:
    info, _ = init_workspace(tmp_path, mode="new")
    stories = [_story("greeter-impl", []), _story("greeter-tests", ["greeter-impl"])]
    story_ids = {"greeter-impl": "ST-1.1", "greeter-tests": "ST-1.2"}
    unlocks = {"greeter-impl": ["greeter-tests"], "greeter-tests": []}
    rendered = PlannerService(info)._render_phase({"component_id": "CMP-01", "name": "Greeter"}, 1, stories, story_ids, unlocks)
    assert "| Unlocks | ST-1.2 |" in rendered and "| Unlocks | greeter-tests" not in rendered
    result = validate_phase_file(rendered, file="PHASE-1-greeter.md")
    assert not [e for e in result.errors if "Unlocks" in e], result.errors
