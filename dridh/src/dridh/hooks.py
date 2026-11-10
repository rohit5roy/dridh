"""Bridge operations behind the OpenCode plugin's hooks (plan section 7.5).

The plugin calls these from OpenCode hooks, never from the agent-facing ``dridh.bridge`` tool (they are not in its
allow-list), and only for Dridh agents (``dridh/...``): ordinary OpenCode sessions never reach Dridh state.

- ``hook.context``: a compact summary of the workspace's feature, stage, next human step and blockers, added to a Dridh
  agent's model request so it starts from Dridh's state instead of guessing.
- ``hook.tool_call``: metadata of one tool call made by a Dridh agent (tool, agent, session, status, duration, the keys
  and SHA-256 of the input, never the input itself), recorded in the ``tool_invocation`` audit table.
- ``hook.session_event``: one session event of a Dridh session (execution started/succeeded/failed/interrupted, idle,
  compaction), recorded the same way for correlation and status telemetry.

All three are best-effort: a workspace without Dridh, an old schema or a locked database yields an empty answer, never an
error that could disturb the OpenCode session.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any

from dridh.common.errors import DridhError, ErrorCode

HOOK_OPERATIONS = frozenset({"hook.context", "hook.tool_call", "hook.session_event"})
MAX_CONTEXT_CHARS = 900
_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def is_dridh_agent(agent: object) -> bool:
    return isinstance(agent, str) and agent.startswith("dridh/")


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{field} must be a short identifier.")
    return value


def _workspace(request: Any):  # type: ignore[no-untyped-def]
    from dridh.workspace.layout import open_workspace, workspace_exists
    from dridh.workspace.paths import validate_workspace_root

    root = validate_workspace_root(request.workspace_root)
    return open_workspace(root) if workspace_exists(root) else None


def context_text(state: dict[str, Any]) -> str:
    """The compact context block a Dridh agent sees at the top of its request."""

    feature = state.get("feature")
    if not feature:
        return ("Dridh workspace: no feature yet. Ask the person what they want to build, then create the feature "
                "(coordinator.create_feature). Approvals are human-only.")
    lines = [f"Dridh workspace ({state.get('mode') or 'unknown'} codebase). Feature '{feature['slug']}': stage "
             f"{feature['stage']} ({feature.get('stage_label') or feature['stage']}), status {feature['status']}."]
    action = state.get("human_action")
    if action:
        lines.append(f"Next human step: {action['text']}")
    blockers = [b for b in feature.get("blockers") or [] if b != "The feature is complete."]
    if blockers:
        lines.append("Blockers: " + "; ".join(blockers[:3]))
    lines.append("Dridh's state is authoritative: read it with coordinator.get_context before acting. Approvals, checklist "
                 "ticks, starting execution and completing a feature are human-only; never claim them.")
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


def _record(request: Any, *, operation: str, agent: str, session_id: str | None, ok: bool, error_code: str | None,
            payload: dict[str, Any], duration_ms: int) -> bool:
    info = _workspace(request)
    if info is None:
        return False
    from dridh.state.invocations import InvocationRepository
    from dridh.state.store import StateStore

    caller = SimpleNamespace(source="hook", agent=agent, session_id=session_id)
    started = time.monotonic() - max(0, duration_ms) / 1000
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        InvocationRepository(connection, info.workspace_id).record(
            request_id=request.request_id, operation=operation, caller=caller, payload=payload, ok=ok,
            error_code=error_code, started=started)
    return True


def dispatch_hook(request: Any) -> dict[str, Any]:
    payload = request.payload or {}
    agent = payload.get("agent")
    if not is_dridh_agent(agent):
        return {"recorded": False, "text": "", "reason": "not a Dridh agent"}
    agent = _name(agent, "agent")
    session_id = payload.get("session_id")
    session_id = _name(session_id, "session_id") if session_id is not None else None
    try:
        if request.operation == "hook.context":
            info = _workspace(request)
            if info is None:
                return {"text": ""}
            from dridh.ui.state import workspace_state

            return {"text": context_text(workspace_state(info))}
        if request.operation == "hook.tool_call":
            tool = _name(payload.get("tool"), "tool")
            status = payload.get("status")
            if status not in ("completed", "error"):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "status must be completed or error.")
            keys = payload.get("input_keys") or []
            digest = payload.get("input_sha256")
            if not isinstance(keys, list) or len(keys) > 50 or not all(isinstance(k, str) and len(k) <= 80 for k in keys):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "input_keys must be a short list of names.")
            if digest is not None and (not isinstance(digest, str) or not _SHA.match(digest)):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "input_sha256 must be a SHA-256 hex digest.")
            duration = payload.get("duration_ms") or 0
            recorded = _record(request, operation=f"opencode.tool:{tool}", agent=agent, session_id=session_id,
                               ok=status == "completed", error_code=None if status == "completed" else "TOOL_ERROR",
                               payload={"input_keys": sorted(keys), "input_sha256": digest},
                               duration_ms=int(duration) if isinstance(duration, int | float) else 0)
            return {"recorded": recorded}
        if request.operation == "hook.session_event":
            event = _name(payload.get("type"), "type")
            if not event.startswith("session."):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Only session events are recorded.")
            failed = event in ("session.execution.failed", "session.compaction.failed")
            recorded = _record(request, operation=f"opencode.event:{event}", agent=agent, session_id=session_id,
                               ok=not failed, error_code="SESSION_FAILED" if failed else None, payload={}, duration_ms=0)
            return {"recorded": recorded}
    except DridhError as exc:
        if exc.code is ErrorCode.VALIDATION_FAILED:
            raise
        return {"recorded": False, "text": "", "reason": exc.code.value}
    except Exception as exc:  # noqa: BLE001 - a hook must never disturb the OpenCode session
        return {"recorded": False, "text": "", "reason": type(exc).__name__}
    raise DridhError(ErrorCode.VALIDATION_FAILED, "Unknown hook operation.")
