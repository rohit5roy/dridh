"""Phase 1.5 security regression suite for credential redaction."""

from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.logging import StructuredLogger
from dridh.common.redaction import REDACTED, redact
from dridh.common.subprocess import ProcessExecutionError, run_process

SECRET = "SYNTHETIC_SECRET_VALUE_9f3a7c1e"
SENSITIVE_KEYS = [
    "api_key",
    "apiKey",
    "openai_api_key",
    "OPENAI_API_KEY",
    "db_password",
    "secret_key",
    "aws_secret_access_key",
    "accessToken",
    "access_token",
    "refresh_token",
    "Authorization",
    "dbPassword",
    "x-api-key",
    "aws_access_key_id",
    "clientSecret",
    "session_key",
    "private_key",
    "credential",
    "credentials",
]


@pytest.mark.parametrize("key", SENSITIVE_KEYS)
def test_sensitive_key_variants_are_redacted(key: str) -> None:
    result = redact({key: SECRET})
    serialised = json.dumps(result)
    assert SECRET not in serialised
    assert REDACTED in serialised


def test_nested_objects_and_arrays_are_redacted() -> None:
    value = {
        "config": {
            "providers": [
                {"openai_api_key": SECRET},
                {"nested": {"db_password": SECRET}},
            ],
            "matrix": [[{"aws_secret_access_key": SECRET}]],
        }
    }
    serialised = json.dumps(redact(value))
    assert SECRET not in serialised
    assert serialised.count(REDACTED) >= 3


@pytest.mark.parametrize(
    "text",
    [
        f"openai_api_key={SECRET}",
        f"db_password={SECRET}",
        f"secret_key={SECRET}",
        f"aws_secret_access_key={SECRET}",
        f"OPENAI_API_KEY={SECRET}",
        f"accessToken={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f'"aws_secret_access_key": "{SECRET}"',
        f"--api-key {SECRET}",
        f"--db-password={SECRET}",
    ],
)
def test_sensitive_text_forms_are_redacted(text: str) -> None:
    result = redact(text)
    assert SECRET not in result
    assert REDACTED in result


@pytest.mark.parametrize(
    "text",
    [
        "token_count=12",
        "max_tokens=100",
        "secretary=alice",
        "monkey=banana",
    ],
)
def test_benign_names_are_preserved(text: str) -> None:
    assert redact(text) == text


def test_exception_messages_are_redacted() -> None:
    error = DridhError(
        ErrorCode.VALIDATION_FAILED,
        f"request failed: openai_api_key={SECRET}",
        details=(f"db_password={SECRET}",),
    )
    payload = error.to_dict()
    assert SECRET not in json.dumps(payload)
    assert REDACTED in json.dumps(payload)


def test_structured_log_fields_are_redacted() -> None:
    stream = io.StringIO()
    logger = StructuredLogger(stream=stream)
    logger.info(
        "security.test",
        "synthetic credential test",
        payload={"openai_api_key": SECRET, "nested": {"db_password": SECRET}},
        safe_field="visible",
    )
    output = stream.getvalue()
    assert SECRET not in output
    assert "visible" in output
    assert REDACTED in output


def test_subprocess_diagnostics_are_redacted() -> None:
    code = (
        "import sys; "
        f"print('openai_api_key={SECRET}', file=sys.stderr); "
        f"print('db_password={SECRET}', file=sys.stderr); "
        "raise SystemExit(7)"
    )

    async def scenario() -> None:
        with pytest.raises(ProcessExecutionError) as exc_info:
            await run_process(sys.executable, ["-c", code], check=True)
        serialised = json.dumps(exc_info.value.to_dict())
        assert SECRET not in serialised
        assert REDACTED in serialised
        assert exc_info.value.exit_code == 7

    asyncio.run(scenario())
