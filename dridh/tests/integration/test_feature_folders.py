"""Each feature keeps its documents in its own numbered folder, with upper-case numbered names.

    docs/dridh/001-ORDER-INTAKE/001-USE-CASES.md, 002-TECHNICAL-DESIGN.md, 003-CENTRAL-PLAN-INDEX.md, ...

A feature created before feature folders (no folder record) keeps the names it was created with.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from dridh.coordinator.lifecycle import FeatureLifecycle
from dridh.designer.service import TOPICS as DESIGNER_TOPICS
from dridh.designer.service import DesignerService
from dridh.document_contracts.validator import classify_document
from dridh.ideator.service import TOPICS as IDEATOR_TOPICS
from dridh.ideator.service import IdeatorService
from dridh.jev import JevClient
from dridh.ledger.drafts import ledger_path
from dridh.planner.service import PlannerService
from dridh.state.store import StateStore
from dridh.workspace.documents_layout import LEGACY, DocumentLayout, feature_layout, upper_slug
from dridh.workspace.layout import init_workspace
from tests.e2e.planner_test_helpers import tick_review
from tests.unit.test_designer_service import Phase12Workers

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
_argv, sys.argv = sys.argv, ["stub_provider", "0", "/dev/null", "/dev/null"]
try:
    import stub_provider  # noqa: E402
finally:
    sys.argv = _argv


class PlannerWorkers:
    def run_sync(self, *, agent: str, prompt: str, **_: object):
        if agent == "dridh/workers/planner-indexer":
            reply = stub_provider._phase13_indexer(prompt)
        elif agent == "dridh/workers/planner-component":
            reply = stub_provider._phase13_component(prompt)
        else:
            reply = stub_provider._phase13_testing(prompt)
        return SimpleNamespace(text=json.dumps(reply), session_id="ses_folders00001")


def _project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project, info


def _approved_use_cases(info, feature: str) -> str:
    ideator = IdeatorService(info, runner=Phase12Workers(), jev_client=JevClient(base_url=""))
    session = ideator.start("Create an order intake capability.")["session_id"]
    FeatureLifecycle(info).bind_session(feature, "ideator_session_id", session, actor="test")
    for topic in IDEATOR_TOPICS:
        ideator.record_answer(session, topic=topic, question=f"Clarify {topic}.", answer=f"Confirmed {topic.replace('_', ' ')}.",
                              source="Product owner")
    ideator.generate(session)
    challenge = ideator.approval_challenge(session)
    tick_review(info, challenge["review_id"])
    ideator.approve(session, reviewer="Product Owner", confirmation=challenge["confirmation"])
    return session


def _approved_design(info, ideator_session: str) -> str:
    designer = DesignerService(info, runner=Phase12Workers())
    session = designer.start(ideator_session, feature="order-intake", existing_context="New service.",
                             code_reacher_evidence="Code Reacher: new codebase, nothing to reuse.")["session_id"]
    for topic, _question, _impact in DESIGNER_TOPICS:
        designer.answer(session, topic=topic, answer=f"Confirmed {topic}.", source="Engineering owner", impact="Sequencing.")
    designer.generate(session)
    challenge = designer.approval_challenge(session)
    tick_review(info, challenge["review_id"])
    designer.approve(session, reviewer="Engineering Owner", confirmation=challenge["confirmation"])
    return session


def test_names_are_upper_case_and_numbered_in_stage_order() -> None:
    layout = DocumentLayout("docs/dridh/001-ORDER-INTAKE")
    assert layout.use_cases() == "docs/dridh/001-ORDER-INTAKE/001-USE-CASES.md"
    assert layout.design("order-intake") == "docs/dridh/001-ORDER-INTAKE/002-TECHNICAL-DESIGN.md"
    assert [Path(p).name for p in (layout.central_plan(), layout.test_plan(), layout.project_structure(),
                                   layout.planning_checklist(), layout.ledger())] == [
        "003-CENTRAL-PLAN-INDEX.md", "004-TEST-PLAN.md", "005-PROJECT-STRUCTURE-PLAN.md",
        "006-PLANNING-REVIEW-CHECKLIST.md", "007-LEDGER.md"]
    assert layout.component_master("CMP-01") == "docs/dridh/001-ORDER-INTAKE/components/CMP-01/001-COMPONENT-MASTER.md"
    assert layout.phase_name(0, 1, "Order service") == "002-PHASE-1-ORDER-SERVICE.md"
    assert layout.phase_name(1, 2, "order_service/v2") == "003-PHASE-2-ORDER-SERVICE-V2.md"
    assert upper_slug("  ") == "COMPONENT"
    # The legacy layout is exactly what features from before feature folders already have on disk.
    assert (LEGACY.use_cases(), LEGACY.design("order-intake"), LEGACY.central_plan(), LEGACY.ledger()) == (
        "001-USE-CASES.md", "order-intake_tdd.md", ".opencode/dridh/documents/CENTRAL-PLAN-INDEX.md",
        ".opencode/dridh/documents/LEDGER.md")
    assert LEGACY.phase_name(0, 1, "Order service") == "PHASE-1-order-service.md"


def test_numbered_names_are_recognised_as_the_same_kinds_of_document() -> None:
    for name, kind in (("004-TEST-PLAN.md", "test_plan"), ("003-CENTRAL-PLAN-INDEX.md", "central_plan"),
                       ("001-COMPONENT-MASTER.md", "component_master"), ("002-TECHNICAL-DESIGN.md", "tdd"),
                       ("001-USE-CASES.md", "use_cases"), ("002-PHASE-1-ORDER-SERVICE.md", "phase_file"),
                       ("TEST-PLAN.md", "test_plan"), ("order-intake_tdd.md", "tdd")):
        assert classify_document(name) == kind, name


def test_features_get_numbered_folders_in_creation_order(tmp_path: Path) -> None:
    _project_root, info = _project(tmp_path)
    lifecycle = FeatureLifecycle(info)
    first = lifecycle.create_feature("Order intake", actor="test")
    second = lifecycle.create_feature("Refunds & returns", actor="test")
    assert first["documents_folder"] == "docs/dridh/001-ORDER-INTAKE"
    assert second["documents_folder"] == "docs/dridh/002-REFUNDS-RETURNS"


def test_a_feature_writes_every_document_into_its_own_folder(tmp_path: Path) -> None:
    project, info = _project(tmp_path)
    feature = FeatureLifecycle(info).create_feature("Order intake", actor="test")
    folder = project / "docs" / "dridh" / "001-ORDER-INTAKE"

    ideator_session = _approved_use_cases(info, feature["slug"])
    assert (folder / "001-USE-CASES.md").is_file()
    assert not (project / "001-USE-CASES.md").exists()

    design_session = _approved_design(info, ideator_session)
    design = folder / "002-TECHNICAL-DESIGN.md"
    assert design.is_file() and not list(project.glob("*_tdd.md"))
    assert "**Source Use Case Document:** 001-USE-CASES.md" in design.read_text(encoding="utf-8")

    planner = PlannerService(info, runner=PlannerWorkers())
    plan_session = planner.start(design_session, model="m")["session_id"]
    generated = planner.generate(plan_session, model="m")
    assert generated["status"] == "GENERATED", generated.get("last_error")
    written = sorted(item["path"] for item in planner._load(plan_session)["generated"]["files"])
    assert all(path.startswith("docs/dridh/001-ORDER-INTAKE/") for path in written), written
    names = {Path(path).name for path in written}
    assert {"003-CENTRAL-PLAN-INDEX.md", "004-TEST-PLAN.md", "005-PROJECT-STRUCTURE-PLAN.md",
            "006-PLANNING-REVIEW-CHECKLIST.md", "001-COMPONENT-MASTER.md"} <= names
    assert any(name.startswith("002-PHASE-1-") for name in names)
    for path in written:
        name = Path(path).name
        assert name == name.upper().replace(".MD", ".md") and name[:3].isdigit(), name
    assert not (project / ".opencode" / "dridh" / "documents").exists()
    # The component master lists its phase files by their numbered names, and links the design in the same folder.
    master = next(folder.glob("components/*/001-COMPONENT-MASTER.md")).read_text(encoding="utf-8")
    assert "- 002-PHASE-1-" in master and "](../../002-TECHNICAL-DESIGN.md#" in master
    # The ledger of the feature in progress lives in the same folder.
    assert ledger_path(info) == "docs/dridh/001-ORDER-INTAKE/007-LEDGER.md"


def test_a_feature_from_before_feature_folders_keeps_its_names(tmp_path: Path) -> None:
    _project_root, info = _project(tmp_path)
    feature = FeatureLifecycle(info).create_feature("Order intake", actor="test")
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        connection.execute("DELETE FROM feature_folder")  # as a feature created by an earlier version has no record
    with StateStore(info.state_db, info.workspace_id) as store:
        assert feature_layout(store.connection, info.workspace_id, feature["id"]) == LEGACY
    assert FeatureLifecycle(info)._get(feature["slug"])["documents_folder"] is None
    assert ledger_path(info) == ".opencode/dridh/documents/LEDGER.md"


def test_a_second_feature_does_not_overwrite_the_first_features_use_cases(tmp_path: Path) -> None:
    project, info = _project(tmp_path)
    lifecycle = FeatureLifecycle(info)
    first = lifecycle.create_feature("Order intake", actor="test")
    second = lifecycle.create_feature("Refunds", actor="test")
    _approved_use_cases(info, first["slug"])
    before = (project / "docs/dridh/001-ORDER-INTAKE/001-USE-CASES.md").read_text(encoding="utf-8")
    _approved_use_cases(info, second["slug"])
    assert (project / "docs/dridh/002-REFUNDS/001-USE-CASES.md").is_file()
    assert (project / "docs/dridh/001-ORDER-INTAKE/001-USE-CASES.md").read_text(encoding="utf-8") == before
