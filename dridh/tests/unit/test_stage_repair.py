"""Ideator and Technical Designer repair contract violations from their workers (plan rule 9), boundedly."""

from __future__ import annotations

import json
from collections import defaultdict
from types import SimpleNamespace

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.designer.service import TOPICS as DESIGNER_TOPICS
from dridh.designer.service import DesignerService
from dridh.ideator.service import IdeatorService
from dridh.jev import JevClient
from tests.unit.test_designer_service import Phase12Workers, approved_ideator
from tests.unit.test_designer_service import workspace as designer_workspace
from tests.unit.test_ideator_service import complete_discovery, workspace

INVALID = json.dumps({"unexpected": True})  # valid JSON that violates every worker's output contract
IDEATOR_DOC = "dridh/workers/ideator-doc-generator"
IDEATOR_INQUIRER = "dridh/workers/ideator-inquirer"
IDEATOR_HANDOFF = "dridh/workers/ideator-handoff"
DESIGNER_INQUIRER = "dridh/workers/designer-inquirer"
DESIGNER_DOC = "dridh/workers/designer-doc-generator"
DESIGNER_HANDOFF = "dridh/workers/designer-handoff"


class FlakyWorkers(Phase12Workers):
    """Contract-violating replies first (``bad[agent]`` of them), then the normal valid reply."""

    def __init__(self, bad: dict[str, int]) -> None:
        self.bad = dict(bad)
        self.prompts: dict[str, list[str]] = defaultdict(list)

    def run_sync(self, *, agent, prompt, model=None, title=None):
        self.prompts[agent].append(prompt)
        if self.bad.get(agent, 0) > 0:
            self.bad[agent] -= 1
            return SimpleNamespace(text=INVALID)
        return super().run_sync(agent=agent, prompt=prompt, model=model, title=title)


def flaky(**by_agent: int) -> FlakyWorkers:
    return FlakyWorkers(by_agent)


def _ideator(tmp_path, workers):
    project, info = workspace(tmp_path)
    service = IdeatorService(info, runner=workers, jev_client=JevClient(base_url=""))
    session_id = service.start("Build an order intake service")["session_id"]
    complete_discovery(service, session_id)
    return project, service, session_id


def test_ideator_repairs_a_bad_document_proposal_and_records_it(tmp_path):
    workers = flaky(**{IDEATOR_DOC: 2})
    project, service, session_id = _ideator(tmp_path, workers)
    generated = service.generate(session_id, model="m")
    assert generated["status"] == "IN_REVIEW" and len(workers.prompts[IDEATOR_DOC]) == 3
    assert "rejected by Dridh's deterministic validation" in workers.prompts[IDEATOR_DOC][1]
    assert "proposal is missing required fields" in workers.prompts[IDEATOR_DOC][1]
    history = service._load(session_id)["repair_history"]
    assert history[0]["agent"] == IDEATOR_DOC and len(history[0]["attempts"]) == 2


def test_ideator_repairs_the_inquirer_and_handoff_replies(tmp_path):
    workers = flaky(**{IDEATOR_INQUIRER: 1, IDEATOR_HANDOFF: 1})
    _, service, session_id = _ideator(tmp_path, workers)
    assert service.generate(session_id, model="m")["status"] == "IN_REVIEW"
    assert len(workers.prompts[IDEATOR_INQUIRER]) == 2 and len(workers.prompts[IDEATOR_HANDOFF]) == 2


def test_ideator_stops_after_three_repairs_and_writes_nothing(tmp_path):
    workers = flaky(**{IDEATOR_HANDOFF: 99})
    project, service, session_id = _ideator(tmp_path, workers)
    with pytest.raises(DridhError) as exc_info:
        service.generate(session_id, model="m")
    assert exc_info.value.code is ErrorCode.WORKER_FAILED and "after 3 repair attempts" in exc_info.value.message
    assert len(workers.prompts[IDEATOR_HANDOFF]) == 4 and "Human intervention" in exc_info.value.recovery
    assert not (project / "001-USE-CASES.md").exists()


def _designer(tmp_path, workers):
    project, info = designer_workspace(tmp_path)
    ideator_session = approved_ideator(info)
    designer = DesignerService(info, runner=workers)
    session_id = designer.start(
        ideator_session, feature="order-intake", existing_context="First release accepts valid orders.",
        code_reacher_evidence="Code Reacher: repository inventory verified.",
    )["session_id"]
    for topic, _question, _impact in DESIGNER_TOPICS:
        designer.answer(session_id, topic=topic, answer=f"Confirmed {topic}.", source="Engineering owner", impact="Affects design.")
    return project, designer, session_id


def test_designer_repairs_a_bad_tdd_proposal_and_records_it(tmp_path):
    workers = flaky(**{DESIGNER_DOC: 2})
    project, designer, session_id = _designer(tmp_path, workers)
    generated = designer.generate(session_id, model="m")
    assert generated["status"] == "IN_REVIEW" and len(workers.prompts[DESIGNER_DOC]) == 3
    assert "rejected by Dridh's deterministic validation" in workers.prompts[DESIGNER_DOC][1]
    assert designer._load(session_id)["repair_history"][0]["agent"] == DESIGNER_DOC
    assert (project / "order-intake_tdd.md").is_file()


def test_designer_stops_after_three_repairs_and_writes_no_tdd(tmp_path):
    workers = flaky(**{DESIGNER_DOC: 99})
    project, designer, session_id = _designer(tmp_path, workers)
    with pytest.raises(DridhError) as exc_info:
        designer.generate(session_id, model="m")
    assert "after 3 repair attempts" in exc_info.value.message and len(workers.prompts[DESIGNER_DOC]) == 4
    assert not (project / "order-intake_tdd.md").exists()


def test_designer_handoff_exhaustion_degrades_to_mandatory_human_review(tmp_path):
    workers = flaky(**{DESIGNER_HANDOFF: 99})
    _, designer, session_id = _designer(tmp_path, workers)
    generated = designer.generate(session_id, model="m")
    assert generated["status"] == "IN_REVIEW" and len(workers.prompts[DESIGNER_HANDOFF]) == 4
    assert "human review remains mandatory" in designer._load(session_id)["handoff"]["summary"]


def test_designer_repairs_its_inquirer(tmp_path):
    workers = flaky(**{DESIGNER_INQUIRER: 1})
    _, designer, session_id = _designer(tmp_path, workers)
    assert len(workers.prompts[DESIGNER_INQUIRER]) == 2 and designer.status(session_id)["status"] == "INQUIRY"
