"""A plan with an infrastructure story (no use case, no flow) that other stories depend on.

Found by the first real-model run: the phase file listed the model's story keys as Unlocks (failing validation), and its
SQLite projection could never match because stories without use-case links were invisible to the reconciliation check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from dridh.planner.service import PlannerService
from tests.e2e.planner_test_helpers import seed_approved_baseline, tick_review

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
_argv, sys.argv = sys.argv, ["stub_provider", "0", "/dev/null", "/dev/null"]  # the stub reads its port and paths from argv at import
try:
    import stub_provider  # noqa: E402
finally:
    sys.argv = _argv


class Workers:
    def run_sync(self, *, agent: str, prompt: str, **_: object):
        if agent == "dridh/workers/planner-indexer":
            reply = stub_provider._phase13_indexer(prompt)
            component = reply["stories"][0]["component_id"]
            scaffold = {
                "key": "scaffold", "component_id": component, "title": "Scaffold the package", "description": "Create the package layout.",
                "use_case_ids": [], "flow_ids": [], "todos": ["Create the package."], "acceptance_criteria": ["The package imports."],
                "definition_of_done": ["Reviewed."], "dependencies": [], "dependency_rationale": {}, "file_ownership": ["pyproject.toml"],
                "resource_ownership": [], "infrastructure": True, "infrastructure_rationale": "Every feature story needs the package to exist.",
            }
            for story in reply["stories"]:
                story["dependencies"] = ["scaffold"]
                story["dependency_rationale"] = {"scaffold": "Needs the package layout."}
            reply["stories"].insert(0, scaffold)
        elif agent == "dridh/workers/planner-component":
            reply = stub_provider._phase13_component(prompt)
        else:
            reply = stub_provider._phase13_testing(prompt)
        return SimpleNamespace(text=json.dumps(reply), session_id="ses_infra00000001")


def test_a_plan_with_an_infrastructure_prerequisite_generates_reconciles_and_is_approved(tmp_path: Path) -> None:
    info, designer_session = seed_approved_baseline(tmp_path / "project")
    service = PlannerService(info, runner=Workers())
    session = service.start(designer_session, model="m")["session_id"]
    generated = service.generate(session, model="m")
    assert generated["status"] == "GENERATED", generated.get("last_error")
    review = service.open_review(session)
    tick_review(info, review["review_id"])
    service.approve(session, reviewer="r", confirmation=service.approval_challenge(session)["confirmation"])
    stories = {s["key"]: s for s in service.execution_stories(session)}
    scaffold_id = stories["scaffold"]["story_id"]
    dependents = [s for k, s in stories.items() if k != "scaffold"]
    assert dependents and all(s["dependencies"] == [scaffold_id] for s in dependents)
    phase_files = [f["path"] for f in generated["generated"]["files"] if "/PHASE-" in f["path"]]
    text = "\n".join((info.project_root / p).read_text(encoding="utf-8") for p in phase_files)
    assert "| Unlocks | scaffold" not in text and "| Use Case ID | N/A |" in text
    service.sync(session)  # status-only synchronisation must accept the projection (it raised before the fix)
