"""Category B acceptance: the real Phase 13 Planner workers run via OpenCode/OpenRouter."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from dridh.integrations.opencode import OpenCodeRunner
from dridh.planner.service import PlannerService
from dridh.state.design import DesignRepository
from dridh.state.documents import DocumentRepository
from dridh.state.planning import PlanningRepository
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline
from tests.integration.live_replies import ReplyRecorder, failure_message

DEFAULT_MODEL = "openrouter/nvidia/nemotron-3.5-super-vl:free"
# The live workflow pins the model through DRIDH_PLANNER_LIVE_MODEL (a manual run may choose another).
MODEL = os.environ.get("DRIDH_PLANNER_LIVE_MODEL") or DEFAULT_MODEL
OPENCODE = os.environ.get("DRIDH_OPENCODE_BIN", "")
CONFIG = os.environ.get("DRIDH_REAL_OPENCODE_CONFIG", "")
WORKSPACE = os.environ.get("DRIDH_REAL_OPENCODE_WORKSPACE", "")
_MISSING = [
    name for name, value in (
        ("DRIDH_REAL_OPENCODE=1", os.environ.get("DRIDH_REAL_OPENCODE") == "1"),
        ("DRIDH_OPENCODE_BIN", bool(OPENCODE)),
        ("DRIDH_REAL_OPENCODE_CONFIG", bool(CONFIG)),
        ("DRIDH_REAL_OPENCODE_WORKSPACE", bool(WORKSPACE)),
        ("OPENROUTER_API_KEY", bool(os.environ.get("OPENROUTER_API_KEY"))),
        ("DRIDH_PLANNER_LIVE_MODEL", bool(os.environ.get("DRIDH_PLANNER_LIVE_MODEL")) and MODEL.startswith("openrouter/")),
    ) if not value
]
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(bool(_MISSING), reason="Category B (live Planner execution, not enabled): missing " + ", ".join(_MISSING)),
]


class RecordingOpenCodeRunner:
    """Record the exact agent/model CLI invocation while delegating to real OpenCode."""

    def __init__(self, runner: OpenCodeRunner) -> None:
        self.runner = runner
        self.calls: list[dict[str, Any]] = []

    def run_sync(self, **kwargs: Any):
        model = kwargs.get("model")
        if model != MODEL:
            raise AssertionError(f"Planner worker did not receive the required explicit model: {model!r}")
        arguments = self.runner._build_args(
            agent=kwargs["agent"],
            prompt=kwargs["prompt"],
            session_id=kwargs.get("session_id"),
            model=model,
            title=kwargs.get("title"),
        )
        model_index = arguments.index("--model") if "--model" in arguments else -1
        if model_index < 0 or arguments[model_index + 1] != MODEL:
            raise AssertionError("OpenCode CLI arguments do not explicitly select the NVIDIA OpenRouter model.")
        try:
            result = self.runner.run_sync(**kwargs)
        except Exception as exc:
            self.calls.append({"agent": kwargs["agent"], "model": model, "success": False, "error_type": type(exc).__name__})
            raise
        self.calls.append({
            "agent": kwargs["agent"],
            "model": model,
            "success": result.exit_code == 0,
            "duration_seconds": result.duration_seconds,
            "session_id": result.session_id,
        })
        return result


def test_planner_complete_pipeline_runs_all_workers_on_nvidia_openrouter_and_reconciles_sqlite(tmp_path: Path) -> None:
    assert os.environ.get("DRIDH_PLANNER_LIVE_MODEL") == MODEL, "Live workflow must pin the model under test."
    assert os.environ.get("OPENROUTER_API_KEY"), "OpenRouter credential must be present."
    project = Path(WORKSPACE) / "phase13-planner-live"
    project.mkdir(parents=True, exist_ok=True)
    info, designer_session_id = seed_approved_baseline(project)

    real_runner = OpenCodeRunner(
        executable=OPENCODE,
        workspace_root=project,
        config_dir=CONFIG,
        timeout=240,
        # OpenCodeRunner intentionally does not inherit the host environment.
        # Preserve the normal CI PATH/HOME/XDG runtime and inject the OpenRouter key.
        env={**os.environ, "OPENCODE_CONFIG_DIR": CONFIG, "OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]},
    )
    runner = RecordingOpenCodeRunner(real_runner)
    replies = ReplyRecorder(runner)
    service = PlannerService(info, runner=replies)

    started = service.start(designer_session_id, model=MODEL)
    session_id = started["session_id"]
    if started["status"] != "INQUIRY":
        pytest.fail(failure_message("planner.start did not reach INQUIRY", started, replies), pytrace=False)

    discovered = service.status(session_id)
    assert discovered["stories"] > 0
    assert discovered["status"] == "INQUIRY"
    assert not discovered["indexer_blockers"]

    generated = service.generate(session_id, model=MODEL)
    if generated["status"] != "GENERATED":
        pytest.fail(failure_message("planner.generate did not reach GENERATED", generated, replies), pytrace=False)
    assert service.gate(session_id)["unlocked"] is False
    review = service.open_review(session_id)
    from tests.e2e.planner_test_helpers import tick_review

    tick_review(service.workspace, review["review_id"])
    challenge = service.approval_challenge(session_id)
    service.approve(session_id, reviewer="live-test-human", confirmation=challenge["confirmation"])
    gate = service.gate(session_id)
    assert gate["unlocked"] is True, gate
    assert generated["generated"]["story_count"] > 0
    assert generated["generated"]["component_count"] > 0
    assert generated["generated"]["test_phase_count"] > 0

    expected_agents = {
        "dridh/workers/planner-indexer",
        "dridh/workers/planner-component",
        "dridh/workers/planner-testing",
    }
    assert {call["agent"] for call in runner.calls} == expected_agents, runner.calls
    assert all(call["model"] == MODEL and call["success"] for call in runner.calls), runner.calls

    reconciled: list[dict[str, str]] = []
    with StateStore(info.state_db, info.workspace_id) as store:
        documents = DocumentRepository(store.connection, info.workspace_id)
        planning = PlanningRepository(store.connection, info.workspace_id)
        design = DesignRepository(store.connection, info.workspace_id)
        for item in generated["generated"]["files"]:
            path = item["path"]
            metadata = documents.document_metadata(path)
            revision = documents.current_revision(path)
            assert metadata is not None and revision is not None, path
            assert revision.content_sha256 == item["sha256"], path
            on_disk = (project / path).read_text(encoding="utf-8")
            assert on_disk == documents.stored_content(revision.id), path
            if item["kind"] in {"central_plan", "phase_file", "test_plan"}:
                kind = {"central_plan": "CENTRAL_PLAN", "phase_file": "PHASE_FILE", "test_plan": "TEST_PLAN"}[item["kind"]]
                findings = planning.findings(
                    relative_path=path,
                    document_id=str(metadata["id"]),
                    kind=kind,
                    current_revision_id=revision.id,
                    current_content=documents.stored_content(revision.id),
                )
                assert not findings, {path: [finding.message for finding in findings]}
            elif item["kind"] == "component_master":
                findings = design.findings(
                    relative_path=path,
                    document_id=str(metadata["id"]),
                    kind="COMPONENT_MASTER",
                    current_revision_id=revision.id,
                    current_content=documents.stored_content(revision.id),
                )
                assert not findings, {path: [finding.message for finding in findings]}
            if item["kind"] != "artifact":
                validator = {
                    "central_plan": "planner.central",
                    "phase_file": "planner.phase",
                    "test_plan": "planner.test-plan",
                    "component_master": "planner.component-master",
                }[item["kind"]]
                validation = documents.latest_validation(revision.id, validator)
                assert validation is not None and validation.status == "PASS", (path, validator)
            reconciled.append({"path": path, "revision_id": revision.id, "kind": item["kind"]})

    evidence = {
        "schema": "dridh.phase13-live-evidence",
        "schema_version": 1,
        "model": MODEL,
        "provider": "OpenRouter",
        "session_id": session_id,
        "designer_session_id": designer_session_id,
        "worker_calls": runner.calls,
        "story_count": generated["generated"]["story_count"],
        "component_count": generated["generated"]["component_count"],
        "test_phase_count": generated["generated"]["test_phase_count"],
        "gate_unlocked": gate["unlocked"],
        "reconciled_documents": reconciled,
    }
    evidence_path = os.environ.get("DRIDH_PLANNER_LIVE_EVIDENCE")
    if evidence_path:
        target = Path(evidence_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("PHASE13_LIVE_EVIDENCE " + json.dumps(evidence, sort_keys=True))
