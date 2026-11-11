"""Stable CLI exit codes. Every ``ErrorCode`` maps to exactly one of them."""

from __future__ import annotations

from dridh.common.errors import ErrorCode

SUCCESS = 0
INVALID_INPUT = 2          # invalid user input or configuration
PREREQUISITE_MISSING = 3   # a tool, workspace or schema step is missing
VALIDATION_FAILED = 4      # a document, plan or acceptance check did not pass
APPROVAL_REQUIRED = 5      # a human must approve or confirm in a terminal
PROVIDER_FAILURE = 6       # a model, process or integration failed
CONFLICT = 7               # workflow conflict, stale state or a dependency that is not ready
INTERNAL_FAILURE = 8       # a bug or corrupt state

EXIT_CODE_BY_ERROR: dict[ErrorCode, int] = {
    ErrorCode.CONFIGURATION_ERROR: INVALID_INPUT,
    ErrorCode.PROTOCOL_ERROR: INVALID_INPUT,
    ErrorCode.WORKFLOW_NOT_FOUND: INVALID_INPUT,
    ErrorCode.WORKSPACE_NOT_FOUND: PREREQUISITE_MISSING,
    ErrorCode.DEPENDENCY_UNAVAILABLE: PREREQUISITE_MISSING,
    ErrorCode.SCHEMA_UNSUPPORTED: PREREQUISITE_MISSING,
    ErrorCode.VALIDATION_FAILED: VALIDATION_FAILED,
    ErrorCode.PLAN_INVALID: VALIDATION_FAILED,
    ErrorCode.ACCEPTANCE_FAILED: VALIDATION_FAILED,
    ErrorCode.APPROVAL_REQUIRED: APPROVAL_REQUIRED,
    ErrorCode.AUTHORIZATION_DENIED: APPROVAL_REQUIRED,
    ErrorCode.PROCESS_EXECUTION_FAILED: PROVIDER_FAILURE,
    ErrorCode.WORKER_FAILED: PROVIDER_FAILURE,
    ErrorCode.WORKER_TIMEOUT: PROVIDER_FAILURE,
    ErrorCode.JEV_UNAVAILABLE: PROVIDER_FAILURE,
    ErrorCode.CONFIGURATION_CONFLICT: CONFLICT,
    ErrorCode.INVALID_TRANSITION: CONFLICT,
    ErrorCode.DEPENDENCY_NOT_READY: CONFLICT,
    ErrorCode.SESSION_UNSAFE: CONFLICT,
    ErrorCode.CANCELLED: CONFLICT,
    ErrorCode.INTERNAL_ERROR: INTERNAL_FAILURE,
    ErrorCode.STATE_CORRUPT: INTERNAL_FAILURE,
}


def exit_code_for(code: ErrorCode) -> int:
    return EXIT_CODE_BY_ERROR[code]
