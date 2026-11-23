"""A plan whose model text contains Markdown (code spans, bold, a pipe) is still executable after approval.

Found by a real-model run: the Testing planner wrote `code spans` in test scopes. Rendering the plan and importing it
into SQLite drops the backticks, so the check that SQLite still matches the approved proposal refused the plan
("The SQLite test-plan definitions changed independently of the approved Planner proposal") and execution could not
start. Both sides are now compared as they read back from the Markdown table.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from dridh.planner.service import PlannerService
from tests.e2e.planner_test_helpers import seed_approved_baseline, tick_review

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
_argv, sys.argv = sys.argv, ["stub_provider", "0", "/dev/null", "/dev/null"]
try:
    import stub_provider  # noqa: E402
finally:
    sys.argv = _argv


class MarkdownWorkers:
    def run_sync(self, *, agent: str, prompt: str, **_: object):
        if agent == "dridh/workers/planner-indexer":
            reply = stub_provider._phase13_indexer(prompt)
            reply["stories"][0]["title"] = "Add the `place_hold` **entry point**"
        elif agent == "dridh/workers/planner-component":
            reply = stub_provider._phase13_component(prompt)
        else:
            reply = stub_provider._phase13_testing(prompt)
            for phase in reply["phases"]:
                phase["test_scope"] = f"`library_holds` package: **{phase['test_scope']}** (a | b)"
                phase["evidence"] = f"`pytest` report; {phase['evidence']}"
        return SimpleNamespace(text=json.dumps(reply), session_id="ses_markdown0001")


def test_markdown_in_the_models_plan_text_does_not_make_the_approved_plan_unexecutable(tmp_path: Path) -> None:
    info, designer_session = seed_approved_baseline(tmp_path / "project")
    service = PlannerService(info, runner=MarkdownWorkers())
    session = service.start(designer_session, model="m")["session_id"]
    generated = service.generate(session, model="m")
    assert generated["status"] == "GENERATED", generated.get("last_error")
    review = service.open_review(session)
    tick_review(info, review["review_id"])
    service.approve(session, reviewer="r", confirmation=service.approval_challenge(session)["confirmation"])
    service.sync(session)  # raised CONFIGURATION_CONFLICT before the fix
    assert service.gate(session)["unlocked"] is True
    assert service.execution_stories(session)
