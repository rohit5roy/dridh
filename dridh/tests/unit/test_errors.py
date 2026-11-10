from __future__ import annotations

import pytest

from dridh.common.errors import (
    DEFAULT_RETRYABLE,
    RECOVERY_GUIDANCE,
    DridhError,
    ErrorCode,
    internal_error,
)


def test_all_stable_error_categories_exist() -> None:
    # Phase 1 categories are a stable prefix. Phase 2 appends its categories; it never reorders them.
    values = [code.value for code in ErrorCode]
    assert values[:7] == [
        "CONFIGURATION_ERROR",
        "WORKSPACE_NOT_FOUND",
        "VALIDATION_FAILED",
        "DEPENDENCY_UNAVAILABLE",
        "PROCESS_EXECUTION_FAILED",
        "PROTOCOL_ERROR",
        "INTERNAL_ERROR",
    ]
    assert values[7:] == [
        "CONFIGURATION_CONFLICT",
        "STATE_CORRUPT",
        "SCHEMA_UNSUPPORTED",
        "WORKFLOW_NOT_FOUND",
        "INVALID_TRANSITION",
        "PLAN_INVALID",
        "APPROVAL_REQUIRED",
        "DEPENDENCY_NOT_READY",
        "AUTHORIZATION_DENIED",
        "WORKER_FAILED",
        "WORKER_TIMEOUT",
        "ACCEPTANCE_FAILED",
        "CANCELLED",
        "JEV_UNAVAILABLE",
        "SESSION_UNSAFE",
    ]
    assert set(DEFAULT_RETRYABLE) == set(ErrorCode)
    assert set(RECOVERY_GUIDANCE) == set(ErrorCode)


def test_error_serialises_machine_readable_fields_and_recovery() -> None:
    error = DridhError(
        ErrorCode.VALIDATION_FAILED,
        "Invalid request with token=supersecret",
        details=("field: api_key=abc123",),
    )
    payload = error.to_dict()
    assert payload["code"] == "VALIDATION_FAILED"
    assert payload["retryable"] is False
    assert payload["recovery"]
    assert "supersecret" not in payload["message"]
    assert "abc123" not in payload["details"][0]


def test_retryable_defaults_can_be_overridden() -> None:
    assert DridhError(ErrorCode.DEPENDENCY_UNAVAILABLE, "down").retryable is True
    assert DridhError(ErrorCode.DEPENDENCY_UNAVAILABLE, "down", retryable=False).retryable is False


def test_unknown_category_is_rejected() -> None:
    with pytest.raises(ValueError):
        DridhError("NOT_A_CODE", "bad")


def test_internal_error_hides_raw_exception_text() -> None:
    error = internal_error(RuntimeError("password=hunter2"))
    payload = error.to_dict()
    assert payload["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in str(payload)
    assert payload["details"] == ["RuntimeError"]
