from __future__ import annotations

from dridh.common.redaction import REDACTED, REDACTED_PATH, redact


def test_redacts_sensitive_dictionary_keys() -> None:
    value = {
        "api_key": "abc123",
        "authorization": "Bearer abc123",
        "nested": {"password": "hunter2", "safe": "visible"},
        "token_count": 12,
    }
    result = redact(value)
    assert result["api_key"] == REDACTED
    assert result["authorization"] == REDACTED
    assert result["nested"]["password"] == REDACTED
    assert result["nested"]["safe"] == "visible"
    assert result["token_count"] == 12


def test_redacts_common_token_formats_in_text() -> None:
    text = (
        "Bearer abcdefghijklmnop "
        "sk-proj-abcdefghijklmnopqrstuvwxyz "
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
        "password=hunter2"
    )
    result = redact(text)
    assert "abcdefghijklmnop" not in result
    assert "hunter2" not in result
    assert "Bearer [REDACTED]" in result


def test_redacts_private_key_block() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert redact(text) == REDACTED


def test_redacts_secret_bearing_paths() -> None:
    result = redact("read /Users/someone/.ssh/id_rsa and /repo/.env")
    assert "/Users/someone/.ssh/id_rsa" not in result
    assert "/repo/.env" not in result
    assert REDACTED_PATH in result


def test_preserves_ordinary_text_and_structure() -> None:
    value = {"message": "validation failed for ST-1.1", "count": 3, "items": [1, 2]}
    assert redact(value) == value


def test_the_word_credentials_in_prose_is_kept_but_credential_files_are_redacted():
    """Found in a real run: a generated design document read "PyPI [REDACTED_PATH] and CI provider"."""
    from dridh.common.redaction import REDACTED_PATH, redact

    for prose in ("PyPI credentials and CI provider are open", "no secrets, credentials or personal data", "how credentials rotate"):
        assert redact(prose) == prose
    for path in ("~/.aws/credentials", "/home/u/.config/gcloud/credentials.json", "credentials.yaml", "/srv/app/.env.production"):
        assert REDACTED_PATH in redact(f"see {path} now")
