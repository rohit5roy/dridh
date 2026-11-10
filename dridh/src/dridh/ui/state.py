"""What an interface needs to show, in one read-only call per screen.

``workspace_state`` feeds the sidebar, the status line and notifications; ``document_view`` the document reviewer;
``lot_view`` execution review. Every value comes from Dridh's own state (SQLite, session files, Git); nothing is
written, and nothing here approves, ticks or starts anything: those stay human CLI acts the interface invokes.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.lifecycle import STAGES
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo

STAGE_LABELS = {
    "INITIALISED": "Feature created",
    "CODEBASE_INDEXING": "Codebase indexing",
    "IDEATION": "Use Case discovery",
    "USE_CASE_REVIEW": "Use Case review",
    "TECHNICAL_DISCOVERY": "Technical design",
    "TDD_REVIEW": "Design review",
    "PLANNING": "Planning",
    "PLAN_VALIDATION": "Plan review",
    "EXECUTION": "Execution",
    "DELIVERY_REVIEW": "Delivery review",
    "COMPLETED": "Completed",
}
MAX_DOCUMENT_CHARS = 400_000
MAX_DIFF_CHARS = 60_000


def _payload(service: Any, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    try:
        return service._load(session_id)  # noqa: SLF001 - read-only view over the stage's own session file
    except DridhError:
        return None


def _review(info: WorkspaceInfo, review_id: str | None) -> dict[str, Any] | None:
    if not review_id:
        return None
    from dridh.state.reviews import ReviewRepository

    try:
        with StateStore(info.state_db, info.workspace_id) as store:
            review = ReviewRepository(store.read(), info.workspace_id).get_review(review_id)
    except DridhError:
        return None
    items = [{"item": c["item"], "checked": bool(c["checked"]), "checked_by": c["checked_by"]} for c in review["checklist"]]
    return {"id": review_id, "status": review["status"], "revision_id": review["revision_id"], "items": items,
            "ticked": sum(1 for c in items if c["checked"]), "total": len(items),
            "approved_by": next((a["reviewer"] for a in review["approvals"] if a["decision"] == "APPROVED"), None)}


def _stage_documents(info: WorkspaceInfo, feature: dict[str, Any]) -> dict[str, Any]:
    """The three reviewable documents, each with its session, review, concerns and Jev checkpoint."""
    from dridh.designer.service import DesignerService
    from dridh.ideator.service import IdeatorService
    from dridh.planner.service import PlannerService

    stages: dict[str, Any] = {}
    ideator = _payload(IdeatorService(info), feature.get("ideator_session_id"))
    if ideator:
        stages["use_cases"] = {
            "label": "Use Cases", "session_id": ideator["session_id"], "status": ideator["status"],
            "document": (ideator.get("generated") or {}).get("path"), "sha256": (ideator.get("generated") or {}).get("sha256"),
            "review": _review(info, ideator.get("review_id")), "concerns": list((ideator.get("handoff") or {}).get("concerns") or []),
            "jev": ideator.get("jev_checkpoint"), "approve": ["ideator", "approve", ideator["session_id"]],
        }
    designer = _payload(DesignerService(info), feature.get("designer_session_id"))
    if designer:
        generated = designer.get("generated") or {}
        stages["design"] = {
            "label": "Technical design", "session_id": designer["session_id"], "status": designer["status"],
            "document": generated.get("path"), "sha256": generated.get("sha256"),
            "review": _review(info, designer.get("review_id")),
            "concerns": list((designer.get("handoff") or {}).get("concerns") or []) + list((designer.get("handoff") or {}).get("open_blockers") or []),
            "jev": designer.get("jev_checkpoint"), "approve": ["designer", "approve", designer["session_id"]],
        }
    planner = _payload(PlannerService(info), feature.get("planner_session_id"))
    if planner:
        generated = planner.get("generated") or {}
        stages["plan"] = {
            "label": "Plan", "session_id": planner["session_id"], "status": planner["status"],
            "document": generated.get("central_plan"), "sha256": None,
            "review": _review(info, (planner.get("review") or {}).get("review_id")),
            "concerns": [*(planner.get("indexer") or {}).get("blockers", []), *(planner.get("testing") or {}).get("blockers", [])],
            "jev": planner.get("jev_checkpoint"), "approve": ["planner", "approve", planner["session_id"]],
            "stories": generated.get("story_count"), "test_phases": generated.get("test_phase_count"),
        }
    return stages


def _mark_stale(info: WorkspaceInfo, stages: dict[str, Any]) -> None:
    """An approved document whose file no longer matches the approved SHA-256 is stale (edited outside its stage)."""
    import hashlib

    for doc in stages.values():
        if not doc.get("sha256") or not doc.get("document"):
            continue
        path = info.project_root / doc["document"]
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            current = None
        doc["stale"] = current != doc["sha256"]


def _runs(info: WorkspaceInfo, feature_id: str) -> list[dict[str, Any]]:
    from dridh.state.runs import RunRepository

    with StateStore(info.state_db, info.workspace_id) as store:
        repo = RunRepository(store.read(), info.workspace_id)
        runs = repo.runs(feature_id)
        out = []
        for run in runs[-3:]:
            lots = repo.lots(run["id"])
            out.append({"id": run["id"], "status": run["status"], "pause_reason": run.get("pause_reason"),
                        "lots": [{"ordinal": lot["ordinal"], "kind": lot["kind"], "status": lot["status"], "story_ids": lot["story_ids"]} for lot in lots]})
    return out


def _human_action(feature: dict[str, Any], stages: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    stage = feature["stage"]
    for key in ("use_cases", "design"):
        doc = stages.get(key)
        if doc and doc.get("stale"):
            owner = "Ideator" if key == "use_cases" else "Designer"
            return {"kind": "stale", "stage_key": key,
                    "text": f"The {doc['label'].lower()} file changed after approval. Ask the Coordinator to have the {owner} regenerate it, then review again."}
    by_stage = {"USE_CASE_REVIEW": "use_cases", "TDD_REVIEW": "design", "PLAN_VALIDATION": "plan"}
    if stage in by_stage and by_stage[stage] in stages:
        doc = stages[by_stage[stage]]
        review = doc.get("review")
        if review is None:
            return {"kind": "open_review", "text": f"Open the {doc['label'].lower()} review.", "stage_key": by_stage[stage]}
        if review["status"] == "APPROVED":
            return {"kind": "advance", "text": f"The {doc['label'].lower()} is approved. Move the feature on.", "stage_key": by_stage[stage]}
        left = review["total"] - review["ticked"]
        if left:
            return {"kind": "checklist", "text": f"Tick {left} checklist item{'s' if left != 1 else ''}, then approve the {doc['label'].lower()}.",
                    "stage_key": by_stage[stage]}
        return {"kind": "approve", "text": f"Approve the {doc['label'].lower()}.", "stage_key": by_stage[stage]}
    if stage == "EXECUTION":
        active = next((r for r in reversed(runs) if r["status"] in ("RUNNING", "PAUSED")), None)
        if active is None:
            return {"kind": "execute", "text": "Start the execution run."}
        waiting = next((lot for lot in active["lots"] if lot["status"] == "AWAITING_REVIEW"), None)
        if waiting:
            return {"kind": "lot", "text": f"Review checkpoint lot {waiting['ordinal']} ({waiting['kind'].lower()}).",
                    "run_id": active["id"], "lot": waiting["ordinal"]}
        return {"kind": "wait", "text": "Execution is running."}
    if stage == "DELIVERY_REVIEW":
        return {"kind": "complete", "text": "Complete the feature."}
    return None


def workspace_state(info: WorkspaceInfo, feature_ref: str | None = None) -> dict[str, Any]:
    from dridh.coordinator.lifecycle import FeatureLifecycle

    lifecycle = FeatureLifecycle(info)
    features = lifecycle.list_features()
    if not features:
        return {"workspace": str(info.project_root), "mode": info.mode, "feature": None, "features": [],
                "human_action": {"kind": "create", "text": "Tell the Coordinator what you want to build."}}
    active = [f for f in features if f["stage"] != "COMPLETED"]
    chosen = next((f for f in features if f["slug"] == feature_ref), None) if feature_ref else (active or features)[-1]
    if chosen is None:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown feature '{feature_ref}'.")
    status = lifecycle.status(chosen["slug"])
    feature = {**chosen, **status["feature"]}
    stages = _stage_documents(info, feature)
    _mark_stale(info, stages)
    runs = _runs(info, feature["id"])
    current = STAGES.index(feature["stage"])
    timeline = [{"stage": s, "label": STAGE_LABELS.get(s, s),
                 "state": "done" if i < current or feature["stage"] == "COMPLETED" else "current" if i == current else "todo"}
                for i, s in enumerate(STAGES) if not (s == "CODEBASE_INDEXING" and feature.get("mode") != "existing")]
    return {
        "workspace": str(info.project_root), "mode": info.mode,
        "feature": {"slug": feature["slug"], "name": feature["name"], "stage": feature["stage"], "stage_label": STAGE_LABELS.get(feature["stage"]),
                    "status": feature["status"], "blocked_reason": feature.get("blocked_reason"), "blockers": status.get("blockers") or []},
        "features": [{"slug": f["slug"], "stage": f["stage"]} for f in features],
        "timeline": timeline, "documents": stages, "runs": runs,
        "human_action": _human_action(feature, stages, runs),
    }


def _sections(markdown: str) -> list[dict[str, Any]]:
    out = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        match = re.match(r"^(#{1,3})\s+(.*)$", line)
        if match:
            out.append({"level": len(match.group(1)), "title": match.group(2).strip(), "line": number})
    return out


def document_view(info: WorkspaceInfo, stage_key: str, feature_ref: str | None = None) -> dict[str, Any]:
    """One reviewable document: its text, outline, validation, concerns, Jev and checklist."""
    from dridh.document_contracts.validator import validate_file

    state = workspace_state(info, feature_ref)
    doc = (state.get("documents") or {}).get(stage_key)
    if not doc or not doc.get("document"):
        raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, f"No {stage_key} document has been generated yet.")
    path = (info.project_root / doc["document"]).resolve()
    if info.project_root.resolve() not in path.parents:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "The document is outside the project.")
    text = path.read_text(encoding="utf-8")[:MAX_DOCUMENT_CHARS]
    try:
        result = validate_file(path)
        validation = {"ok": result.ok, "errors": result.errors[:50], "warnings": result.warnings[:50]}
    except Exception as exc:  # noqa: BLE001 - a viewer shows what it can
        validation = {"ok": None, "errors": [f"Could not validate: {type(exc).__name__}"], "warnings": []}
    from dridh.ui.mermaid import render_markdown

    display, drawn, kept = render_markdown(text)
    return {"feature": state["feature"], "stage_key": stage_key, "display": display, "diagrams": {"drawn": drawn, "source_only": kept}, "label": doc["label"], "path": doc["document"], "sha256": doc.get("sha256"),
            "session_id": doc["session_id"], "status": doc["status"], "text": text, "sections": _sections(text), "validation": validation,
            "concerns": doc.get("concerns") or [], "jev": doc.get("jev"), "review": doc.get("review"), "approve": doc["approve"]}


def lot_view(info: WorkspaceInfo, run_id: str, ordinal: int) -> dict[str, Any]:
    """A checkpoint lot: each story's latest attempt, its evidence and its diff."""
    from dridh.state.runs import RunRepository

    with StateStore(info.state_db, info.workspace_id) as store:
        repo = RunRepository(store.read(), info.workspace_id)
        run = repo.get_run(run_id)
        lot = next((x for x in repo.lots(run_id) if x["ordinal"] == ordinal), None)
        if lot is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Run {run_id} has no lot {ordinal}.")
        stories = []
        for story_id in lot["story_ids"]:
            attempts = repo.attempts(run_id, story_id)
            last = attempts[-1] if attempts else None
            evidence = (last or {}).get("evidence") or {}
            commit = (last or {}).get("commit_sha")
            diff = ""
            if commit and re.fullmatch(r"[0-9a-f]{7,64}", commit):
                shown = subprocess.run(["git", "show", "--stat", "--patch", "--format=%s%n", commit], cwd=info.project_root,  # noqa: S603, S607
                                       capture_output=True, text=True, timeout=30, check=False)
                diff = shown.stdout[:MAX_DIFF_CHARS]
            tests = evidence.get("tests") or {}
            stories.append({
                "story_id": story_id, "attempts": len(attempts), "status": (last or {}).get("status"), "kind": (last or {}).get("kind"),
                "commit": commit, "summary": evidence.get("summary"), "changed_files": evidence.get("changed_files") or [],
                "tests": {"command": tests.get("command"), "exit_code": tests.get("exit_code"),
                          "tail": str(tests.get("output") or tests.get("stdout") or "")[-1500:]},
                "new_mocks": evidence.get("new_mocks") or [], "findings": evidence.get("findings") or [],
                "jev": evidence.get("jev"), "diff": diff,
            })
    return {"run": {"id": run["id"], "status": run["status"], "pause_reason": run.get("pause_reason")},
            "lot": {"ordinal": lot["ordinal"], "kind": lot["kind"], "status": lot["status"]}, "stories": stories}


def dump(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, default=str)

