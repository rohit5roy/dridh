"""Structured JSON logging with correlation metadata and redaction."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, TextIO

from dridh.common.redaction import redact

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


@dataclass(frozen=True)
class LogContext:
    workspace_id: str | None = None
    feature_id: str | None = None
    story_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    correlation_id: str | None = None
    operation: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "workspace_id": self.workspace_id,
            "feature_id": self.feature_id,
            "story_id": self.story_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "correlation_id": self.correlation_id,
            "operation": self.operation,
        }


class StructuredLogger:
    """Minimal JSON-lines logger that always writes diagnostics to one stream."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        context: LogContext | None = None,
        minimum_level: str = "INFO",
        service: str = "dridh",
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._context = context or LogContext()
        self._minimum_level = _LEVELS.get(minimum_level.upper(), 20)
        self._service = service

    def bind(self, **fields: str | None) -> StructuredLogger:
        known = {key: value for key, value in fields.items() if key in LogContext.__dataclass_fields__}
        return StructuredLogger(
            stream=self._stream,
            context=replace(self._context, **known),
            minimum_level=next(
                (name for name, value in _LEVELS.items() if value == self._minimum_level),
                "INFO",
            ),
            service=self._service,
        )

    def log(self, event: str, message: str, *, severity: str = "INFO", **fields: Any) -> None:
        level = severity.upper()
        if _LEVELS.get(level, 20) < self._minimum_level:
            return
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "severity": level,
            "service": self._service,
            **self._context.as_dict(),
            "event": event,
            "message": message,
            **fields,
        }
        safe = redact(record)
        self._stream.write(json.dumps(safe, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._stream.flush()

    def debug(self, event: str, message: str, **fields: Any) -> None:
        self.log(event, message, severity="DEBUG", **fields)

    def info(self, event: str, message: str, **fields: Any) -> None:
        self.log(event, message, severity="INFO", **fields)

    def warning(self, event: str, message: str, **fields: Any) -> None:
        self.log(event, message, severity="WARNING", **fields)

    def error(self, event: str, message: str, **fields: Any) -> None:
        self.log(event, message, severity="ERROR", **fields)


def configure_logging(
    *,
    level: str = "INFO",
    stream: TextIO | None = None,
    service: str = "dridh",
) -> StructuredLogger:
    return StructuredLogger(stream=stream, minimum_level=level, service=service)
