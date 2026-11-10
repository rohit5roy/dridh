"""Stable error categories and the shared Dridh exception type."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from dridh.common.redaction import redact


class ErrorCode(str, Enum):
    """Stable, machine-readable error categories."""

    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    PROCESS_EXECUTION_FAILED = "PROCESS_EXECUTION_FAILED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIGURATION_CONFLICT = "CONFIGURATION_CONFLICT"
    STATE_CORRUPT = "STATE_CORRUPT"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    PLAN_INVALID = "PLAN_INVALID"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    WORKER_FAILED = "WORKER_FAILED"
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    ACCEPTANCE_FAILED = "ACCEPTANCE_FAILED"
    CANCELLED = "CANCELLED"
    JEV_UNAVAILABLE = "JEV_UNAVAILABLE"
    SESSION_UNSAFE = "SESSION_UNSAFE"


DEFAULT_RETRYABLE: dict[ErrorCode, bool] = {
    ErrorCode.CONFIGURATION_ERROR: False,
    ErrorCode.WORKSPACE_NOT_FOUND: False,
    ErrorCode.VALIDATION_FAILED: False,
    ErrorCode.DEPENDENCY_UNAVAILABLE: True,
    ErrorCode.PROCESS_EXECUTION_FAILED: False,
    ErrorCode.PROTOCOL_ERROR: False,
    ErrorCode.INTERNAL_ERROR: False,
    ErrorCode.CONFIGURATION_CONFLICT: False,
    ErrorCode.STATE_CORRUPT: False,
    ErrorCode.SCHEMA_UNSUPPORTED: False,
    ErrorCode.WORKFLOW_NOT_FOUND: False,
    ErrorCode.INVALID_TRANSITION: False,
    ErrorCode.PLAN_INVALID: False,
    ErrorCode.APPROVAL_REQUIRED: False,
    ErrorCode.DEPENDENCY_NOT_READY: True,
    ErrorCode.AUTHORIZATION_DENIED: False,
    ErrorCode.WORKER_FAILED: True,
    ErrorCode.WORKER_TIMEOUT: True,
    ErrorCode.ACCEPTANCE_FAILED: False,
    ErrorCode.CANCELLED: False,
    ErrorCode.JEV_UNAVAILABLE: True,
    ErrorCode.SESSION_UNSAFE: False,
}

RECOVERY_GUIDANCE: dict[ErrorCode, str] = {
    ErrorCode.CONFIGURATION_ERROR: (
        "Check the Dridh configuration and required environment variables, then retry."
    ),
    ErrorCode.WORKSPACE_NOT_FOUND: (
        "Verify that the workspace path exists, is a directory, and is accessible, then retry."
    ),
    ErrorCode.VALIDATION_FAILED: (
        "Correct the request fields identified in details, then retry."
    ),
    ErrorCode.DEPENDENCY_UNAVAILABLE: (
        "Install or start the required dependency, then retry."
    ),
    ErrorCode.PROCESS_EXECUTION_FAILED: (
        "Inspect the redacted stderr diagnostics, verify the executable and arguments, then retry."
    ),
    ErrorCode.PROTOCOL_ERROR: (
        "Send exactly one valid protocol v1 JSON request envelope and retry."
    ),
    ErrorCode.INTERNAL_ERROR: (
        "Capture the correlation ID for diagnosis and retry only after the cause is understood."
    ),
}


RECOVERY_GUIDANCE.update(
    {
        ErrorCode.CONFIGURATION_CONFLICT: (
            "Review the listed files or settings, resolve the conflict, then retry."
        ),
        ErrorCode.STATE_CORRUPT: (
            "Keep the damaged files for diagnosis. Restore them from a backup or reinitialise the workspace, then retry."
        ),
        ErrorCode.SCHEMA_UNSUPPORTED: (
            "Use a Dridh version that supports this schema, or run the documented migration, then retry."
        ),
        ErrorCode.WORKFLOW_NOT_FOUND: (
            "Check the workflow ID and the workspace root, then retry."
        ),
        ErrorCode.INVALID_TRANSITION: (
            "Inspect the workflow status. The requested change is not allowed from its current state."
        ),
        ErrorCode.PLAN_INVALID: (
            "Correct the plan fields listed in details, or ask the Planner for a revised plan, then retry."
        ),
        ErrorCode.APPROVAL_REQUIRED: (
            "A human must approve the plan in the terminal with dridh workflow approve before execution continues."
        ),
        ErrorCode.DEPENDENCY_NOT_READY: (
            "Wait for the listed dependencies to complete, or inspect why they have not, then retry."
        ),
        ErrorCode.AUTHORIZATION_DENIED: (
            "This caller is not allowed to perform the operation. Use the documented interface for it."
        ),
        ErrorCode.WORKER_FAILED: (
            "Inspect the worker run record and its redacted diagnostics, then retry the task if attempts remain."
        ),
        ErrorCode.WORKER_TIMEOUT: (
            "The worker exceeded its time limit. Retry the task, or raise the configured worker timeout."
        ),
        ErrorCode.ACCEPTANCE_FAILED: (
            "A deterministic acceptance check failed. Inspect the check results, correct the work, then retry."
        ),
        ErrorCode.CANCELLED: (
            "The operation was cancelled by the caller. No further action is needed unless you want to resume."
        ),
        ErrorCode.SESSION_UNSAFE: (
            "The stored session cannot safely be continued. Start a new session, or resume with --fresh-if-unsafe to record a fresh one."
        ),
        ErrorCode.JEV_UNAVAILABLE: (
            "The optional Jev service did not answer within its bounds. The deterministic fallback was used."
        ),
    }
)


def _normalise_details(details: Iterable[object] | None) -> tuple[str, ...]:
    if not details:
        return ()
    normalised: list[str] = []
    for detail in details:
        text = detail if isinstance(detail, str) else repr(detail)
        normalised.append(str(redact(text)))
    return tuple(normalised)


class DridhError(Exception):
    """An error with a stable category, safe details and recovery guidance."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: Iterable[object] | None = None,
        retryable: bool | None = None,
        recovery: str | None = None,
    ) -> None:
        self.code = ErrorCode(code)
        self.message = str(redact(message))
        self.details = _normalise_details(details)
        self.retryable = DEFAULT_RETRYABLE[self.code] if retryable is None else retryable
        self.recovery = recovery or RECOVERY_GUIDANCE[self.code]
        super().__init__(self.message)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": list(self.details),
            "retryable": self.retryable,
            "recovery": self.recovery,
        }

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


def from_agent_error(exc: BaseException) -> DridhError | None:
    """A typed DridhError for a model-runner failure (``AgentRunError``), else None.

    Found live with a slower model: a worker timeout escaped the Designer and reached the agent and the person as
    "An unexpected internal error occurred", with nothing saying it was the model, or that a retry would work."""
    from dridh.integrations.opencode import AgentErrorCode, AgentRunError

    if not isinstance(exc, AgentRunError):
        return None
    code = {AgentErrorCode.TIMEOUT: ErrorCode.WORKER_TIMEOUT, AgentErrorCode.CANCELLED: ErrorCode.CANCELLED,
            AgentErrorCode.OPENCODE_UNAVAILABLE: ErrorCode.DEPENDENCY_UNAVAILABLE,
            AgentErrorCode.QUOTA_EXHAUSTED: ErrorCode.DEPENDENCY_UNAVAILABLE}.get(exc.code, ErrorCode.WORKER_FAILED)
    return DridhError(code, f"The model worker failed ({exc.code.value}): {exc.message}", details=exc.details,
                      retryable=exc.retryable or exc.code is AgentErrorCode.TIMEOUT, recovery=exc.recovery)


def internal_error(exc: BaseException) -> DridhError:
    """Wrap an unexpected exception without exposing its raw text (model-runner failures keep their meaning)."""

    typed = from_agent_error(exc)
    if typed is not None:
        return typed
    return DridhError(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected internal error occurred.",
        details=(f"{type(exc).__name__}",),
    )
