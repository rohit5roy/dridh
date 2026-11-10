"""Capture raw model replies from live (Category B) tests so failures are diagnosable.

Live tests call a real, non-deterministic model. When it returns something the validators reject,
the only useful evidence is what it actually said, so every reply is recorded and printed in full
(bounded) on failure. Secret values from the environment are redacted from the output.
"""
from __future__ import annotations

import json
import os
from typing import Any

_MAX_REPLY_CHARS = 40000
_SECRET_ENV = ("OPENROUTER_API_KEY", "OPENCODE_API_KEY")


def _redact(text: str) -> str:
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


class ReplyRecorder:
    """Wrap an OpenCodeRunner, keeping each worker's raw reply (or failure) for later display."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner
        self.replies: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:  # delegate everything else to the real runner
        return getattr(self.runner, name)

    def run_sync(self, **kwargs: Any):
        entry: dict[str, Any] = {"agent": kwargs.get("agent"), "model": kwargs.get("model")}
        self.replies.append(entry)
        try:
            result = self.runner.run_sync(**kwargs)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["details"] = [str(item) for item in getattr(exc, "details", ()) or ()]
            raise
        entry["exit_code"] = result.exit_code
        entry["text"] = result.text
        return result

    def dump(self) -> str:
        if not self.replies:
            return "(no model replies were recorded)"
        blocks = []
        for index, entry in enumerate(self.replies, start=1):
            head = f"[{index}] agent={entry.get('agent')} model={entry.get('model')}"
            if "error" in entry:
                body = f"RUNNER ERROR: {entry['error']}\ndetails: {json.dumps(entry['details'], ensure_ascii=False)}"
            else:
                text = str(entry.get("text", ""))
                clipped = text[:_MAX_REPLY_CHARS] + (f"\n...[{len(text) - _MAX_REPLY_CHARS} more chars]" if len(text) > _MAX_REPLY_CHARS else "")
                body = f"exit_code={entry.get('exit_code')} reply_chars={len(text)}\n{clipped}"
            blocks.append(f"{head}\n{body}")
        return _redact("\n\n".join(blocks))


def failure_message(summary: str, payload: Any, recorder: ReplyRecorder) -> str:
    """Full (untruncated) state plus every raw model reply."""
    state = _redact(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    return f"{summary}\n--- state ---\n{state}\n--- raw model replies ---\n{recorder.dump()}"
