"""Token and cost accounting for the agent runs Dridh starts.

OpenCode's JSON event stream ends every model step with a ``step_finish`` event that carries the step's token counts
(input, output, reasoning, cache read and write) and the cost OpenCode computed for it. ``OpenCodeRunner`` sums those per
invocation and appends one line per invocation to ``.opencode/dridh/logs/usage.jsonl``: agent, model, session, outcome,
duration and the totals. Failed invocations are recorded too, because a rejected or repaired reply still cost tokens.

Nothing here stores prompt, answer or document text. Recording is best-effort: a workspace that does not exist or a file
that cannot be written never disturbs the agent run.

Limits: an invocation that times out or is cancelled is killed before its output is read, so its tokens are not counted.
The interactive Coordinator conversation runs in OpenCode itself, not through the runner; ``opencode stats --cost
--project .`` covers it. A provider that bills through a subscription login reports a cost of 0; the token counts are
still exact, so cost can be computed from a price list.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USAGE_LOG = Path(".opencode") / "dridh" / "logs" / "usage.jsonl"
COUNTERS = ("input", "output", "reasoning", "cache_read", "cache_write")
_STORY = re.compile(r"^Dridh Executor (\S+)$")
_STAGES = (("ideator", "ideator"), ("designer", "designer"), ("technical-designer", "designer"), ("planner", "planner"),
           ("executor", "executor"), ("coordinator", "coordinator"), ("code-reacher", "code-reacher"))


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def empty_usage() -> dict[str, Any]:
    return {"steps": 0, **dict.fromkeys(COUNTERS, 0), "cost": 0.0}


def usage_from_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Sum the tokens and cost of every finished model step in one invocation's events."""
    total = empty_usage()
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        total["steps"] += 1
        total["input"] += int(_number(tokens.get("input")))
        total["output"] += int(_number(tokens.get("output")))
        total["reasoning"] += int(_number(tokens.get("reasoning")))
        total["cache_read"] += int(_number(cache.get("read")))
        total["cache_write"] += int(_number(cache.get("write")))
        total["cost"] += _number(part.get("cost"))
    total["cost"] = round(total["cost"], 6)
    return total


def usage_from_stdout(stdout: str) -> dict[str, Any]:
    """The same sum from OpenCode's raw JSONL output; lines that are not JSON objects are ignored."""
    events = []
    for line in stdout.splitlines():
        if '"step_finish"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return usage_from_events(events)


def stage_of(agent: str) -> str:
    """The lifecycle stage an agent works for, from its role identifier (``dridh/workers/planner-testing`` -> planner)."""
    name = agent.rsplit("/", 1)[-1]
    for prefix, stage in _STAGES:
        if name == prefix or name.startswith(prefix + "-"):
            return stage
    return "other"


def record_usage(project_root: str | Path, *, agent: str, model: str | None, session_id: str | None, title: str | None,
                 outcome: str, duration_seconds: float, usage: dict[str, Any], resumed: bool) -> None:
    """Append one invocation to the workspace's usage log. Never raises."""
    try:
        path = Path(project_root) / USAGE_LOG
        if not path.parent.parent.is_dir():  # no Dridh workspace here (or a story worktree without one)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        story = _STORY.match(title or "")
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"), "agent": agent, "stage": stage_of(agent),
            "model": model or os.environ.get("DRIDH_USAGE_MODEL_LABEL") or "opencode-default", "session_id": session_id,
            "story_id": story.group(1) if story else None, "outcome": outcome, "resumed": bool(resumed),
            "duration_seconds": round(float(duration_seconds), 3), **usage,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001, S110 - accounting must never break an agent run
        pass


def read_usage(project_root: str | Path) -> list[dict[str, Any]]:
    path = Path(project_root) / USAGE_LOG
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _add(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    bucket["invocations"] += 1
    bucket["failed"] += 0 if entry.get("outcome") == "OK" else 1
    bucket["duration_seconds"] = round(bucket["duration_seconds"] + _number(entry.get("duration_seconds")), 3)
    bucket["steps"] += int(_number(entry.get("steps")))
    for key in COUNTERS:
        bucket[key] += int(_number(entry.get(key)))
    bucket["cost"] = round(bucket["cost"] + _number(entry.get("cost")), 6)


def _bucket() -> dict[str, Any]:
    return {"invocations": 0, "failed": 0, "duration_seconds": 0.0, **empty_usage()}


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals overall and by stage, agent, model and story. ``total_tokens`` counts every token the provider processed."""
    total = _bucket()
    groups: dict[str, dict[str, dict[str, Any]]] = {"by_stage": {}, "by_agent": {}, "by_model": {}, "by_story": {}}
    for entry in entries:
        _add(total, entry)
        for name, key in (("by_stage", "stage"), ("by_agent", "agent"), ("by_model", "model"), ("by_story", "story_id")):
            value = entry.get(key)
            if value:
                _add(groups[name].setdefault(str(value), _bucket()), entry)
    for bucket in (total, *(b for group in groups.values() for b in group.values())):
        bucket["total_tokens"] = sum(bucket[key] for key in COUNTERS)
    return {"recorded": bool(entries), **total, **{name: dict(sorted(group.items())) for name, group in groups.items()}}
