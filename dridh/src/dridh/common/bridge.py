"""One-request-per-process JSON bridge runner.

Exactly one response envelope is written to stdout. All diagnostics go to
stderr so adapters can treat stdout as a machine-readable protocol stream.
"""

from __future__ import annotations

import json
import signal
import sys
from types import FrameType
from typing import TextIO

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.logging import StructuredLogger, configure_logging
from dridh.common.protocol import BridgeFailure, failure_envelope, handle_request, serialise_response


def _request_operation(raw: str | bytes) -> str | None:
    """Best-effort operation name for diagnostics; never affects the response."""

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    operation = parsed.get("operation")
    return operation if isinstance(operation, str) else None


def _request_id(raw: str | bytes) -> str:
    """Best-effort request ID for the cancellation envelope; never affects other responses."""

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return "unavailable"
    value = parsed.get("request_id") if isinstance(parsed, dict) else None
    return value if isinstance(value, str) and value else "unavailable"


def _raise_cancelled(signum: int, frame: FrameType | None) -> None:
    """Turn SIGTERM into an interruption so in-flight worker process groups are terminated."""

    raise KeyboardInterrupt(f"signal {signum}")


def run_bridge(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    logger: StructuredLogger | None = None,
) -> int:
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr
    log = logger or configure_logging(stream=error_stream)

    raw = input_stream.read()
    operation = _request_operation(raw)
    # Start a parent watchdog: if the OpenCode process that spawned this bridge dies,
    # terminate all worker process groups. This handles the SIGKILL residual (SF-01).
    import os

    from dridh.common.watchdog import start_parent_watchdog

    parent_pid = os.getppid()
    start_parent_watchdog(parent_pid=parent_pid, interval=1.0, grace=2.0)

    previous = _install_cancellation_handler()
    try:
        response = handle_request(raw)
    except KeyboardInterrupt:
        response = failure_envelope(
            DridhError(
                ErrorCode.CANCELLED,
                "The bridge request was cancelled before it completed.",
                details=("in-flight worker processes were terminated",),
            ),
            _request_id(raw),
        )
    finally:
        _restore_handler(previous)

    try:
        output_stream.write(serialise_response(response) + "\n")
        output_stream.flush()
    except BrokenPipeError:
        return 0

    if isinstance(response, BridgeFailure):
        log.error(
            "bridge.request.failed",
            "Bridge request rejected.",
            correlation_id=response.request_id,
            operation=operation,
            error_code=response.error.code,
            detail_count=len(response.error.details),
        )
    else:
        log.info(
            "bridge.request.succeeded",
            "Bridge request completed.",
            correlation_id=response.request_id,
            operation=operation,
        )
    # Exit status 0 means one envelope was written. Callers decide success from `ok`.
    return 0


def _install_cancellation_handler() -> object:
    try:
        return signal.signal(signal.SIGTERM, _raise_cancelled)
    except ValueError:
        return None


def _restore_handler(previous: object) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)  # type: ignore[arg-type]
    except ValueError:
        return


def main() -> int:
    return run_bridge()
