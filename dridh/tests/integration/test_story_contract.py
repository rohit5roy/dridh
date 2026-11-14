"""Story cards carry the approved contract, and unusable file ownership is rejected.

Found by a benchmark of one feature on four models: an indexer card restated renew(item_id, today) as
renew(member_id, item_id, today) and the Executor built that (18 of 56 acceptance tests failed); another card said
"follow the signatures in the approved TDD", which the Executor cannot read; and ownership entries such as
"lendlib/library.py (hold methods only)" matched no file, so seven of twelve stories could never pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.executor.story_runner import implementation_prompt
from dridh.orchestration.repair import call_with_repair
from dridh.orchestration.roster import paths_within
from dridh.planner.contract import approved_signatures, calls, contract_conflicts, ownership_problem, story_contract
from dridh.planner.service import PlannerService, _overlap
from dridh.state.design import ApiEventRecord
from tests.integration.test_planner_infrastructure_story import Workers

# ------------------------------------------------------------------ A1: the contract reaches the Executor


def _event(position: int, use_case: str, flow: str | None, component: str, name: str, kind: str = "API") -> ApiEventRecord:
    return ApiEventRecord(use_case, flow, component, kind, name, position)


EVENTS = (
    _event(1, "UC-01", "SF-01.1", "CMP-01", "API: place_hold(member_id, item_id, today) -> None"),
    _event(2, "UC-01", "SF-01.1", "CMP-01", "holds_for(item_id) -> list[str]"),
    _event(3, "UC-01", "SF-01.2", "CMP-01", "cancel_hold(member_id, item_id) -> None"),
    _event(4, "UC-01", "SF-01.1", "CMP-02", "notify(member_id, message)"),
    _event(5, "UC-01", None, "CMP-01", "HoldPlaced", kind="EVENT"),
    _event(6, "UC-02", "SF-02.1", "CMP-01", "renew(item_id, today) -> date"),
    _event(7, "UC-01", "SF-01.2", "CMP-01", "holds_for(item_id) -> list[str]"),  # the same reference for another flow
)


def test_a_story_gets_the_references_of_its_flows_and_use_cases_verbatim_in_document_order() -> None:
    story = {"component_id": "CMP-01", "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1", "SF-01.2"]}
    assert story_contract(story, reversed(EVENTS)) == [  # given in any order, returned in document order
        "API: place_hold(member_id, item_id, today) -> None", "holds_for(item_id) -> list[str]",
        "cancel_hold(member_id, item_id) -> None", "Event: HoldPlaced"]  # CMP-02's notify is another component's; no duplicates


def test_a_story_whose_component_has_no_reference_gets_its_flows_references_and_infrastructure_gets_none() -> None:
    other = {"component_id": "CMP-03", "use_case_ids": ["UC-02"], "flow_ids": ["SF-02.1"]}
    assert story_contract(other, EVENTS) == ["renew(item_id, today) -> date"]  # it may call another component's API
    assert story_contract({"component_id": "CMP-01", "use_case_ids": ["UC-03"], "flow_ids": ["SF-03.1"]}, EVENTS) == []
    infrastructure = {"component_id": "CMP-01", "use_case_ids": [], "flow_ids": [], "infrastructure": True}
    assert story_contract(infrastructure, EVENTS) == []


def test_the_executor_prompt_carries_the_contract_section_only_when_there_is_one() -> None:
    story = {"story_id": "ST-1.1", "title": "Renew", "component_id": "CMP-01", "description": "Renew a loan.", "todos": ["Add renew."],
             "acceptance_criteria": ["A renewal extends the due date."], "definition_of_done": ["Tests pass."],
             "approved_contract": ["renew(item_id, today) -> date"]}
    prompt = implementation_prompt(story, ["src"], [])
    assert ("Approved contract for this story (copied from the approved design; implement these names and parameters exactly; "
            "if a todo or acceptance criterion disagrees, the contract wins):\n- renew(item_id, today) -> date\n") in prompt
    assert prompt.index("Approved contract") < prompt.index("Todos:")
    assert "Approved contract" not in implementation_prompt({**story, "approved_contract": []}, ["src"], [])
    assert "Approved contract" not in implementation_prompt({k: v for k, v in story.items() if k != "approved_contract"}, ["src"], [])


def test_the_real_planner_attaches_each_storys_contract_from_the_approved_tdd(tmp_path: Path) -> None:
    from tests.e2e.planner_test_helpers import seed_approved_baseline

    info, designer_session = seed_approved_baseline(tmp_path / "project")
    service = PlannerService(info, runner=Workers())
    session = service.start(designer_session, model="m")["session_id"]
    assert service.generate(session, model="m")["status"] == "GENERATED"
    stories = {s["key"]: s for s in service.planned_stories(session)}
    assert stories["scaffold"]["approved_contract"] == [] and stories["scaffold"]["flow_ids"] == []
    assert stories["sf-01-1-cmp-01"]["approved_contract"] == ["POST /orders"]
    assert stories["sf-01-1-cmp-01"]["flow_ids"] == ["SF-01.1"]
    assert stories["sf-02-1-cmp-02"]["approved_contract"] == ["Event: order.cancelled"]
    prompt = implementation_prompt({**stories["sf-01-1-cmp-01"], "story_id": "ST-1.1"}, ["src"], [])
    assert "Approved contract for this story" in prompt and "- POST /orders" in prompt

    # A plan whose bound TDD revision is not stored (an older plan, or a moved database) executes as before: no contract.
    payload = service._load(session)
    payload["source"]["tdd_revision_id"] = "rev-that-does-not-exist"
    service._save(payload)
    assert all(s["approved_contract"] == [] for s in service.planned_stories(session))


# ------------------------------------------------------------------ A2: cards may not contradict the contract

BASELINE = {
    "components": [{"id": "CMP-01"}],
    "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
    "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    "api_contract": [{"use_case_id": "UC-01", "flow_id": "SF-01.1", "component_id": "CMP-01", "reference_kind": "API",
                      "reference_name": "renew(item_id, today) -> date", "position": 1}],
}


def _card(**changes: object) -> dict[str, object]:
    story = {"key": "renew-loan", "component_id": "CMP-01", "title": "Renew a loan", "description": "Implement renewal.",
             "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"], "infrastructure": False, "infrastructure_rationale": "",
             "todos": ["Implement renew."], "acceptance_criteria": ["Renewing extends the due date by 14 days."],
             "definition_of_done": ["Tests pass."], "dependencies": [], "dependency_rationale": {},
             "file_ownership": ["lendlib/library.py"], "resource_ownership": []}
    story.update(changes)
    return story


def _validate(story: dict[str, object]) -> dict[str, object]:
    return PlannerService.__new__(PlannerService)._validate_indexer({"stories": [story], "summary": "s", "blockers": []}, BASELINE)


def test_a_card_restating_an_approved_signature_with_other_parameters_is_rejected_with_both_signatures() -> None:
    with pytest.raises(DridhError) as error:
        _validate(_card(todos=["Add Library.renew(member_id, item_id, today) returning the new due date."]))
    assert error.value.code is ErrorCode.WORKER_FAILED and "different parameters" in error.value.message
    assert error.value.details == ("renew-loan", "found: Library.renew(member_id, item_id, today)", "approved: renew(item_id, today) -> date")


@pytest.mark.parametrize("field, text", [
    ("todos", ["Implement renew(item_id, today) exactly as approved."]),  # the approved parameters
    ("todos", ["Implement renew(item_id: str, today: date = date.today()) -> date."]),  # annotations, defaults, return type
    ("todos", ["Implement renew  ( item_id ,today )."]),  # whitespace
    ("todos", ["Implement renew and keep holds working."]),  # no parentheses
    ("acceptance_criteria", ['renew("B1", date(2026, 1, 1)) returns 2026-01-15.']),  # an example call passes values
    ("description", ["Renew via renew(item_id=item, today=day)."]),  # keyword arguments name the approved parameters
])
def test_a_card_that_agrees_with_the_contract_or_does_not_restate_it_is_accepted(field: str, text: list[str]) -> None:
    value = text[0] if field == "description" else text
    assert _validate(_card(**{field: value}))["stories"][0]["key"] == "renew-loan"


@pytest.mark.parametrize("field", ["title", "description", "acceptance_criteria", "definition_of_done"])
def test_every_card_field_is_checked(field: str) -> None:
    text = "Use renew(member_id, item_id, today)."
    with pytest.raises(DridhError, match="different parameters"):
        _validate(_card(**{field: text if field in ("title", "description") else [text]}))


def test_signatures_compare_parameter_names_in_order() -> None:
    approved = approved_signatures(["API: place_hold(member_id, item_id, today) -> None"])
    assert contract_conflicts({"todos": ["place_hold(item_id, member_id, today)"]}, approved)  # same names, other order
    assert not contract_conflicts({"todos": ["def place_hold(self, member_id, item_id, *, today)"]}, approved)  # self and * ignored
    assert [c.params for c in calls("public Date renew(String itemId, LocalDate today)")] == [("itemId", "today")]


def test_a_rejected_card_goes_back_through_the_repair_loop_and_the_corrected_card_is_accepted() -> None:
    replies = iter([
        {"stories": [_card(todos=["Add renew(member_id, item_id, today)."])], "summary": "s", "blockers": []},
        {"stories": [_card(todos=["Add renew(item_id, today)."])], "summary": "s", "blockers": []},
    ])
    prompts: list[str] = []

    def run(prompt: str, session_id: str | None = None) -> SimpleNamespace:
        prompts.append(prompt)
        return SimpleNamespace(text=json.dumps(next(replies)), session_id="ses_contract001")

    outcome = call_with_repair(run, "PROMPT", label="Central Plan Indexer",
                               validate=lambda reply: PlannerService.__new__(PlannerService)._validate_indexer(reply, BASELINE))
    assert outcome.repaired and outcome.value["stories"][0]["todos"] == ["Add renew(item_id, today)."]
    feedback = prompts[1]
    assert "renew-loan" in feedback and "found: renew(member_id, item_id, today)" in feedback
    assert "approved: renew(item_id, today) -> date" in feedback


def test_the_indexer_is_told_python_attaches_the_contract_and_ownership_is_a_path() -> None:
    service = PlannerService.__new__(PlannerService)
    prompt = service._indexer_prompt({**BASELINE, "code_index": {"required": False}}, {})
    assert "Python attaches each story's approved contract" in prompt and "must not tell the Executor to consult the TDD" in prompt
    assert "project-relative paths or globs only" in prompt and "explicit relative paths/globs" not in prompt
    profile = (Path(__file__).resolve().parents[2] / "opencode" / "agents" / "dridh" / "workers" / "planner-indexer.md").read_text()
    assert "Python attaches each story's approved contract" in profile and "no notes, spaces, parentheses or commas" in profile


# ------------------------------------------------------------------ B: file ownership is a path


@pytest.mark.parametrize("entry", ["lendlib/library.py (hold methods only)", "a b.py", "x.py, y.py"])
def test_ownership_entries_that_are_not_paths_are_rejected_with_what_to_do(entry: str) -> None:
    with pytest.raises(DridhError) as error:
        _validate(_card(file_ownership=[entry]))
    assert error.value.code is ErrorCode.WORKER_FAILED
    assert "Ownership is per file" in error.value.message and "todos or definition_of_done" in error.value.message
    assert error.value.details[:2] == ("renew-loan", f"entry: {entry}")


@pytest.mark.parametrize("entry", ["lendlib/library.py", "tests/", "src/**/*.py", "sm-shop/src/main/java/com/x/Y.java", "app/[id]/page.tsx"])
def test_plain_paths_directories_and_globs_are_accepted(entry: str) -> None:
    assert ownership_problem(entry) is None
    assert _validate(_card(file_ownership=[entry]))["stories"][0]["file_ownership"] == [entry]


def test_unsafe_paths_are_still_rejected() -> None:
    for entry in ("/etc/passwd", "C:/x.py", "../outside.py", "src/../../x.py"):
        with pytest.raises(DridhError, match="plain project-relative paths"):
            _validate(_card(file_ownership=[entry]))


def test_ownership_enforcement_and_the_parallel_check_agree_with_accepted_globs() -> None:
    """Before: paths_within matched by prefix only, so `src/**/*.py` owned no file, and _overlap treated it as disjoint
    from `src/a.py`, so two stories that could both edit src/a.py were marked safe to run in parallel."""
    allowed = ("lendlib/library.py", "tests/", "src/**/*.py", "app/[id]/page.tsx")
    owned = ("lendlib/library.py", "tests/test_x.py", "src/a.py", "src/deep/b.py", "app/[id]/page.tsx")
    assert paths_within(allowed, owned) == []
    assert paths_within(allowed, ("lendlib/other.py", "src/a.ts", "app/x/page.tsx")) == ["lendlib/other.py", "src/a.ts", "app/x/page.tsx"]
    assert _overlap("src/**/*.py", "src/a.py") and _overlap("**/*.py", "lib/x.py") and _overlap("tests/", "tests/test_x.py")
    assert not _overlap("src/*.py", "lib/x.py") and not _overlap("a.py", "b.py")
