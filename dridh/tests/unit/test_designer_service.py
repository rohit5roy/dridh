"""Phase 12 Technical Designer lifecycle, traceability and approval-gate tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.designer.service import TOPICS, DesignerService
from dridh.document_contracts.validator import TDD_SECTIONS, validate_tdd
from dridh.ideator.service import TOPICS as IDEATOR_TOPICS
from dridh.ideator.service import IdeatorService
from dridh.jev import JevClient
from dridh.state.documents import DocumentRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace
from tests.e2e.planner_test_helpers import tick_review

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ideator"


class Phase12Workers:
    def run_sync(self, *, agent: str, prompt: str, model: str | None = None, title: str | None = None):
        if agent == "dridh/workers/ideator-inquirer":
            result = {
                "summary": "The approved Use Case describes customer order intake.",
                "key_facts": ["A customer submits an order."],
                "gaps": [],
                "unresolved": [],
            }
        elif agent == "dridh/workers/ideator-doc-generator":
            result = json.loads((FIXTURES / "valid-proposal.json").read_text(encoding="utf-8"))
        elif agent == "dridh/workers/ideator-handoff":
            result = {"summary": "Use Case handoff ready for human review.", "concerns": []}
        elif agent == "dridh/workers/designer-inquirer":
            result = {
                "summary": "The approved order-intake scope needs a verified component and integration design.",
                "key_facts": ["The customer submits an order and receives an outcome."],
                "gaps": ["Existing repository placement requires Code Reacher evidence or owner confirmation."],
                "recommended_questions": [
                    {"topic": "repositories", "question": "Which repository owns the order-intake capability?"},
                ],
            }
        elif agent == "dridh/workers/designer-doc-generator":
            result = {
                "sections": {
                    "problem_resources": "Customers need an auditable order intake path; operations need a clear outcome.",
                    "design_summary": "A single Order Intake component validates and records order submissions.",
                    "system_context": "The customer invokes the order intake capability. Existing system boundaries remain OPEN until repository evidence is confirmed.",
                    "internal_external_flow": "The request is validated before it is persisted; rejected input does not become an accepted order.",
                    "data_ownership": "The Order Intake component owns the accepted order record. Storage keys and retention require owner confirmation.",
                    "api_design": "N/A — the current approved scope does not identify a public API contract; confirm with the API owner.",
                    "events": "N/A — no event publication is required by the supplied approved flow; confirm with the integration owner.",
                    "data_model": "An order record must retain its canonical ID, validation state and creation time. Physical schema and indexes remain OPEN.",
                    "quality_approach": "Test valid orders, invalid orders, duplicate submission, persistence failure and recovery using real dependencies where possible.",
                    "security_privacy": "Authenticate the submitting customer, authorise access to their orders and protect customer data at rest and in transit.",
                    "monitoring": "Track accepted/rejected counts, validation failures, persistence latency and dependency availability.",
                    "failure_recovery": "A persistence failure must not report success. Retry semantics must be idempotent and observable.",
                    "migration": "N/A — no existing order data migration was specified; confirm with the data owner.",
                    "rollback": "Disable the new intake path and retain the existing submission route until data consistency is verified.",
                },
                "components": [
                    {
                        "key": "order-intake",
                        "name": "Order Intake",
                        "status": "NEW",
                        "responsibility": "Validate an order submission and persist the accepted order outcome.",
                        "owner": "OPEN — product/engineering owner confirmation required",
                        "inputs_outputs": "Input: customer order submission. Output: accepted or validation-rejected outcome.",
                        "dependencies": "Identity provider and persistent order store; exact services need Code Reacher evidence.",
                        "use_case_ids": ["UC-01"],
                        "flow_ids": ["SF-01.1", "EF-01.1"],
                    }
                ],
                "decisions": [],
                "risks": [
                    {
                        "title": "Duplicate order submission",
                        "description": "A client retry could create more than one order unless the submission is idempotent.",
                        "mitigation": "Define an idempotency key and test repeated submissions against the real persistence layer.",
                    }
                ],
                "traceability": [
                    {
                        "uc_id": "UC-01",
                        "flow_ids": ["SF-01.1", "EF-01.1"],
                        "component_keys": ["order-intake"],
                        "decision_keys": [],
                        "api_event": "N/A",
                    }
                ],
            }
        elif agent == "dridh/workers/designer-handoff":
            result = {
                "summary": "Both approved flows are mapped to the Order Intake component.",
                "concerns": ["Confirm the persistent store and API ownership before implementation."],
                "open_blockers": ["Architecture owner must confirm repository and data-store placement."],
            }
        else:
            raise AssertionError(f"Unexpected worker: {agent}")
        return SimpleNamespace(text=json.dumps(result))


def workspace(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project, info


def approved_ideator(info: Any) -> str:
    ideator = IdeatorService(info, runner=Phase12Workers(), jev_client=JevClient(base_url=""))
    session_id = ideator.start(
        "Create an order intake capability.",
        existing_context="Customers currently submit orders by email.",
    )["session_id"]
    answers = {
        "problem": "Order intake by email is difficult to track.",
        "why_for_whom": "Customers need clear confirmation and operations needs a reliable queue.",
        "solution": "Provide a structured order submission flow.",
        "scope": "Accept a valid order and report validation rejection for invalid data.",
        "out_of_scope": "Fulfilment and shipping.",
        "actors": "Customer is the primary actor.",
        "journey": "Customer confirms an order submission.",
        "success_flow": "The system records a valid order as accepted.",
        "alternate_flow": "No additional alternate flow is required for the initial release.",
        "failure_handling": "Invalid details are rejected with actionable feedback.",
        "boundary_cases": "An empty order must not be accepted.",
        "business_rules": "A rejected order is never recorded as accepted.",
        "priority": "High priority.",
        "assumptions": "The customer has already selected the items.",
    }
    for topic in IDEATOR_TOPICS:
        ideator.record_answer(
            session_id,
            topic=topic,
            question=f"Clarify {topic.replace('_', ' ')}.",
            answer=answers[topic],
            source="Product owner",
        )
    generated = ideator.generate(session_id)
    challenge = ideator.approval_challenge(session_id)
    tick_review(ideator.workspace, challenge["review_id"])
    approved = ideator.approve(
        session_id,
        reviewer="Product Owner",
        confirmation=challenge["confirmation"],
    )
    assert generated["status"] == "IN_REVIEW"
    assert approved["status"] == "APPROVED"
    assert ideator.gate(session_id)["unlocked"] is True
    return session_id


def complete_designer(info: Any, *, evidence: str = "Code Reacher: existing service map and repository inventory verified.") -> tuple[DesignerService, str]:
    ideator_session_id = approved_ideator(info)
    designer = DesignerService(info, runner=Phase12Workers())
    started = designer.start(
        ideator_session_id,
        feature="order-intake",
        existing_context="The first release accepts a valid order and rejects invalid details.",
        code_reacher_evidence=evidence,
    )
    session_id = started["session_id"]
    assert started["status"] == "INQUIRY"
    assert started["topics_missing"] == [topic for topic, _, _ in TOPICS]
    for topic, _question, _impact in TOPICS:
        designer.answer(
            session_id,
            topic=topic,
            answer=f"Confirmed technical input for {topic.replace('_', ' ')}.",
            source="Engineering owner",
            impact="Affects component/API/data ownership and implementation sequencing.",
        )
    return designer, session_id


def test_technical_designer_refuses_unapproved_use_case_baseline(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    ideator = IdeatorService(info, runner=Phase12Workers(), jev_client=JevClient(base_url=""))
    session_id = ideator.start("Build order intake")["session_id"]
    designer = DesignerService(info, runner=Phase12Workers())

    with pytest.raises(DridhError) as locked:
        designer.start(session_id, feature="order-intake")
    assert locked.value.code is ErrorCode.DEPENDENCY_NOT_READY


def test_planner_is_blocked_after_use_case_approval_until_tdd_is_human_approved(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    approved_ideator(info)

    with pytest.raises(DridhError) as blocked:
        DesignerService.require_planner_gate(info)
    assert blocked.value.code is ErrorCode.DEPENDENCY_NOT_READY
    assert "TDD" in blocked.value.message


def test_designer_inquiry_generation_table13_reconciliation_and_terminal_approval(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    designer, session_id = complete_designer(info)

    status = designer.status(session_id)
    assert status["topics_missing"] == []
    assert status["parity"]["ok"] is True
    assert status["gate"]["unlocked"] is False

    generated = designer.generate(session_id)
    assert generated["status"] == "IN_REVIEW"
    assert generated["parity"]["ok"] is True
    assert generated["handoff"]["concerns"]
    assert generated["handoff"]["open_blockers"]
    assert generated["gate"]["unlocked"] is False
    assert generated["jev_checkpoint"]["advisory"] is True and generated["jev_checkpoint"]["flagged"] is False  # recorded, never a gate

    tdd_path = project / "order-intake_tdd.md"
    document = tdd_path.read_text(encoding="utf-8")
    result = validate_tdd(document, file=tdd_path.name)
    assert result.ok, result.errors
    assert all(f"## {section}" in document for section in TDD_SECTIONS)
    notes = (info.root / "artifacts" / "designer" / session_id / "TECHNICAL-INQUIRY-NOTES.md").read_text(encoding="utf-8")
    assert "STAGING — NOT THE CANONICAL TDD" in notes
    assert "| # | Question | Asked In | Answer | Source | Impact |" in document
    assert "| UC ID | Flow IDs | Catering Component(s) | API / Event | Design Decision (DD-N) |" in document
    assert "UC-01" in document and "SF-01.1" in document and "EF-01.1" in document
    assert "### CMP-01: Order Intake" in document
    assert "### R1: Duplicate order submission" in document
    assert "<aside" not in document and "<!--" not in document

    with StateStore(info.state_db, info.workspace_id) as store:
        metadata = DocumentRepository(store.connection, info.workspace_id).document_metadata(tdd_path.name)
        revision = DocumentRepository(store.connection, info.workspace_id).current_revision(tdd_path.name)
        assert metadata is not None and revision is not None
        assert revision.content_sha256 == generated["document"]["sha256"]
        review = store.connection.execute(
            "SELECT status, revision_id FROM review WHERE id=?",
            (generated["review_id"],),
        ).fetchone()
        assert review is not None and review["status"] == "OPEN"
        assert review["revision_id"] == revision.id

    challenge = designer.approval_challenge(session_id)
    tick_review(designer.workspace, challenge["review_id"])
    approved = designer.approve(
        session_id,
        reviewer="Engineering Owner",
        confirmation=challenge["confirmation"],
    )
    assert approved["status"] == "APPROVED"
    assert approved["gate"]["unlocked"] is True
    planner_gate = DesignerService.require_planner_gate(info)
    assert planner_gate is not None and planner_gate["unlocked"] is True


def test_tdd_gate_closes_when_document_changes_after_approval(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    designer, session_id = complete_designer(info)
    designer.generate(session_id)
    challenge = designer.approval_challenge(session_id)
    tick_review(designer.workspace, challenge["review_id"])
    designer.approve(session_id, reviewer="Engineering Owner", confirmation=challenge["confirmation"])

    target = project / "order-intake_tdd.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nManual edit after approval.\n", encoding="utf-8")
    gate = designer.gate(session_id)
    assert gate["unlocked"] is False
    assert "SHA-256" in gate["reason"]


def test_tdd_gate_closes_when_approved_use_case_source_changes(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    designer, session_id = complete_designer(info)
    designer.generate(session_id)
    challenge = designer.approval_challenge(session_id)
    tick_review(designer.workspace, challenge["review_id"])
    designer.approve(session_id, reviewer="Engineering Owner", confirmation=challenge["confirmation"])

    canonical = project / "001-USE-CASES.md"
    canonical.write_text(canonical.read_text(encoding="utf-8") + "\nManual requirement change.\n", encoding="utf-8")
    gate = designer.gate(session_id)
    assert gate["unlocked"] is False
    assert "Use Case" in gate["reason"]


def test_tdd_traceability_must_cover_every_approved_flow(tmp_path: Path) -> None:
    _, info = workspace(tmp_path)
    ideator_session_id = approved_ideator(info)
    designer = DesignerService(info, runner=Phase12Workers())
    source = designer.start(ideator_session_id, feature="order-intake")
    payload = designer._load(source["session_id"])
    source_record = designer._validate_source_current(payload)
    invalid = {
        "components": [{
            "key": "order-intake", "name": "Order Intake", "status": "NEW",
            "responsibility": "Accept an order", "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"],
        }],
        "decisions": [],
        "risks": [],
        "sections": {},
        "traceability": [{
            "uc_id": "UC-01", "flow_ids": ["SF-01.1"],
            "component_keys": ["order-intake"], "decision_keys": [], "api_event": "N/A",
        }],
    }
    with pytest.raises(DridhError) as error:
        designer._validate_generation(invalid, source_record)
    assert error.value.code is ErrorCode.VALIDATION_FAILED
    assert "EF-01.1" in error.value.details


def test_every_designer_worker_gets_the_model_name_and_the_handoff_actually_runs(tmp_path: Path) -> None:
    """Regression: generate() reused `model` for the parsed TDD, so the Handoff call got an object as --model and failed."""
    _, info = workspace(tmp_path)
    designer, session_id = complete_designer(info)
    seen: list[tuple[str, object]] = []
    workers = designer.runner

    class Recording:
        def run_sync(self, **kwargs):
            seen.append((kwargs["agent"], kwargs.get("model")))
            assert kwargs.get("model") is None or isinstance(kwargs["model"], str), kwargs.get("model")
            return workers.run_sync(**kwargs)

    designer.runner = Recording()
    generated = designer.generate(session_id, model="opencode/test-model")
    assert [agent for agent, _ in seen] == ["dridh/workers/designer-doc-generator", "dridh/workers/designer-handoff"]
    assert {model for _, model in seen} == {"opencode/test-model"}
    assert not any("subprocess argument" in concern for concern in generated["handoff"]["concerns"])
    assert generated["handoff"]["summary"] != "Automated Handoff review did not complete; human review remains mandatory."


def test_an_existing_codebase_design_gets_code_reacher_evidence_from_the_index(tmp_path: Path) -> None:
    """Found in a real-model run: the Coordinator described the code in free text, so every repository fact in the TDD
    was marked unverified. For an existing codebase the evidence now comes from Dridh's own index."""
    from dridh.code_reacher.indexer import CodeIndexer

    project = tmp_path / "project"
    (project / "greeter").mkdir(parents=True)
    (project / "greeter" / "__init__.py").write_text("def greet(name: str) -> str:\n    return 'Hello, ' + name + '!'\n\n\ndef _hidden() -> None:\n    pass\n")
    info, _ = init_workspace(project, mode="existing")
    CodeIndexer(info).index(embed=False)
    ideator_id = approved_ideator(info)
    designer = DesignerService(info, runner=Phase12Workers())
    started = designer.start(ideator_id, feature="add-farewell")
    evidence = designer._load(started["session_id"])["code_reacher_evidence"]
    assert evidence.startswith("Code Reacher index run ") and "greeter/__init__.py [python]" in evidence
    assert "function greeter.greet: greet(name: str) -> str (greeter/__init__.py:1)" in evidence and "_hidden" not in evidence

    supplied = DesignerService(info, runner=Phase12Workers())._load  # an explicit caller-supplied evidence string still wins
    other = DesignerService(info, runner=Phase12Workers()).start(ideator_id, feature="other", code_reacher_evidence="Owner-verified facts.")
    assert supplied(other["session_id"])["code_reacher_evidence"] == "Owner-verified facts."
