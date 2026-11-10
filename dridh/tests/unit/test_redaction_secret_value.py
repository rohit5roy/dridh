"""SecretValue: structural redaction wrapper."""

from __future__ import annotations

from dridh.common.redaction import REDACTED, SecretValue, redact


def test_secret_value_repr_and_str_are_redacted() -> None:
    secret = SecretValue("super-secret-token")
    assert repr(secret) == REDACTED
    assert str(secret) == REDACTED
    assert secret.get() == "super-secret-token"


def test_redact_replaces_secret_value() -> None:
    secret = SecretValue("super-secret-token")
    assert redact(secret) == REDACTED
    assert redact({"key": secret}) == {"key": REDACTED}
    assert redact([secret, "normal"]) == [REDACTED, "normal"]


def test_secret_value_equality_and_hash() -> None:
    a = SecretValue("same")
    b = SecretValue("same")
    c = SecretValue("different")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)


def test_url_query_params_redacted() -> None:
    from dridh.common.redaction import redact

    text = "https://api.example.com/data?api_key=secret123&token=abc&normal=keep"
    result = redact(text)
    assert "secret123" not in result
    assert "abc" not in result
    assert "normal=keep" in result


def test_env_var_redaction() -> None:
    from dridh.common.redaction import REDACTED, redact_env

    env = {"API_KEY": "secret", "PATH": "/usr/bin", "HOME": "/home/user", "DB_PASSWORD": "dbpass"}
    result = redact_env(env)
    assert result["API_KEY"] == REDACTED
    assert result["DB_PASSWORD"] == REDACTED
    assert result["PATH"] == "/usr/bin"
    assert result["HOME"] == "/home/user"


def test_redact_error() -> None:
    from dridh.common.redaction import redact_error

    exc = ValueError("failed with api_key=secret123")
    result = redact_error(exc)
    assert "secret123" not in result
    assert "ValueError" in result
