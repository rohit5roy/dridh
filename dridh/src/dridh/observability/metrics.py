"""One read-only report of how a workspace is doing.

Every figure comes from SQLite state that already exists. Nothing here stores or prints prompt, answer or document
text, so the report is safe to share. A section that cannot be computed (older schema, missing table) is reported
as ``unavailable`` instead of failing the whole report.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import dridh
from dridh.observability.usage import read_usage, summarise
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo


def _counts(connection: sqlite3.Connection, sql: str, *args: Any) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in connection.execute(sql, args)}


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _seconds_since(stamp: str | None, now: datetime) -> int | None:
    if not stamp:
        return None
    try:
        return max(0, int((now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()))
    except ValueError:
        return None


def _lifecycle(c: sqlite3.Connection, w: str, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for feature in c.execute("SELECT id,slug,stage,status FROM feature WHERE workspace_id=? ORDER BY created_at", (w,)):
        last = c.execute("SELECT MAX(created_at) FROM workflow_transition WHERE feature_id=?", (feature["id"],)).fetchone()[0]
        rows.append({"slug": feature["slug"], "stage": feature["stage"], "status": feature["status"], "seconds_in_stage": _seconds_since(last, now),
                     "transitions": c.execute("SELECT COUNT(*) FROM workflow_transition WHERE feature_id=?", (feature["id"],)).fetchone()[0]})
    return rows


def _bridge(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    rows = c.execute("SELECT operation,status,error_code,duration_ms FROM tool_invocation WHERE workspace_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1000", (w,)).fetchall()
    durations = [r["duration_ms"] for r in rows]
    failed = [r for r in rows if r["status"] == "FAILED"]
    return {"sampled": len(rows), "failed": len(failed), "latency_ms": {"p50": _percentile(durations, 0.5), "p95": _percentile(durations, 0.95)},
            "by_operation": _counts(c, "SELECT operation, COUNT(*) FROM tool_invocation WHERE workspace_id=? GROUP BY operation ORDER BY 2 DESC LIMIT 20", w),
            "error_codes": dict(sorted(((code, sum(1 for r in failed if r["error_code"] == code)) for code in {r["error_code"] for r in failed}), key=lambda item: -item[1]))}


def _jev(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    by_status = _counts(c, "SELECT status, COUNT(*) FROM jev_evaluation WHERE workspace_id=? GROUP BY status", w)
    health = c.execute("SELECT status,latency_ms,checked_at FROM jev_provider_health WHERE workspace_id=? ORDER BY checked_at DESC LIMIT 1", (w,)).fetchone()
    return {"evaluations": by_status, "fallbacks": by_status.get("FALLBACK", 0), "provider_health": dict(health) if health else None}


def _code_reacher(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    run = c.execute("SELECT * FROM code_index_run WHERE workspace_id=? AND status='SUCCEEDED' ORDER BY started_at DESC, rowid DESC LIMIT 1", (w,)).fetchone()
    if run is None:
        return {"indexed": False}
    started, completed = run["started_at"], run["completed_at"]
    seconds = None
    if started and completed:
        try:
            seconds = round((datetime.fromisoformat(completed) - datetime.fromisoformat(started)).total_seconds(), 2)
        except ValueError:
            seconds = None
    failed = c.execute("SELECT COUNT(*) FROM code_index_run WHERE workspace_id=? AND status='FAILED'", (w,)).fetchone()[0]
    return {"indexed": True, "files": run["files_seen"], "indexed_files": run["files_indexed"], "index_seconds": seconds, "failed_runs": failed,
            "age_seconds": _seconds_since(completed or started, now), "source": str(run["source_sha"])[:12], "note": run["error"]}


def _execution(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    stories = _counts(c, "SELECT status, COUNT(*) FROM story WHERE workspace_id=? GROUP BY status", w)
    attempts = _counts(c, "SELECT kind||':'||status, COUNT(*) FROM story_attempt WHERE workspace_id=? GROUP BY kind, status", w)
    return {"runs": _counts(c, "SELECT status, COUNT(*) FROM execution_run WHERE workspace_id=? GROUP BY status", w), "stories": stories,
            "active_stories": stories.get("IN_PROGRESS", 0), "blocked_stories": stories.get("BLOCKED", 0), "attempts": attempts,
            "repairs": sum(n for key, n in attempts.items() if key.startswith("REPAIR")),
            "lots": _counts(c, "SELECT status, COUNT(*) FROM execution_lot WHERE workspace_id=? GROUP BY status", w),
            "worktrees": _counts(c, "SELECT status, COUNT(*) FROM worktree WHERE workspace_id=? GROUP BY status", w)}


def _delivery(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    return {"pull_requests": _counts(c, "SELECT state, COUNT(*) FROM pull_request WHERE workspace_id=? GROUP BY state", w),
            "checks": _counts(c, "SELECT COALESCE(k.conclusion, k.status), COUNT(*) FROM pull_request_check k JOIN pull_request p ON p.id=k.pull_request_id WHERE p.workspace_id=? GROUP BY 1", w),
            "gates": _counts(c, "SELECT gate||':'||status, COUNT(*) FROM story_gate WHERE workspace_id=? GROUP BY gate, status", w),
            "blocked_dependency_gates": c.execute("SELECT COUNT(*) FROM story_gate WHERE workspace_id=? AND status IN ('BLOCKED','FAIL')", (w,)).fetchone()[0]}


def _reviews(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    reviews = _counts(c, "SELECT status, COUNT(*) FROM review WHERE workspace_id=? GROUP BY status", w)
    documents = _counts(c, "SELECT lifecycle_state, COUNT(*) FROM document WHERE workspace_id=? GROUP BY lifecycle_state", w)
    return {"reviews": reviews, "pending": reviews.get("OPEN", 0) + reviews.get("IN_REVIEW", 0), "approved": reviews.get("APPROVED", 0),
            "stale_documents": documents.get("STALE", 0), "documents": documents}


def _ledger(c: sqlite3.Connection, w: str, now: datetime) -> dict[str, Any]:
    marker = c.execute("SELECT entry_count, imported_at FROM ledger_import WHERE workspace_id=?", (w,)).fetchone()
    by_status = _counts(c, "SELECT deviation_status, COUNT(*) FROM ledger_entry WHERE workspace_id=? GROUP BY deviation_status", w)
    return {"imported": marker is not None, "entries": by_status, "age_seconds": _seconds_since(marker["imported_at"], now) if marker else None,
            "deviations": sum(n for status, n in by_status.items() if status != "DONE")}


SECTIONS: dict[str, Callable[[sqlite3.Connection, str, datetime], Any]] = {
    "lifecycle": _lifecycle, "bridge": _bridge, "jev": _jev, "code_reacher": _code_reacher,
    "execution": _execution, "delivery": _delivery, "reviews": _reviews, "ledger": _ledger,
}


def collect(info: WorkspaceInfo) -> dict[str, Any]:
    now = datetime.now(UTC)
    report: dict[str, Any] = {"generated_at": now.isoformat(timespec="seconds"), "dridh_version": dridh.__version__,
                              "workspace": {"id": info.workspace_id, "mode": info.mode}}
    with StateStore(info.state_db, info.workspace_id) as store:
        connection = store.read()
        row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        report["schema_version"] = int(row[0]) if row else None
        for name, build in SECTIONS.items():
            try:
                report[name] = build(connection, info.workspace_id, now)
            except sqlite3.Error as exc:
                report[name] = {"unavailable": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:  # token and cost accounting comes from the usage log, not from SQLite
        report["usage"] = summarise(read_usage(info.project_root))
    except (OSError, ValueError) as exc:
        report["usage"] = {"unavailable": f"{type(exc).__name__}: {str(exc)[:120]}"}
    _flag_stale_index(report, info)
    return report


def _flag_stale_index(report: dict[str, Any], info: WorkspaceInfo) -> None:
    """Say so when the index no longer matches the project's Git HEAD (a missed change is never silent)."""
    section = report.get("code_reacher")
    if not isinstance(section, dict) or not section.get("indexed"):
        return
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=info.project_root, capture_output=True, text=True, check=False, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude).opencode"], cwd=info.project_root,
                               capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return
    if done.returncode != 0:
        return
    head = done.stdout.strip()
    section["stale"] = bool(dirty.stdout.strip()) or not head.startswith(str(section["source"]))
    if section["stale"]:
        section["recovery"] = "Run `dridh index` to refresh the code index before planning or executing."


def render(report: dict[str, Any]) -> str:
    """The report as readable text tables (one renderer for the terminal and the panel: see tables.py)."""
    from dridh.observability.tables import render as render_tables

    return render_tables(report)
