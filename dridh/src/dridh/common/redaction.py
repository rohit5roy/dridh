"""Secret redaction for logs, errors and bridge diagnostics.

The detector is deliberately conservative but broader than the Phase 1
implementation: it recognises exact, prefixed, compound, snake_case,
camelCase and kebab-case credential names. It preserves benign names such as
``token_count`` and ``max_tokens``.
"""

from __future__ import annotations

import re
from typing import Any


class SecretValue:
    """A wrapper for sensitive values that is always redacted.

    Use this to mark values that are known to be secrets at construction time,
    rather than relying on pattern matching. ``repr`` and ``str`` both return
    the redacted marker, and ``redact()`` replaces the instance entirely.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


REDACTED = "[REDACTED]"
REDACTED_PATH = "[REDACTED_PATH]"

# Canonical key names that are always sensitive.
_SENSITIVE_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "access_key",
        "access_key_id",
        "secret_key",
        "private_key",
        "session_key",
        "client_secret",
        "client_key",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "bearer_token",
        "password",
        "passwd",
        "token",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "secret",
    }
)

# A canonical key ending in one of these suffixes is sensitive. The suffix form
# catches prefixes such as ``openai_api_key`` and ``aws_secret_access_key``
# without turning every key containing "token" into a false positive.
_SENSITIVE_SUFFIXES = (
    "api_key",
    "api_secret",
    "access_key",
    "access_key_id",
    "secret_key",
    "private_key",
    "session_key",
    "client_secret",
    "client_key",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "bearer_token",
    "password",
    "passwd",
    "token",
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "secret",
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_KEY_NAME = (
    r"(?:[A-Za-z0-9_.-]*(?:api[_-]?key|api[_-]?secret|"
    r"access[_-]?key(?:[_-]?id)?|secret[_-]?key|private[_-]?key|"
    r"session[_-]?key|client[_-]?secret|client[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"bearer[_-]?token|password|passwd|token|authorization|"
    r"credential(?:s)?|cookie|secret))"
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
        REDACTED,
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), f"Bearer {REDACTED}"),
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}", re.IGNORECASE), REDACTED),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (
        # CLI flags: --api-key hunter2, -password=hunter2
        re.compile(
            r"(?P<flag>--?(?:api[_-]?key|api[_-]?secret|access[_-]?key(?:[_-]?id)?|"
            r"secret[_-]?key|private[_-]?key|session[_-]?key|client[_-]?secret|"
            r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
            r"password|passwd|token|secret|authorization|credential(?:s)?)"
            r"(?:\s+|=))(?P<value>[^\s]+)",
            re.IGNORECASE,
        ),
        rf"\g<flag>{REDACTED}",
    ),
    (
        # key=value, key: value, "key": "value", and prefixed/compound names.
        re.compile(
            rf"(?P<key>(?P<quote>[\"']?){_KEY_NAME}(?P=quote))"
            rf"(?P<sep>\s*[:=]\s*)"
            rf"(?P<value>(?P<vquote>[\"']?)[^\s,;\"'&]+(?P=vquote))",
            re.IGNORECASE,
        ),
        rf"\g<key>\g<sep>\g<vquote>{REDACTED}\g<vquote>",
    ),
    (
        re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^/\s@]+)@"),
        rf"\1{REDACTED}@",
    ),
    (
        # URL query parameters with sensitive names.
        re.compile(
            rf"(?P<prefix>[?&](?:{_KEY_NAME})=)(?P<value>[^&\s]+)",
            re.IGNORECASE,
        ),
        rf"\g<prefix>{REDACTED}",
    ),
    (
        # Environment variable assignments: KEY=value on its own line or in a block.
        re.compile(
            rf"(?m)^(?P<key>(?:[A-Z_]+_)?(?:{_KEY_NAME}))=(?P<value>.+)$",
            re.IGNORECASE,
        ),
        rf"\g<key>={REDACTED}",
    ),
    (
        re.compile(
            r"(?<![\w.-])(?:~?/)?[^\s\"']*"
            r"(?:/\.ssh/|/\.aws/|/\.config/opencode/auth\.json|id_rsa|id_ed25519|"
            # "credentials" only as a file (".../credentials", "credentials.json"): the bare word is ordinary prose and
            # was being rewritten in generated documents ("PyPI [REDACTED_PATH]", found in the big-pickle run).
            r"\.env(?:\.[\w-]+)?|/credentials\b|\bcredentials\.(?:json|ya?ml|toml|ini|cfg|txt|xml)\b|\.npmrc|\.pypirc)"
            r"[^\s\"']*"
        ),
        REDACTED_PATH,
    ),
)


def _canonical_key(key: object) -> str:
    """Normalise snake_case, camelCase and kebab-case keys to snake_case."""

    text = _CAMEL_BOUNDARY.sub("_", str(key))
    text = text.lower()
    return _NON_ALNUM.sub("_", text).strip("_")


def _is_sensitive_key(key: object) -> bool:
    canonical = _canonical_key(key)
    if not canonical:
        return False
    if canonical in _SENSITIVE_EXACT:
        return True
    return any(canonical.endswith(f"_{suffix}") for suffix in _SENSITIVE_SUFFIXES)


def _redact_text(value: str) -> str:
    result = value
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a recursively redacted copy of ``value``.

    Dictionary keys that normally carry credentials are replaced wholesale
    rather than attempting to pattern-match their values. Strings are scanned
    for common token formats, authorization headers, credential key/value
    pairs, CLI flags and secret-bearing paths.
    """

    if _depth > 20:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(key) else redact(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, SecretValue):
        return REDACTED
    if isinstance(value, str):
        return _redact_text(value)
    return value


def redact_error(exc: BaseException) -> str:
    """Redact an exception's type, message and (stringified) details.

    Use this before persisting or reporting an exception. The original
    exception is never returned; only a redacted string is.
    """

    parts = [type(exc).__name__]
    message = str(exc)
    if message:
        parts.append(_redact_text(message))
    for attr in ("details", "args"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(_redact_text(str(redact(value))))
    return ": ".join(parts)[:500]


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of an environment dict with sensitive values redacted.

    Keys matching the sensitive-key rules are replaced wholesale; other values
    are scanned for token patterns.
    """

    result: dict[str, str] = {}
    for key, value in env.items():
        if _is_sensitive_key(key):
            result[key] = REDACTED
        else:
            result[key] = _redact_text(value)
    return result
