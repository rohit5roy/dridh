"""Seed a fully reconciled, fixture-approved baseline for Phase 13 tests.

The use-case and TDD documents are deterministic contract fixtures. Their review
approvals are inserted only into isolated temporary workspaces; no production
workspace or user approval record is touched.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dridh.designer.service import SESSION_SCHEMA as DESIGNER_SCHEMA
from dridh.designer.service import DesignerService
from dridh.document_contracts.validator import STANDARDS_VERSION, validate_tdd, validate_use_cases
from dridh.ideator.service import SESSION_SCHEMA as IDEATOR_SCHEMA
from dridh.ideator.service import IdeatorService
from dridh.state.design import DesignRepository, validated_tdd_model
from dridh.state.documents import DocumentRepository, content_hash
from dridh.state.reviews import ReviewRepository
from dridh.state.store import StateStore
from dridh.state.usecases import UseCaseRepository, validated_model
from dridh.workspace.layout import WorkspaceInfo, init_workspace

IDEATOR_SESSION = "inquiry-000000000013"
DESIGNER_SESSION = "designer-000000000013"


def seed_approved_baseline(project: Path) -> tuple[WorkspaceInfo, str]:
    """Create a new-codebase workspace with approved Use Case and TDD revisions."""
    project = Path(project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    fixture_dir = Path(__file__).parent / "fixtures"
    use_case_text = (fixture_dir / "planner-use-cases.md").read_text(encoding="utf-8")
    tdd_text = (fixture_dir / "planner-feature-tdd.md").read_text(encoding="utf-8")

    use_case_validation = validate_use_cases(use_case_text, file="001-USE-CASES.md")
    if not use_case_validation.ok:
        raise AssertionError(f"Phase 13 fixture Use Cases are invalid: {use_case_validation.errors[:5]}")
    tdd_validation = validate_tdd(tdd_text, file="feature_tdd.md")
    if not tdd_validation.ok:
        raise AssertionError(f"Phase 13 fixture TDD is invalid: {tdd_validation.errors[:5]}")

    use_case_path = project / "001-USE-CASES.md"
    tdd_path = project / "feature_tdd.md"
    use_case_path.write_text(use_case_text, encoding="utf-8")
    tdd_path.write_text(tdd_text, encoding="utf-8")

    info, _created = init_workspace(project, mode="new")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with StateStore(info.state_db, info.workspace_id) as store:
        with store.transaction() as connection:
            documents = DocumentRepository(connection, info.workspace_id)
            use_case_revision, _ = documents.record_revision(
                relative_path="001-USE-CASES.md",
                document_type="use_cases",
                content=use_case_text,
                standards_version=STANDARDS_VERSION,
                source_path=str(use_case_path),
            )
            documents.record_validation(
                revision_id=use_case_revision.id,
                validator="ideator.use_cases",
                validator_version=f"dridh-{STANDARDS_VERSION}",
                result=use_case_validation,
            )
            use_case_meta = documents.document_metadata("001-USE-CASES.md")
            assert use_case_meta is not None
            use_case_model = validated_model(use_case_text, "001-USE-CASES.md")
            UseCaseRepository(connection, info.workspace_id).replace_projection(
                document_id=str(use_case_meta["id"]),
                revision_id=use_case_revision.id,
                model=use_case_model,
            )
            reviews = ReviewRepository(connection, info.workspace_id)
            use_case_review_id = reviews.create_review(
                document_id=str(use_case_meta["id"]),
                revision_id=use_case_revision.id,
                reviewer="Phase 13 isolated fixture",
                actor="test:phase13-fixture",
            )
            use_case_approval_id = reviews.decide(
                use_case_review_id,
                decision="APPROVED",
                reviewer="Phase 13 isolated fixture",
                reason="Fixture baseline for isolated Planner acceptance testing.",
                actor="test:phase13-fixture",
                revision_id=use_case_revision.id,
            )

            tdd_revision, _ = documents.record_revision(
                relative_path="feature_tdd.md",
                document_type="tdd",
                content=tdd_text,
                standards_version=STANDARDS_VERSION,
                source_path=str(tdd_path),
            )
            documents.record_validation(
                revision_id=tdd_revision.id,
                validator="designer.tdd",
                validator_version="1",
                result=tdd_validation,
            )
            tdd_meta = documents.document_metadata("feature_tdd.md")
            assert tdd_meta is not None
            tdd_model = validated_tdd_model(tdd_text, "feature_tdd.md")
            DesignRepository(connection, info.workspace_id).replace_tdd(
                document_id=str(tdd_meta["id"]),
                revision_id=tdd_revision.id,
                model=tdd_model,
            )
            tdd_review_id = reviews.create_review(
                document_id=str(tdd_meta["id"]),
                revision_id=tdd_revision.id,
                reviewer="Phase 13 isolated fixture",
                actor="test:phase13-fixture",
            )
            tdd_approval_id = reviews.decide(
                tdd_review_id,
                decision="APPROVED",
                reviewer="Phase 13 isolated fixture",
                reason="Fixture TDD baseline for isolated Planner acceptance testing.",
                actor="test:phase13-fixture",
                revision_id=tdd_revision.id,
            )

    ideator = IdeatorService(info)
    ideator._save({
        "schema": IDEATOR_SCHEMA,
        "schema_version": 1,
        "session_id": IDEATOR_SESSION,
        "status": "APPROVED",
        "objective": "Order Intake approved fixture baseline.",
        "existing_context": "",
        "questions": [],
        "proposal": None,
        "discovery_summary": None,
        "generated": {
            "path": "001-USE-CASES.md",
            "revision_id": use_case_revision.id,
            "sha256": content_hash(use_case_text),
            "review_id": use_case_review_id,
            "document_content": use_case_text,
        },
        "handoff": None,
        "jev": None,
        "review_id": use_case_review_id,
        "approval_id": use_case_approval_id,
        "approved_by": "Phase 13 isolated fixture",
        "approved_at": now,
    })

    designer = DesignerService(info)
    designer._save({
        "schema": DESIGNER_SCHEMA,
        "schema_version": 1,
        "session_id": DESIGNER_SESSION,
        "status": "APPROVED",
        "feature": "Order Intake",
        "feature_slug": "order-intake",
        "objective": "Produce the approved Order Intake technical design.",
        "ideator_session_id": IDEATOR_SESSION,
        "source": {
            "path": "001-USE-CASES.md",
            "document_id": str(use_case_meta["id"]),
            "revision_id": use_case_revision.id,
            "sha256": content_hash(use_case_text),
        },
        "existing_context": "",
        "code_reacher_evidence": "New codebase fixture; no existing repository index is required.",
        "inquirer": {"summary": "Fixture technical design facts.", "key_facts": [], "gaps": []},
        "questions": [],
        "generated": {
            "path": "feature_tdd.md",
            "revision_id": tdd_revision.id,
            "sha256": content_hash(tdd_text),
            "review_id": tdd_review_id,
            "components": [],
            "traceability": [],
        },
        "handoff": {"summary": "Fixture TDD baseline.", "concerns": []},
        "review_id": tdd_review_id,
        "approval_id": tdd_approval_id,
        "approved_by": "Phase 13 isolated fixture",
        "approved_at": now,
    })

    use_case_gate = ideator.gate(IDEATOR_SESSION)
    tdd_gate = designer.gate(DESIGNER_SESSION)
    if not use_case_gate.get("unlocked"):
        raise AssertionError(f"Fixture Use Case gate did not unlock: {use_case_gate}")
    if not tdd_gate.get("unlocked"):
        raise AssertionError(f"Fixture TDD gate did not unlock: {tdd_gate}")
    return info, DESIGNER_SESSION


def tick_review(workspace: WorkspaceInfo, review_id: str) -> None:
    """Stand in for the human ticking every review checklist item (approval is refused until that is done)."""
    from dridh.state.reviews import ReviewRepository
    from dridh.state.store import StateStore

    with StateStore(workspace.state_db, workspace.workspace_id) as store, store.transaction() as connection:
        repository = ReviewRepository(connection, workspace.workspace_id)
        for item in repository.get_review(review_id)["checklist"]:
            repository.set_checklist(review_id, item["item"], True, actor="human:test")
