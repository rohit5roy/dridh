"""End-to-end Phase 11 acceptance: idea -> discovery -> Use Case -> approval -> Designer gate."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from dridh.ideator.service import TOPICS, IdeatorService
from dridh.jev import JevClient
from dridh.state.documents import DocumentRepository
from dridh.state.store import StateStore
from dridh.state.usecases import UseCaseRepository
from dridh.workspace.layout import init_workspace
from tests.e2e.planner_test_helpers import tick_review

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ideator"


class DeterministicWorkers:
    def run_sync(self, *, agent: str, prompt: str, model: str | None = None, title: str | None = None):
        if agent == "dridh/workers/ideator-inquirer":
            response = {
                "summary": "Customers need a reliable order intake path.",
                "key_facts": ["A customer submits an order.", "The system reports validation errors."],
                "gaps": [],
                "unresolved": [],
            }
        elif agent == "dridh/workers/ideator-doc-generator":
            response = json.loads((FIXTURES / "valid-proposal.json").read_text(encoding="utf-8"))
        elif agent == "dridh/workers/ideator-handoff":
            response = {"summary": "The discovery and Use Case document are aligned.", "concerns": []}
        else:
            raise AssertionError(f"Unexpected worker: {agent}")
        return SimpleNamespace(text=json.dumps(response))


def test_ideator_end_to_end_from_initial_idea_to_designer_unlock(tmp_path: Path) -> None:
    project = tmp_path / "customer-project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    service = IdeatorService(
        info,
        runner=DeterministicWorkers(),
        jev_client=JevClient(base_url=""),
    )

    started = service.start(
        "Create an order intake workflow for customers.",
        existing_context="Current process: customers email orders to the operations team.",
    )
    session_id = started["session_id"]

    answers = {
        "problem": "Orders arrive through unstructured emails and are hard to track.",
        "why_for_whom": "Customers need clear confirmation; operations need a reliable queue.",
        "solution": "Provide a structured order submission path.",
        "scope": "Capture an order and return accepted or validation-rejected status.",
        "out_of_scope": "Fulfilment and shipping are excluded.",
        "actors": "Customer is the primary actor.",
        "journey": "Customer confirms a completed order form.",
        "success_flow": "The system accepts and records a valid order.",
        "alternate_flow": "No additional alternate path is currently required.",
        "failure_handling": "Invalid order details are rejected with actionable feedback.",
        "boundary_cases": "Empty order details must not be accepted.",
        "business_rules": "Only valid orders may be recorded as accepted.",
        "priority": "High priority for the first release.",
        "assumptions": "The customer has already selected the products.",
    }
    for topic in TOPICS:
        service.record_answer(
            session_id,
            topic=topic,
            question=f"Clarify {topic.replace('_', ' ')}.",
            answer=answers[topic],
            source="Product owner",
        )

    progress = service.status(session_id)
    assert progress["topics_missing"] == []
    assert progress["parity"]["ok"] is True

    generated = service.generate(session_id)
    assert generated["status"] == "IN_REVIEW"
    assert generated["validation"]["ok"] is True
    assert generated["technical_designer"]["unlocked"] is False
    assert generated["handoff"]["concerns"] == []

    json_path = info.root / "artifacts" / "ideator" / session_id / "inquiry.json"
    notes_path = info.root / "artifacts" / "ideator" / session_id / "INQUIRY-NOTES.md"
    checklist_path = info.root / "artifacts" / "ideator" / session_id / "REVIEW-CHECKLIST.md"
    inquiry = json.loads(json_path.read_text(encoding="utf-8"))
    notes = notes_path.read_text(encoding="utf-8")
    canonical = (project / "001-USE-CASES.md").read_text(encoding="utf-8")
    assert len(inquiry["questions"]) == len(TOPICS)
    assert "Current process: customers email orders" in notes
    assert "STAGING — NOT THE CANONICAL APPROVED USE-CASE DOCUMENT" in notes
    assert "ACT-01" in canonical and "UC-01" in canonical and "SF-01.1" in canonical
    assert checklist_path.is_file()
    assert service.parity(session_id)["ok"] is True

    with StateStore(info.state_db, info.workspace_id) as store:
        documents = DocumentRepository(store.connection, info.workspace_id)
        metadata = documents.document_metadata("001-USE-CASES.md")
        revision = documents.current_revision("001-USE-CASES.md")
        assert metadata is not None and revision is not None
        marker = UseCaseRepository(store.connection, info.workspace_id).import_marker(str(metadata["id"]))
        assert marker is not None and marker["source_revision_id"] == revision.id
        stored = UseCaseRepository(store.connection, info.workspace_id).stored_model(str(metadata["id"]))
        assert len(stored.actors) == 1
        assert len(stored.use_cases) == 1
        assert len(stored.flows) == 2
        review = store.connection.execute(
            "SELECT id, status, revision_id FROM review WHERE id = ?",
            (generated["review_id"],),
        ).fetchone()
        assert review is not None and review["status"] == "OPEN"
        assert review["revision_id"] == revision.id

    # The gate is closed while the human review is pending.
    assert service.gate(session_id)["unlocked"] is False
    challenge = service.approval_challenge(session_id)
    tick_review(service.workspace, challenge["review_id"])
    approved = service.approve(
        session_id,
        reviewer="Product Owner",
        confirmation=challenge["confirmation"],
    )
    assert approved["status"] == "APPROVED"
    assert approved["technical_designer"]["unlocked"] is True
    assert approved["technical_designer"]["source_revision_id"] == generated["revision_id"]
    assert service.gate(session_id)["unlocked"] is True

    with StateStore(info.state_db, info.workspace_id) as store:
        approval = store.connection.execute(
            "SELECT decision, reviewer, revision_id FROM human_approval WHERE review_id = ?",
            (generated["review_id"],),
        ).fetchone()
        assert approval is not None
        assert approval["decision"] == "APPROVED"
        assert approval["reviewer"] == "Product Owner"
        assert approval["revision_id"] == generated["revision_id"]
