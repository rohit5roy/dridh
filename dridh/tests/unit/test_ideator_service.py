"""Phase 11 Ideator persistence, generation, approval and gate regression suite."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.ideator.service import TOPICS, IdeatorError, IdeatorService
from dridh.jev import JevClient
from dridh.state.store import StateStore
from dridh.state.usecases import UseCaseRepository
from dridh.workspace.layout import init_workspace
from tests.e2e.planner_test_helpers import tick_review

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ideator"


def workspace(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project, info


def service_for(info: Any, *, runner: Any | None = None) -> IdeatorService:
    return IdeatorService(info, runner=runner, jev_client=JevClient(base_url=""))


def load_proposal(name: str = "valid-proposal.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def complete_discovery(service: IdeatorService, session_id: str) -> None:
    for topic in TOPICS:
        service.record_answer(
            session_id,
            topic=topic,
            question=f"Please clarify {topic.replace('_', ' ')}.",
            answer=f"Confirmed discovery details for {topic.replace('_', ' ')}.",
            source="Product owner",
        )


def test_discovery_is_resumable_and_markdown_is_a_json_projection(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)
    started = service.start("Build an order intake service", existing_context="Existing context: order intake is manual.")
    session_id = started["session_id"]
    service.record_answer(
        session_id,
        topic="problem",
        question="What problem are we solving?",
        answer="Order intake is manual and unreliable.",
        source="Product owner",
    )

    resumed = service_for(info).status(session_id)
    assert resumed["status"] == "DISCOVERY"
    assert "problem" not in resumed["topics_missing"]
    assert len(resumed["topics_missing"]) == len(TOPICS) - 1
    assert service.parity(session_id)["ok"] is True

    json_path = info.root / "artifacts" / "ideator" / session_id / "inquiry.json"
    markdown_path = info.root / "artifacts" / "ideator" / session_id / "INQUIRY-NOTES.md"
    inquiry = json.loads(json_path.read_text(encoding="utf-8"))
    notes = markdown_path.read_text(encoding="utf-8")
    assert inquiry["existing_context"].startswith("Existing context")
    assert inquiry["questions"][0]["source"] == "Product owner"
    assert "STAGING — NOT THE CANONICAL APPROVED USE-CASE DOCUMENT" in notes
    assert "Existing context: order intake is manual." in notes
    assert "Order intake is manual and unreliable." in notes


def test_generation_persists_usecase_projection_but_keeps_designer_locked_until_approval(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)

    generated = service.generate(session_id, proposal=load_proposal(), run_workers=False)
    assert generated["status"] == "IN_REVIEW"
    assert generated["validation"]["ok"] is True
    assert generated["technical_designer"]["unlocked"] is False
    assert service.parity(session_id)["ok"] is True

    document = (project / "001-USE-CASES.md").read_text(encoding="utf-8")
    assert "ACT-01" in document and "UC-01" in document
    assert "SF-01.1" in document and "EF-01.1" in document
    assert "| Q-001" not in document
    assert "Please clarify problem." in document
    assert (info.root / "artifacts" / "ideator" / session_id / "REVIEW-CHECKLIST.md").is_file()

    with StateStore(info.state_db, info.workspace_id) as store:
        metadata = store.connection.execute(
            "SELECT id FROM document WHERE relative_path='001-USE-CASES.md'"
        ).fetchone()
        assert metadata is not None
        model = UseCaseRepository(store.connection, info.workspace_id).stored_model(metadata["id"])
        assert [actor.actor_id for actor in model.actors] == ["ACT-01"]
        assert [item.use_case_id for item in model.use_cases] == ["UC-01"]
        assert [flow.flow_id for flow in model.flows] == ["SF-01.1", "EF-01.1"]

    gate = service.gate(session_id)
    assert gate["unlocked"] is False
    assert "human approval" in gate["reason"].lower()


def test_human_approval_reconciles_projection_and_unlocks_technical_designer(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)
    generated = service.generate(session_id, proposal=load_proposal(), run_workers=False)

    challenge = service.approval_challenge(session_id)
    tick_review(service.workspace, challenge["review_id"])
    approved = service.approve(
        session_id,
        reviewer="Product Owner",
        confirmation=challenge["confirmation"],
    )
    assert approved["status"] == "APPROVED"
    assert approved["revision_id"] == generated["revision_id"]
    assert approved["technical_designer"]["unlocked"] is True
    assert approved["technical_designer"]["approved_by"] == "Product Owner"
    assert service.status(session_id)["technical_designer"]["unlocked"] is True


def test_approval_refuses_document_changed_after_review_and_never_unlocks(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)
    service.generate(session_id, proposal=load_proposal(), run_workers=False)
    challenge = service.approval_challenge(session_id)
    tick_review(service.workspace, challenge["review_id"])
    target = project / "001-USE-CASES.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nEdited after review.\n", encoding="utf-8")

    with pytest.raises(DridhError) as error:
        service.approve(session_id, reviewer="Reviewer", confirmation=challenge["confirmation"])
    assert error.value.code is ErrorCode.CONFIGURATION_CONFLICT
    assert service.gate(session_id)["unlocked"] is False


def test_generation_rejects_incomplete_discovery_and_bad_actor_references(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    with pytest.raises(DridhError) as incomplete:
        service.generate(session_id, proposal=load_proposal(), run_workers=False)
    assert incomplete.value.code is ErrorCode.VALIDATION_FAILED

    complete_discovery(service, session_id)
    with pytest.raises(IdeatorError, match="unknown actor"):
        service.generate(session_id, proposal=load_proposal("invalid-unknown-actor.json"), run_workers=False)
    with pytest.raises(IdeatorError, match="origin_step"):
        service.generate(session_id, proposal=load_proposal("invalid-branch-origin.json"), run_workers=False)


def test_existing_document_ids_are_reserved_before_regeneration(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)

    first = service.generate(session_id, proposal=load_proposal(), run_workers=False)
    first_document = (project / "001-USE-CASES.md").read_text(encoding="utf-8")
    assert "ACT-01" in first_document and "UC-01" in first_document
    assert first["status"] == "IN_REVIEW"

    service.record_answer(
        session_id,
        topic="assumptions",
        question="Please clarify assumptions.",
        answer="A customer has already selected the products.",
        source="Product owner",
    )
    second = service.generate(session_id, proposal=load_proposal(), run_workers=False)
    second_document = (project / "001-USE-CASES.md").read_text(encoding="utf-8")
    assert second["status"] == "IN_REVIEW"
    assert "ACT-02" in second_document and "UC-02" in second_document
    assert "SF-02.1" in second_document and "EF-02.1" in second_document


def test_inquiry_regression_fixture_has_a_versioned_question_contract() -> None:
    payload = json.loads((FIXTURES / "inquiry-regression.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "dridh.inquiry"
    assert payload["schema_version"] == 1
    assert [item["question_id"] for item in payload["questions"]] == ["Q-001", "Q-002"]
    assert all({"topic", "order", "question", "answer", "source", "unresolved"} <= item.keys() for item in payload["questions"])


def test_previously_managed_but_tampered_canonical_document_is_not_overwritten(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)
    service.generate(session_id, proposal=load_proposal(), run_workers=False)
    canonical = project / "001-USE-CASES.md"
    canonical.write_text(canonical.read_text(encoding="utf-8") + "\\nManual edit.\\n", encoding="utf-8")

    service.record_answer(
        session_id,
        topic="assumptions",
        question="Please clarify assumptions.",
        answer="The customer already selected the products.",
        source="Product owner",
    )
    tampered = canonical.read_text(encoding="utf-8")
    with pytest.raises(DridhError) as error:
        service.generate(session_id, proposal=load_proposal(), run_workers=False)
    assert error.value.code is ErrorCode.CONFIGURATION_CONFLICT
    assert canonical.read_text(encoding="utf-8") == tampered


def test_resume_rejects_invalid_question_schema_even_when_both_projections_are_modified(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)
    json_path = info.root / "artifacts" / "ideator" / session_id / "inquiry.json"
    notes_path = info.root / "artifacts" / "ideator" / session_id / "INQUIRY-NOTES.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["questions"][0]["topic"] = "unsupported-topic"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # Even a matching projection cannot make an invalid persisted schema acceptable.
    notes_path.write_text(service._render_notes(payload), encoding="utf-8")
    with pytest.raises(DridhError) as error:
        service.status(session_id)
    assert error.value.code is ErrorCode.STATE_CORRUPT


def test_markdown_tampering_is_detected_before_generation(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    notes = info.root / "artifacts" / "ideator" / session_id / "INQUIRY-NOTES.md"
    notes.write_text(notes.read_text(encoding="utf-8") + "\nTampered.\n", encoding="utf-8")
    assert service.parity(session_id)["ok"] is False
    with pytest.raises(DridhError) as mismatch:
        service.generate(session_id, proposal=load_proposal(), run_workers=False)
    assert mismatch.value.code is ErrorCode.VALIDATION_FAILED


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_sync(self, *, agent: str, prompt: str, model: str | None = None, title: str | None = None):
        self.calls.append({"agent": agent, "prompt": prompt, "model": model, "title": title})
        if agent.endswith("ideator-inquirer"):
            payload = {"summary": "Order intake needs a reliable path.", "key_facts": ["Customer submits orders."], "gaps": [], "unresolved": []}
        elif agent.endswith("ideator-doc-generator"):
            payload = load_proposal()
        elif agent.endswith("ideator-handoff"):
            payload = {"summary": "All required topics are represented.", "concerns": []}
        else:
            raise AssertionError(f"Unexpected Ideator worker: {agent}")
        return SimpleNamespace(text=json.dumps(payload))


def test_full_worker_pipeline_calls_all_three_bounded_workers(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    runner = FakeRunner()
    service = IdeatorService(info, runner=runner)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)

    result = service.generate(session_id)
    assert result["status"] == "IN_REVIEW"
    assert [call["agent"] for call in runner.calls] == [
        "dridh/workers/ideator-inquirer",
        "dridh/workers/ideator-doc-generator",
        "dridh/workers/ideator-handoff",
    ]
    assert "DISCOVERY:" in runner.calls[2]["prompt"]
    assert "Please clarify problem." in runner.calls[2]["prompt"]
    assert service.parity(session_id)["ok"] is True


def test_generation_records_the_shared_jev_checkpoint_as_advisory_with_a_deterministic_fallback(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)  # Jev is unreachable here: the shared checkpoint must fall back, not block
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)
    generated = service.generate(session_id, proposal=load_proposal(), run_workers=False)

    checkpoint = generated["jev_checkpoint"]
    assert checkpoint["advisory"] is True and checkpoint["flagged"] is False
    assert checkpoint["decision"] == "FALLBACK_DETERMINISTIC" and checkpoint["used_fallback"] is True
    assert "jev" not in generated and service.status(session_id)["jev_checkpoint"] == checkpoint
    assert generated["technical_designer"]["unlocked"] is False  # Jev never approves
    with StateStore(info.state_db, info.workspace_id) as store:
        row = store.connection.execute(
            "SELECT subject_type, subject_id, status FROM jev_evaluation WHERE id=?", (checkpoint["evaluation_id"],)).fetchone()
    assert row is not None and (row["subject_type"], row["subject_id"]) == ("TASK", session_id)


def test_a_session_cannot_overwrite_the_document_another_session_put_under_review(tmp_path: Path) -> None:
    """Found in a Coordinator-driven real-model run: a throwaway session overwrote the document awaiting approval."""
    project, info = workspace(tmp_path)
    first = service_for(info)
    reviewed = first.start("Build order intake")["session_id"]
    complete_discovery(first, reviewed)
    generated = first.generate(reviewed, proposal=load_proposal(), run_workers=False)
    document = (project / "001-USE-CASES.md").read_bytes()

    second = service_for(info)
    other = second.start("A throwaway probe")["session_id"]
    complete_discovery(second, other)
    with pytest.raises(DridhError) as refused:
        second.generate(other, proposal=load_proposal(), run_workers=False)
    assert refused.value.code is ErrorCode.CONFIGURATION_CONFLICT and reviewed in refused.value.message
    assert (project / "001-USE-CASES.md").read_bytes() == document
    challenge = first.approval_challenge(reviewed)  # the reviewed document is still approvable
    assert challenge["revision_id"] == generated["revision_id"]
