"""Typed OpenCode V2 agent invocation and resume.

The runner is a deliberately thin adapter over the supported CLI:

    opencode --standalone run --agent <agent> --format json [--session <id>] <prompt>

It parses the JSONL event stream, extracts the session ID and final text, and
classifies invocation, transport, timeout, provider, quota and cancellation
failures into stable categories. It does not use native multi-agent depth-2.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from dridh.common.redaction import redact
from dridh.common.subprocess import ProcessExecutionError, run_process
from dridh.observability.usage import record_usage, usage_from_stdout
from dridh.workspace.paths import validate_workspace_root

_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9_-]{8,}$")
_QUOTA_MARKERS = (
    "free usage limit",
    "quota exhausted",
    "rate limit exceeded",
    "too many requests",
)
_PROVIDER_MARKERS = (
    "provider",
    "model error",
    "api error",
    "upstream",
    "authentication",
    "unauthorized",
    "invalid api key",
)


class AgentErrorCode(str, Enum):
    """Stable categories for agent-runner failures."""

    INVALID_INVOCATION = "INVALID_INVOCATION"
    OPENCODE_UNAVAILABLE = "OPENCODE_UNAVAILABLE"
    PROCESS_FAILURE = "PROCESS_FAILURE"
    TIMEOUT = "TIMEOUT"
    SESSION_EXTRACTION_FAILED = "SESSION_EXTRACTION_FAILED"
    SESSION_RESUME_FAILED = "SESSION_RESUME_FAILED"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    CANCELLED = "CANCELLED"


_RECOVERY = {
    AgentErrorCode.INVALID_INVOCATION: "Correct the agent, prompt, session ID or workspace path and retry.",
    AgentErrorCode.OPENCODE_UNAVAILABLE: "Install OpenCode V2 or set DRIDH_OPENCODE_BIN to its executable.",
    AgentErrorCode.PROCESS_FAILURE: "Inspect the redacted stderr diagnostics and the OpenCode process exit code.",
    AgentErrorCode.TIMEOUT: "Increase the bounded timeout or retry after the provider is responsive.",
    AgentErrorCode.SESSION_EXTRACTION_FAILED: "Inspect OpenCode JSON output; do not retry blindly.",
    AgentErrorCode.SESSION_RESUME_FAILED: "Verify the session ID belongs to the same agent and workspace.",
    AgentErrorCode.MALFORMED_OUTPUT: "Inspect the OpenCode version/output format; do not parse partial output.",
    AgentErrorCode.PROVIDER_FAILURE: "Check provider/model availability and credentials, then retry with a bounded policy.",
    AgentErrorCode.QUOTA_EXHAUSTED: "Wait for quota reset or select an available model; do not retry in a tight loop.",
    AgentErrorCode.CANCELLED: "No action required; the caller cancelled the run.",
}


class AgentRunError(Exception):
    """A typed agent-runner failure with safe details."""

    def __init__(
        self,
        code: AgentErrorCode,
        message: str,
        *,
        details: Sequence[str] = (),
        retryable: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.code = code
        self.message = str(redact(message))
        self.details = tuple(str(redact(item)) for item in details)
        self.retryable = retryable
        self.session_id = session_id
        self.recovery = _RECOVERY[code]
        super().__init__(f"{code.value}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": list(self.details),
            "retryable": self.retryable,
            "session_id": self.session_id,
            "recovery": self.recovery,
        }


@dataclass(frozen=True)
class AgentRunResult:
    """Normalised result of one one-shot agent invocation."""

    agent: str
    prompt: str
    session_id: str
    text: str
    exit_code: int
    duration_seconds: float
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    stderr: str = ""
    resumed: bool = False
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "prompt": self.prompt,
            "session_id": self.session_id,
            "text": self.text,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "event_count": len(self.events),
            "stderr": self.stderr,
            "resumed": self.resumed,
            "usage": dict(self.usage),
        }


def _resolve_executable(executable: str) -> str:
    if not executable:
        raise AgentRunError(
            AgentErrorCode.OPENCODE_UNAVAILABLE,
            "OpenCode executable is not configured.",
            details=("set DRIDH_OPENCODE_BIN or pass executable=",),
        )
    candidate = Path(executable)
    if candidate.is_absolute() or os.sep in executable:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise AgentRunError(
                AgentErrorCode.OPENCODE_UNAVAILABLE,
                "OpenCode executable does not exist.",
                details=(f"executable: {executable}",),
            ) from None
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise AgentRunError(
                AgentErrorCode.OPENCODE_UNAVAILABLE,
                "OpenCode executable is not executable.",
                details=(f"executable: {executable}",),
            )
        return str(resolved)
    found = shutil.which(executable)
    if not found:
        raise AgentRunError(
            AgentErrorCode.OPENCODE_UNAVAILABLE,
            "OpenCode executable was not found on PATH.",
            details=(f"executable: {executable}",),
        )
    return str(Path(found).resolve())


def _session_id_from_events(events: Sequence[dict[str, Any]]) -> str | None:
    for event in events:
        value = event.get("sessionID")
        if isinstance(value, str) and _SESSION_ID.match(value):
            return value
        part = event.get("part")
        if isinstance(part, dict):
            value = part.get("sessionID")
            if isinstance(value, str) and _SESSION_ID.match(value):
                return value
    return None


def _text_from_events(events: Sequence[dict[str, Any]]) -> str:
    texts: list[str] = []
    for event in events:
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts).strip()


def _error_text(events: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for event in events:
        if event.get("type") not in {"error", "session.error", "session.execution.failed"}:
            continue
        part = event.get("part")
        candidate = part if isinstance(part, dict) else event
        for key in ("message", "error", "reason"):
            value = candidate.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                for inner in ("type", "name", "message"):
                    detail = value.get(inner)
                    if isinstance(detail, str):
                        parts.append(detail)
    return "\n".join(parts)


def _last_event_types(stdout: str, count: int = 5) -> str:
    types: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            types.append("non-json")
            continue
        types.append(str(event.get("type")) if isinstance(event, dict) else "non-object")
    return ",".join(types[-count:]) or "none"


def _failure_evidence(stdout: str, stderr: str) -> str:
    """Text that can legitimately describe a failure: error events, stderr and non-JSON lines.

    Tool results and assistant text are excluded: a worker that merely reads a file mentioning
    "too many requests" must not be classified as a quota failure.
    """
    events: list[dict[str, Any]] = []
    plain: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            plain.append(line)
            continue
        if isinstance(event, dict):
            events.append(event)
    return "\n".join([_error_text(events), *plain, stderr])


def _classify_known_failure(stdout: str, stderr: str) -> AgentErrorCode | None:
    haystack = _failure_evidence(stdout, stderr).lower()
    if any(marker in haystack for marker in _QUOTA_MARKERS):
        return AgentErrorCode.QUOTA_EXHAUSTED
    if any(marker in haystack for marker in _PROVIDER_MARKERS):
        return AgentErrorCode.PROVIDER_FAILURE
    return None


def _provider_diagnostic(stdout: str, stderr: str) -> str:
    """Extract provider error text without returning the whole assistant stream."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    structured = _error_text(events)
    if structured:
        return structured[:4000]

    diagnostic_markers = (*_QUOTA_MARKERS, *_PROVIDER_MARKERS, "unauthorized", "insufficient", "error")
    candidates = [
        line.strip()
        for line in _failure_evidence(stdout, stderr).splitlines()
        if any(marker in line.lower() for marker in diagnostic_markers)
    ]
    return "\n".join(candidates[-4:])[:4000]


class OpenCodeRunner:
    """Invoke and resume OpenCode agents through the supported CLI."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        workspace_root: str | Path,
        config_dir: str | Path | None = None,
        timeout: float = 120.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = _resolve_executable(
            executable or os.environ.get("DRIDH_OPENCODE_BIN", "opencode")
        )
        try:
            self.workspace_root = validate_workspace_root(workspace_root)
        except Exception as exc:  # noqa: BLE001 - map to runner category
            raise AgentRunError(
                AgentErrorCode.INVALID_INVOCATION,
                "workspace_root is not a valid directory.",
                details=(str(exc),),
            ) from None
        if timeout <= 0:
            raise AgentRunError(
                AgentErrorCode.INVALID_INVOCATION,
                "timeout must be greater than zero.",
            )
        self.timeout = float(timeout)
        self.config_dir: Path | None = None
        if config_dir is not None:
            candidate = Path(config_dir)
            if not candidate.is_dir():
                raise AgentRunError(
                    AgentErrorCode.INVALID_INVOCATION,
                    "config_dir is not a directory.",
                    details=(f"config_dir: {candidate}",),
                )
            self.config_dir = candidate.resolve()
        self.env = {str(k): str(v) for k, v in (env or {}).items()}

    def _child_env(self, root: Path | None = None) -> dict[str, str]:
        child = dict(self.env)
        child.setdefault("CI", "1")
        child.setdefault("NO_COLOR", "1")
        if self.config_dir is not None:
            child["OPENCODE_CONFIG_DIR"] = str(self.config_dir)
        # OpenCode takes its working directory from $PWD, not from the process's real working directory. An inherited
        # PWD (the directory Dridh was started from) would send an Executor's edits outside its story worktree.
        child["PWD"] = str(root if root is not None else self.workspace_root)
        return child

    def _redact_diagnostic(self, value: str) -> str:
        """Redact known runtime credentials before surfacing a provider error."""
        safe = str(redact(value))
        for key in ("OPENCODE_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL", "OPENROUTER_API_KEY", "DRIDH_JEV_API_KEY"):
            secret = self.env.get(key) or os.environ.get(key, "")
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
        return safe[-4000:]

    def _build_args(
        self,
        *,
        agent: str,
        prompt: str,
        session_id: str | None,
        model: str | None,
        title: str | None,
    ) -> list[str]:
        if not agent or not agent.strip():
            raise AgentRunError(AgentErrorCode.INVALID_INVOCATION, "agent must not be empty.")
        if not prompt or not prompt.strip():
            raise AgentRunError(AgentErrorCode.INVALID_INVOCATION, "prompt must not be empty.")
        if session_id is not None and not _SESSION_ID.match(session_id):
            raise AgentRunError(
                AgentErrorCode.INVALID_INVOCATION,
                "session_id is not a valid OpenCode session ID.",
                details=(f"session_id: {session_id}",),
            )
        args = [
            self.executable,
            "--standalone",
            "run",
            "--agent",
            agent.strip(),
            "--format",
            "json",
        ]
        if session_id is not None:
            args.extend(["--session", session_id])
        if model:
            args.extend(["--model", model])
        if title:
            args.extend(["--title", title])
        # The prompt is deliberately NOT part of argv. OpenCode's `run` command re-quotes any
        # positional message containing a space ("..." with inner quotes backslash-escaped), which
        # corrupts structured prompts (e.g. canonical JSON) before they reach the model. The prompt is
        # delivered on stdin instead, which OpenCode passes through verbatim. This also keeps large
        # prompts clear of OS argument-size limits and out of process listings.
        return args

    async def run(
        self,
        *,
        agent: str,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        title: str | None = None,
        worktree_path: str | Path | None = None,
    ) -> AgentRunResult:
        args = self._build_args(
            agent=agent,
            prompt=prompt,
            session_id=session_id,
            model=model,
            title=title,
        )
        root = Path(worktree_path) if worktree_path is not None else self.workspace_root
        try:
            process = await run_process(
                self.executable,
                args[1:],
                cwd=root,
                allowed_roots=(root,),
                env=self._child_env(root),
                timeout=self.timeout,
                input_text=prompt,
            )
        except asyncio.CancelledError:
            raise AgentRunError(
                AgentErrorCode.CANCELLED,
                "Agent invocation was cancelled.",
                retryable=False,
            ) from None
        except ProcessExecutionError as exc:
            if exc.timed_out:
                raise AgentRunError(
                    AgentErrorCode.TIMEOUT,
                    f"Agent invocation timed out after {self.timeout} seconds.",
                    retryable=True,
                    details=tuple(exc.details),
                ) from None
            if "executable" in exc.message.lower():
                raise AgentRunError(
                    AgentErrorCode.OPENCODE_UNAVAILABLE,
                    exc.message,
                    details=tuple(exc.details),
                ) from None
            known = _classify_known_failure("", exc.message + "\n" + "\n".join(exc.details))
            if known is not None:
                raise AgentRunError(
                    known,
                    "Agent process failed with a provider/quota condition.",
                    retryable=known is AgentErrorCode.PROVIDER_FAILURE,
                    details=tuple(exc.details),
                ) from None
            raise AgentRunError(
                AgentErrorCode.PROCESS_FAILURE,
                "Agent process exited unsuccessfully.",
                retryable=False,
                details=tuple(exc.details),
            ) from None

        usage = usage_from_stdout(process.stdout)
        try:
            result = self._result(process, agent=agent, prompt=prompt, session_id=session_id, usage=usage)
        except AgentRunError as exc:
            record_usage(self.workspace_root, agent=agent, model=model, session_id=exc.session_id or session_id, title=title,
                         outcome=exc.code.value, duration_seconds=process.duration_seconds, usage=usage, resumed=session_id is not None)
            raise
        record_usage(self.workspace_root, agent=agent, model=model, session_id=result.session_id, title=title, outcome="OK",
                     duration_seconds=process.duration_seconds, usage=usage, resumed=result.resumed)
        return result

    def _result(self, process: Any, *, agent: str, prompt: str, session_id: str | None, usage: dict[str, Any]) -> AgentRunResult:
        """Turn a finished OpenCode process into a result, or raise the classified failure."""
        if process.exit_code != 0:
            known = _classify_known_failure(process.stdout, process.stderr)
            diagnostic = self._redact_diagnostic(_provider_diagnostic(process.stdout, process.stderr))
            stderr = self._redact_diagnostic(process.stderr[-2000:])
            details = [f"exit_code: {process.exit_code}", f"last_event_types: {_last_event_types(process.stdout)}"]
            if diagnostic:
                details.append(f"provider_diagnostic: {diagnostic}")
            if stderr:
                details.append(f"stderr: {stderr}")
            if known is not None:
                raise AgentRunError(
                    known,
                    "Agent process reported a provider/quota failure.",
                    retryable=known is AgentErrorCode.PROVIDER_FAILURE,
                    details=details,
                )
            raise AgentRunError(
                AgentErrorCode.PROCESS_FAILURE,
                "Agent process exited unsuccessfully.",
                details=details,
            )

        events: list[dict[str, Any]] = []
        malformed_lines = 0
        for line in process.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                malformed_lines += 1

        if not events and process.stdout.strip():
            raise AgentRunError(
                AgentErrorCode.MALFORMED_OUTPUT,
                "OpenCode produced no parseable JSON events.",
                details=(f"malformed_lines: {malformed_lines}",),
            )

        session = _session_id_from_events(events)
        if session is None:
            raise AgentRunError(
                AgentErrorCode.SESSION_EXTRACTION_FAILED,
                "OpenCode output did not contain a valid session ID.",
                details=(f"exit_code: {process.exit_code}",),
            )
        if session_id is not None and session != session_id:
            raise AgentRunError(
                AgentErrorCode.SESSION_RESUME_FAILED,
                "OpenCode did not preserve the requested session ID.",
                details=(f"requested: {session_id}", f"returned: {session}"),
                session_id=session,
            )

        text = _text_from_events(events)
        known = None
        if _error_text(events):
            known = _classify_known_failure(process.stdout, process.stderr)
        if known is None and any(marker in text.lower() for marker in _QUOTA_MARKERS):
            known = AgentErrorCode.QUOTA_EXHAUSTED
        if known is not None:
            raise AgentRunError(
                known,
                "Agent output reported a provider/quota failure.",
                retryable=known is AgentErrorCode.PROVIDER_FAILURE,
                details=(f"exit_code: {process.exit_code}",),
                session_id=session,
            )
        if not text:
            raise AgentRunError(
                AgentErrorCode.MALFORMED_OUTPUT,
                "OpenCode events did not contain assistant text.",
                details=(f"event_count: {len(events)}",),
                session_id=session,
            )

        return AgentRunResult(
            agent=agent,
            prompt=prompt,
            session_id=session,
            text=text,
            exit_code=process.exit_code,
            duration_seconds=process.duration_seconds,
            events=tuple(events),
            stderr=str(redact(process.stderr[-4000:])),
            resumed=session_id is not None,
            usage=usage,
        )

    def run_sync(self, **kwargs: Any) -> AgentRunResult:
        """Synchronous convenience wrapper for CLI and test callers."""

        return asyncio.run(self.run(**kwargs))
