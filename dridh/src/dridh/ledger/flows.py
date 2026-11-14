"""Approved flows and which story owns each, read from the database (the imported, approved documents and plan)."""

from __future__ import annotations

import sqlite3
from typing import Any


def approved_flows(connection: sqlite3.Connection, workspace_id: str) -> dict[str, dict[str, str]]:
    """``flow_id -> {use_case, type, text}`` for every flow of the approved Use Case document."""
    rows = connection.execute(
        "SELECT flow_id, parent_use_case_id, flow_type, subject, action_text, result_text FROM use_case_flow "
        "WHERE workspace_id=? ORDER BY parent_use_case_id, step_order", (workspace_id,)).fetchall()
    return {row[0]: {"use_case": row[1], "type": row[2], "text": f"{row[3]}: {row[4]} → {row[5]}"} for row in rows}


def story_flows(connection: sqlite3.Connection, workspace_id: str) -> dict[str, list[str]]:
    """``story_id -> [flow_id]``: the flows a story implements. A story linked to a whole Use Case owns all its flows."""
    flows = approved_flows(connection, workspace_id)
    by_use_case: dict[str, list[str]] = {}
    for flow_id, item in flows.items():
        by_use_case.setdefault(item["use_case"], []).append(flow_id)
    owned: dict[str, set[str]] = {}
    for story_id, use_case_id, flow_id in connection.execute(
            "SELECT story_id, use_case_id, flow_id FROM story_use_case WHERE workspace_id=?", (workspace_id,)):
        owned.setdefault(story_id, set()).update([flow_id] if flow_id else by_use_case.get(use_case_id, []))
    return {story: sorted(items) for story, items in owned.items()}


def flows_for_story(connection: sqlite3.Connection, workspace_id: str, story_id: str) -> list[dict[str, Any]]:
    """The flows of one story with their type and text, for the Executor's prompt."""
    flows = approved_flows(connection, workspace_id)
    return [{"id": flow, **flows[flow]} for flow in story_flows(connection, workspace_id).get(story_id, []) if flow in flows]


def delivered_flows(connection: sqlite3.Connection, workspace_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    """``story_id -> flow_id -> {status, note, proven, method, tests}`` from each story's latest passing attempt: what the
    Executor reported for each flow, and what the scripts proved. Empty for a story with no such evidence."""
    import json

    latest: dict[str, dict[str, Any]] = {}
    for story_id, evidence_json in connection.execute(
            "SELECT story_id, evidence_json FROM story_attempt WHERE workspace_id=? AND status='PASSED' "
            "ORDER BY finished_at, rowid", (workspace_id,)):
        latest[story_id] = json.loads(evidence_json or "{}")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for story_id, evidence in latest.items():
        report, proof = evidence.get("flow_report") or {}, evidence.get("flow_proof") or {}
        result[story_id] = {flow: {**item, **{k: proof.get(flow, {}).get(k) for k in ("proven", "method", "tests")}}
                            for flow, item in report.items()}
    return result
