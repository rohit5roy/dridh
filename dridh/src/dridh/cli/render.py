"""Human-readable output for CLI commands.

JSON stays the contract for scripts, agents and the OpenCode plugin: it is printed whenever stdout is not a terminal,
when `--json` is given (before or after the command) or when DRIDH_OUTPUT=json. In a terminal, commands print the
text rendering below instead. DRIDH_OUTPUT=text forces text (for example in a pager).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

_FORCED_JSON = False


def force_json() -> None:
    global _FORCED_JSON
    _FORCED_JSON = True


def wants_json() -> bool:
    if _FORCED_JSON:
        return True
    choice = os.environ.get("DRIDH_OUTPUT", "").strip().lower()
    if choice == "json":
        return True
    if choice == "text":
        return False
    return not sys.stdout.isatty()


def to_json(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, default=str)


# ------------------------------------------------------------------ generic

_HIDDEN = {"workspace_id"}


def _label(key: str) -> str:
    return key.replace("_", " ")


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _row(item: dict[str, Any]) -> str:
    return ", ".join(f"{_label(k)}: {_scalar(v)}" for k, v in item.items() if _is_scalar(v) and k not in _HIDDEN)


def generic(document: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(document, dict):
        for key in sorted(document):
            if key in _HIDDEN:
                continue
            value = document[key]
            if _is_scalar(value):
                lines.append(f"{pad}{_label(key)}: {_scalar(value)}")
            elif not value:
                lines.append(f"{pad}{_label(key)}: none")
            else:
                lines.append(f"{pad}{_label(key)}:")
                lines.extend(generic(value, indent + 1))
    elif isinstance(document, list):
        for item in document:
            if _is_scalar(item):
                lines.append(f"{pad}- {_scalar(item)}")
            elif isinstance(item, dict) and all(_is_scalar(v) for v in item.values()):
                lines.append(f"{pad}- {_row(item)}")
            else:
                lines.append(f"{pad}-")
                lines.extend(generic(item, indent + 1))
    else:
        lines.append(f"{pad}{_scalar(document)}")
    return lines


# ------------------------------------------------------------------ specific views


def _feature_status(doc: dict[str, Any]) -> list[str]:
    feature = doc.get("feature") or {}
    lines = [f"Feature: {feature.get('name')} ({feature.get('slug')})",
             f"Stage: {feature.get('stage')}  Status: {feature.get('status')}"]
    if feature.get("blocked_reason"):
        lines.append(f"Blocked: {feature['blocked_reason']}")
    nxt = doc.get("next_stage")
    if nxt:
        lines.append(f"Next stage: {nxt} ({'ready to advance' if doc.get('ready_to_advance') else 'not ready'})")
    for blocker in doc.get("blockers") or []:
        lines.append(f"  blocker: {blocker if isinstance(blocker, str) else _row(blocker) if isinstance(blocker, dict) else blocker}")
    if doc.get("stale"):
        lines.append(f"Stale approval: {doc['stale'] if _is_scalar(doc['stale']) else _row(doc['stale'])}")
    sessions = {k.removesuffix("_session_id"): v for k, v in (doc.get("sessions") or {}).items() if v}
    if sessions:
        lines.append("Sessions: " + ", ".join(f"{k} {v}" for k, v in sessions.items()))
    actions = doc.get("next_actions") or []
    if actions:
        lines.append("Next actions: " + ", ".join(actions))
    return lines


def _feature_list(doc: dict[str, Any]) -> list[str]:
    features = doc.get("features") or []
    if not features:
        return ["No features yet. Create one with: dridh feature create \"<name>\""]
    width = max(len(str(f.get("slug", ""))) for f in features)
    return [f"{str(f.get('slug', '')).ljust(width)}  {str(f.get('stage', '')).ljust(18)}  {f.get('status', '')}  {f.get('name', '')}" for f in features]


def _execute_status(doc: dict[str, Any]) -> list[str]:
    run = doc.get("run") or {}
    lines = [f"Run {run.get('id')}: {run.get('status')}" + (f" ({run['status_reason']})" if run.get("status_reason") else "")]
    counts = doc.get("stories") or {}
    lines.append(f"Stories: {doc.get('total', 0)} total; " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
    for lot in doc.get("lots") or []:
        lines.append(f"  lot {lot.get('lot_no', lot.get('id'))} [{lot.get('kind', '')}] {lot.get('status')}" + (f" verdict {lot['verdict']}" if lot.get("verdict") else ""))
    if doc.get("jev_flagged_stories"):
        lines.append("Jev flagged (advisory): " + ", ".join(doc["jev_flagged_stories"]))
    attempts = doc.get("attempts") or []
    if attempts:
        lines.append("Attempts:")
        for a in attempts:
            sha = (a.get("commit_sha") or "")[:12]
            lines.append(f"  {a.get('story_id')} #{a.get('attempt_no')} {a.get('kind')} {a.get('status')}" + (f" {sha}" if sha else ""))
    return lines


def _deliver_status(doc: dict[str, Any]) -> list[str]:
    lines = [f"Delivery for run {doc.get('run_id')}"]
    for story in doc.get("stories") or []:
        gates = "  ".join(f"{k}={v}" for k, v in (story.get("gates") or {}).items())
        lines.append(f"  {story.get('story_id')}  {story.get('status')}  {gates}")
    return lines


VIEWS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "feature-status": _feature_status,
    "feature-list": _feature_list,
    "execute-status": _execute_status,
    "deliver-status": _deliver_status,
}


def render(document: Any, view: str | None = None) -> str:
    if view in VIEWS and isinstance(document, dict):
        try:
            return "\n".join(VIEWS[view](document))
        except (KeyError, TypeError, AttributeError):
            pass  # an unexpected shape falls back to the generic rendering rather than failing the command
    return "\n".join(generic(document))


def render_error(error: dict[str, Any]) -> str:
    lines = [f"Error ({error.get('code', 'ERROR')}): {error.get('message', '')}"]
    for detail in error.get("details") or []:
        lines.append(f"  - {detail}")
    if error.get("recovery"):
        lines.append(f"What to do: {error['recovery']}")
    return "\n".join(lines)
