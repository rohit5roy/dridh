"""Read-only Coordinator queries for the bridge: context, state queries and document validation.

Nothing here writes state, approves anything or runs a model. Output is bounded and contains no document text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.coordinator.lifecycle import FeatureLifecycle
from dridh.document_contracts.validator import (
    validate_central_plan,
    validate_component_master,
    validate_coordinator,
    validate_phase_file,
    validate_qa_log,
    validate_tdd,
    validate_test_plan,
    validate_use_cases,
)
from dridh.state.runs import RunRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo
from dridh.workspace.paths import ensure_within_root

VALIDATORS = {
    "use-cases": validate_use_cases, "tdd": validate_tdd, "central-plan": validate_central_plan,
    "component-master": validate_component_master, "phase-file": validate_phase_file,
    "coordinator": validate_coordinator, "qa-log": validate_qa_log, "test-plan": validate_test_plan,
}
QUERY_KINDS = ("features", "stories", "runs", "lots", "gates", "ledger")
MAX_DOCUMENT_BYTES = 2_000_000
MAX_ROWS = 200


def get_context(info: WorkspaceInfo, reference: str | None) -> dict[str, Any]:
    """Everything the Coordinator needs to decide the next step for a feature, in one call."""
    lifecycle = FeatureLifecycle(info)
    features = lifecycle.list_features()
    summary = [{"slug": f["slug"], "stage": f["stage"], "status": f["status"]} for f in features]
    if reference is None:
        if len(features) != 1:
            return {"workspace": {"mode": info.mode, "workspace_id": info.workspace_id}, "features": summary,
                    "message": "Pass a feature slug." if features else "No feature exists yet; create one with coordinator.create_feature."}
        reference = features[0]["slug"]
    status = lifecycle.status(reference)
    feature = status["feature"]
    with StateStore(info.state_db, info.workspace_id) as store:
        runs = [{k: r[k] for k in ("id", "status", "pause_reason", "max_parallel", "created_at")}
                for r in RunRepository(store.read(), info.workspace_id).runs(feature["id"])][-5:]
    return {"workspace": {"mode": info.mode, "workspace_id": info.workspace_id}, "features": summary,
            "status": status, "execution_runs": runs}


def query(info: WorkspaceInfo, kind: str, *, feature: str | None = None, status: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    if kind not in QUERY_KINDS:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"kind must be one of: {', '.join(QUERY_KINDS)}.")
    sql = {
        "features": ("SELECT slug,name,mode,stage,status,blocked_reason,updated_at FROM feature WHERE workspace_id=? {f} ORDER BY created_at", "status"),
        "stories": ("SELECT story_id,title,component_id,phase_number,status FROM story WHERE workspace_id=? {f} ORDER BY position", "status"),
        "runs": ("SELECT id,feature_id,status,pause_reason,max_parallel,max_repairs,created_at FROM execution_run WHERE workspace_id=? {f} ORDER BY created_at", "status"),
        "lots": ("SELECT l.run_id,l.ordinal,l.kind,l.threshold_percent,l.status FROM execution_lot l WHERE l.workspace_id=? {f} ORDER BY l.run_id,l.ordinal", "l.status"),
        "gates": ("SELECT story_id,gate,status,revision_sha FROM story_gate WHERE workspace_id=? {f} ORDER BY story_id,gate", "status"),
        "ledger": ("SELECT ledger_id,title,origin_id,deviation_status,linked_story_id,approved_by FROM ledger_entry WHERE workspace_id=? {f} ORDER BY position", "deviation_status"),
    }[kind]
    clauses, args = "", [info.workspace_id]
    if status:
        clauses += f" AND {sql[1]}=?"
        args.append(status)
    if run_id and kind in ("lots", "runs"):
        clauses += " AND " + ("l.run_id=?" if kind == "lots" else "id=?")
        args.append(run_id)
    with StateStore(info.state_db, info.workspace_id) as store:
        rows = [dict(r) for r in store.read().execute(sql[0].format(f=clauses) + f" LIMIT {MAX_ROWS}", args)]
    return {"kind": kind, "count": len(rows), "rows": rows, "truncated": len(rows) == MAX_ROWS}


def validate_document(info: WorkspaceInfo, kind: str, path: str) -> dict[str, Any]:
    """Run one deterministic validator on a file inside the project. Read-only."""
    validator = VALIDATORS.get(kind)
    if validator is None:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"kind must be one of: {', '.join(sorted(VALIDATORS))}.")
    target = ensure_within_root(path, info.project_root)
    if not Path(target).is_file() or Path(target).is_symlink() or Path(target).stat().st_size > MAX_DOCUMENT_BYTES:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "path must be a regular file inside the project, at most 2 MB.")
    result = validator(Path(target).read_text(encoding="utf-8", errors="replace"), file=Path(target).name)
    findings = [f.to_dict() for f in result.findings][:50]
    return {"kind": kind, "path": path, "ok": result.ok, "errors": len([f for f in result.findings if f.severity == "ERROR"]),
            "warnings": len([f for f in result.findings if f.severity == "WARNING"]), "findings": findings}


REVIEW_STATUSES = ("OPEN", "IN_REVIEW", "APPROVED", "REJECTED", "CHANGES_REQUESTED")


def list_reviews(info: WorkspaceInfo, *, status: str | None = None) -> dict[str, Any]:
    """Reviews with their document and checklist progress. Read-only: ticking items and approving stay human-only CLI acts."""
    if status is not None and status not in REVIEW_STATUSES:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"status must be one of: {', '.join(REVIEW_STATUSES)}.")
    sql = ("SELECT r.id AS review_id, d.relative_path AS document, d.document_type, r.status, r.reviewer, r.created_at, "
           "(SELECT COUNT(*) FROM review_checklist_item c WHERE c.review_id=r.id) AS items, "
           "(SELECT COUNT(*) FROM review_checklist_item c WHERE c.review_id=r.id AND c.checked=1) AS checked "
           "FROM review r JOIN document d ON d.id=r.document_id AND d.workspace_id=r.workspace_id WHERE r.workspace_id=?")
    args: list[Any] = [info.workspace_id]
    if status:
        sql += " AND r.status=?"
        args.append(status)
    with StateStore(info.state_db, info.workspace_id) as store:
        rows = [dict(r) for r in store.read().execute(sql + f" ORDER BY r.created_at LIMIT {MAX_ROWS}", args)]
    return {"count": len(rows), "reviews": rows, "truncated": len(rows) == MAX_ROWS}


def review_checklist(info: WorkspaceInfo, review_id: str) -> dict[str, Any]:
    """One review's checklist: what a human still has to tick before `dridh reviews approve` (or the stage approve) is accepted."""
    from dridh.state.reviews import ReviewRepository

    with StateStore(info.state_db, info.workspace_id) as store:
        review = ReviewRepository(store.read(), info.workspace_id).get_review(review_id)
    items = [{"item": c["item"], "checked": bool(c["checked"]), "checked_by": c["checked_by"]} for c in review["checklist"]]
    outstanding = [c["item"] for c in items if not c["checked"]]
    return {
        "review_id": review_id, "status": review["status"], "revision_id": review["revision_id"],
        "items": items, "outstanding": outstanding, "complete": not outstanding and bool(items),
        "human_action": ("A human ticks the remaining items with `dridh reviews checklist " + review_id + " <item>` (or --all after reading them), "
                         "then approves at a terminal.") if outstanding or not items else "A human can now approve at a terminal.",
    }
