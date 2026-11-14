"""The completion matrix: every approved flow with its story, its state, its proof or deviation, and its code.

One row per approved flow, grouped by use case. Every value is read from what Dridh already holds: the approved use
cases (flows and their text), the design's API and event references, the plan (which story owns which flow), each
story's latest passing attempt (the Executor's flow report and the test proof), the ledger entries and their human
decisions, and the story-to-code mapping. No model is involved. The panel's Coverage tab and ``dridh coverage`` show it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dridh.ledger.drafts import ledger_file
from dridh.ledger.flows import approved_flows, delivered_flows, story_flows
from dridh.observability.tables import DASH, layout, table
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo

EXPLAINING = ("DEVIATED", "NOT_DONE", "WONT_DO")
# state -> (label, tone). The tone is for colour only: ok, warn, bad, muted.
STATES = {
    "proven": ("✓ proven", "ok"),
    "explained": ("✓ deviation approved", "ok"),
    "awaiting": ("! deviation awaits you", "warn"),
    "rejected": ("✗ deviation rejected", "bad"),
    "unexplained": ("✗ deviation not recorded", "bad"),
    "not_proven": ("✗ not proven", "bad"),
    "no_evidence": ("✗ no flow report", "bad"),
    "no_story": ("✗ no story", "bad"),
    "blocked": ("✗ story blocked", "bad"),
    "in_review": ("… in review", "muted"),
    "in_progress": ("… in progress", "muted"),
    "not_started": ("· not started", "muted"),
}
DONE_STATES = ("proven", "explained")


def _code(connection: Any, workspace_id: str) -> dict[str, list[str]]:
    """``story_id -> [file path]`` from the story-to-code mapping (empty before the first mapping)."""
    files: dict[str, list[str]] = {}
    try:
        rows = connection.execute(
            "SELECT DISTINCT m.target_id, f.path FROM code_repository_mapping m JOIN code_entity e ON e.id=m.entity_id "
            "JOIN code_file f ON f.id=e.file_id WHERE m.workspace_id=? AND m.target_type='STORY' ORDER BY m.target_id, f.path",
            (workspace_id,)).fetchall()
    except Exception:  # noqa: BLE001 - an older or unindexed workspace simply has no code links
        return files
    for story, path in rows:
        files.setdefault(story, []).append(path)
    return files


def _contracts(connection: Any, workspace_id: str) -> dict[str, list[str]]:
    """``flow_id -> [approved API/event reference]`` from the imported design, in document order."""
    from dridh.planner.contract import reference_text

    found: dict[str, list[str]] = {}
    for flow, kind, name in connection.execute(
            "SELECT flow_id, reference_kind, reference_name FROM api_event_mapping WHERE workspace_id=? AND flow_id IS NOT NULL ORDER BY position",
            (workspace_id,)):
        text = reference_text(kind, name)
        if text not in found.setdefault(flow, []):
            found[flow].append(text)
    return found


def coverage(workspace: WorkspaceInfo, *, awaiting: list[str] | None = None) -> dict[str, Any]:
    """``{summary, counts, use_cases: [{use_case, title, done, total, rows: [...]}]}``.

    ``awaiting`` (ledger IDs that need a human decision) is computed with the ledger check when not given."""
    from dridh.document_contracts.ledger_store import load_ledger_decisions

    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        connection = store.read()
        w = workspace.workspace_id
        flows = approved_flows(connection, w)
        titles = {r[0]: r[1] for r in connection.execute("SELECT use_case_id, title FROM use_case WHERE workspace_id=?", (w,))}
        owners = story_flows(connection, w)
        delivered = delivered_flows(connection, w)
        stories = {r[0]: r[1] for r in connection.execute("SELECT story_id, status FROM story WHERE workspace_id=?", (w,))}
        entries = [dict(r) for r in connection.execute(
            "SELECT ledger_id, origin_id, deviation_status, linked_story_id FROM ledger_entry WHERE workspace_id=? ORDER BY position", (w,))]
        decisions = dict(load_ledger_decisions(connection))
        code = _code(connection, w)
        contracts = _contracts(connection, w)
    if awaiting is None:
        awaiting = []
        if ledger_file(workspace).is_file() and entries:
            from dridh.coordinator.delivery_check import delivery_readiness

            awaiting = delivery_readiness(workspace)["awaiting_approval"]
    owner_of = {flow: story for story, items in owners.items() for flow in items}
    by_flow: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_flow.setdefault(entry["origin_id"], []).append(entry)

    groups: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for flow, item in flows.items():
        story = owner_of.get(flow)
        report = delivered.get(story or "", {}).get(flow)
        entry = next((e for e in by_flow.get(flow, []) if e["deviation_status"] in EXPLAINING), None)
        state, proof = _state(flow, story, stories.get(story or ""), report, entry, awaiting, decisions)
        counts[state] = counts.get(state, 0) + 1
        label, tone = STATES[state]
        group = groups.setdefault(item["use_case"], {"use_case": item["use_case"], "title": titles.get(item["use_case"], ""), "rows": []})
        group["rows"].append({
            "flow": flow, "type": item["type"].lower().replace("_", " "), "story": story or DASH,
            "story_status": (stories.get(story or "") or "").lower().replace("_", " ") or DASH,
            "state": state, "label": label, "tone": tone, "proof": proof,
            "code": ", ".join(code.get(story or "", [])[:3]) + (" …" if len(code.get(story or "", [])) > 3 else "") or DASH,
            "detail": {"text": item["text"], "contract": contracts.get(flow, []), "tests": (report or {}).get("tests") or [],
                       "report": (report or {}).get("status"), "note": (report or {}).get("note") or "",
                       "ledger": entry["ledger_id"] if entry else None,
                       "ledger_status": entry["deviation_status"] if entry else None,
                       "awaiting": bool(entry and entry["ledger_id"] in awaiting),
                       "code": code.get(story or "", [])},
        })
    use_cases = []
    for group in groups.values():
        group["done"] = sum(1 for row in group["rows"] if row["state"] in DONE_STATES)
        group["total"] = len(group["rows"])
        use_cases.append(group)
    total = sum(counts.values())
    done = sum(counts.get(state, 0) for state in DONE_STATES)
    return {"summary": summary(done, total, counts), "done": done, "total": total, "counts": counts, "use_cases": use_cases}


def _state(flow: str, story: str | None, status: str | None, report: dict[str, Any] | None, entry: dict[str, Any] | None,
           awaiting: list[str], decisions: dict[str, Any]) -> tuple[str, str]:
    """(state, proof or deviation text) for one flow, by the same rules as the delivery check."""
    if entry is not None:
        ledger, kind = entry["ledger_id"], entry["deviation_status"].replace("_", " ").lower()
        decision = decisions.get(ledger)
        if decision is not None and decision.decision != "approve":
            return "rejected", f"{ledger} {kind}, rejected"
        if ledger in awaiting:
            return "awaiting", f"{ledger} {kind}, awaits approval"
        return "explained", f"{ledger} {kind}, approved"
    if story is None:
        return "no_story", DASH
    if status != "COMPLETED":
        return {"BLOCKED": "blocked", "IN_REVIEW": "in_review", "IN_PROGRESS": "in_progress"}.get(status or "", "not_started"), DASH
    if report is None:
        return "no_evidence", DASH
    if report.get("status") != "IMPLEMENTED":
        return "unexplained", f"reported {str(report.get('status')).replace('_', ' ').lower()}"
    if not report.get("proven"):
        return "not_proven", "no passing test named for it"
    tests = report.get("tests") or []
    return "proven", (tests[0].rsplit("::", 1)[-1] + (f" +{len(tests) - 1}" if len(tests) > 1 else "")) if tests else "proven"


def summary(done: int, total: int, counts: dict[str, int]) -> str:
    """``12 of 15 flows done · 2 deviations await you · 1 not proven``; empty before any flow is approved."""
    if not total:
        return ""
    parts = [f"{done} of {total} flows proven or explained"]
    for state, words in (("awaiting", "await you"), ("not_proven", "not proven"), ("no_evidence", "without a flow report"),
                         ("unexplained", "deviation not recorded"), ("rejected", "deviation rejected"), ("blocked", "story blocked")):
        if counts.get(state):
            parts.append(f"{counts[state]} {words}")
    return " · ".join(parts)


COLUMNS = ["Flow", "Type", "Story", "Status", "Proof or deviation", "Code"]


def tables(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One table per use case, laid out for a terminal (the panel colours the same cells itself)."""
    out = []
    for group in report["use_cases"]:
        rows = [[r["flow"], r["type"], r["story"], r["label"], r["proof"], r["code"]] for r in group["rows"]]
        item = table(f"{group['use_case']} {group['title']}  ·  {group['done']} of {group['total']} done", COLUMNS, rows)
        item["lines"] = layout(item)
        out.append(item)
    return out


def render(report: dict[str, Any]) -> str:
    if not report["total"]:
        return "No approved flows yet: coverage starts once the use cases are approved."
    out = [report["summary"]]
    for item in tables(report):
        out += ["", item["title"], *("  " + line for line in item["lines"])]
    return "\n".join(out)


def widths(report: dict[str, Any], measure: Callable[[str], int] = len) -> list[int]:
    """Column widths across every use case, so the panel's tables line up with each other."""
    cells = [COLUMNS] + [[r["flow"], r["type"], r["story"], r["label"], r["proof"], r["code"]] for g in report["use_cases"] for r in g["rows"]]
    return [max(measure(str(row[i])) for row in cells) for i in range(len(COLUMNS))]
