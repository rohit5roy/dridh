"""The live-model preflight must skip only on provider unavailability, never hide real failures.

Uses a real local HTTP server (no mocking of the HTTP layer) to stand in for the provider.
"""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / ".github" / "scripts" / "live_model_preflight.py"
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="repository CI scripts are not available")

KEY = "sk-or-test-secret-value"


def _load():
    spec = importlib.util.spec_from_file_location("live_model_preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load() if SCRIPT.is_file() else None


class _Provider:
    """A tiny HTTP server returning a scripted status/body, recording what it received."""

    def __init__(self, status: int, body: dict | str = "") -> None:
        self.status, self.body, self.requests = status, body, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(length) or b"{}")})
                payload = outer.body if isinstance(outer.body, str) else json.dumps(outer.body)
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

            def log_message(self, *args):  # silence
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def provider():
    created = []

    def make(status, body=""):
        item = _Provider(status, body)
        created.append(item)
        return item

    yield make
    for item in created:
        item.close()


def test_answered_request_means_available_and_sends_a_minimal_authenticated_call(provider):
    server = provider(200, {"choices": [{"message": {"content": "OK"}}]})
    ok, reason = preflight.probe(server.url, KEY, "google/gemma-4-31b-it:free", timeout=5)
    assert ok is True and "answered" in reason
    sent = server.requests[0]
    assert sent["path"] == "/chat/completions" and sent["auth"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == "google/gemma-4-31b-it:free" and sent["body"]["max_tokens"] <= 16


def test_daily_free_limit_is_unavailable_with_the_provider_message(provider):
    server = provider(429, {"error": {"message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day"}})
    ok, reason = preflight.probe(server.url, KEY, "m", timeout=5)
    assert ok is False and "429" in reason and "free-models-per-day" in reason
    assert len(server.requests) == 1  # quota/auth errors are not retried


@pytest.mark.parametrize(("status", "fragment"), [(401, "credential rejected"), (402, "credits"), (403, "forbidden"), (404, "not found")])
def test_other_client_errors_are_unavailable_and_not_retried(provider, status, fragment):
    server = provider(status, {"error": {"message": "nope"}})
    ok, reason = preflight.probe(server.url, KEY, "m", timeout=5)
    assert ok is False and fragment in reason and len(server.requests) == 1


def test_server_errors_are_retried_once_then_reported(provider, monkeypatch):
    monkeypatch.setattr(preflight.time, "sleep", lambda _s: None)
    server = provider(503, "upstream down")
    ok, reason = preflight.probe(server.url, KEY, "m", timeout=5, attempts=2)
    assert ok is False and "503" in reason and "provider error" in reason
    assert len(server.requests) == 2


def test_unreachable_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr(preflight.time, "sleep", lambda _s: None)
    ok, reason = preflight.probe("http://127.0.0.1:9", KEY, "m", timeout=2, attempts=1)
    assert ok is False and "unreachable" in reason


def test_reason_is_single_line_bounded_and_never_contains_the_key(provider):
    server = provider(400, {"error": {"message": f"bad\nrequest {KEY} " + "x" * 100}})
    ok, reason = preflight.probe(server.url, KEY, "m", timeout=5)
    assert ok is False and "\n" not in reason and len(reason) <= 240
    assert KEY not in reason  # a provider that echoes the credential must not leak it into logs


def _env(monkeypatch, tmp_path, **values):
    for name in ("LIVE_MODEL", "OPENROUTER_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL", "REQUIRE_LIVE"):
        monkeypatch.delenv(name, raising=False)
    out, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return out, summary


def test_main_skips_cleanly_when_the_provider_is_unavailable(provider, monkeypatch, tmp_path, capsys):
    server = provider(429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}})
    monkeypatch.setattr(preflight, "OPENROUTER_BASE_URL", server.url)
    out, summary = _env(monkeypatch, tmp_path, LIVE_MODEL="openrouter/google/gemma-4-31b-it:free", OPENROUTER_API_KEY=KEY, OPENCODE_PROVIDER_CREDENTIAL="c")
    assert preflight.main() == 0
    assert "available=false" in out.read_text()
    assert "SKIPPED" in summary.read_text()
    printed = capsys.readouterr().out
    assert "::warning" in printed and KEY not in printed + out.read_text() + summary.read_text()


def test_main_proceeds_when_the_provider_answers(provider, monkeypatch, tmp_path):
    server = provider(200, {"ok": True})
    monkeypatch.setattr(preflight, "OPENROUTER_BASE_URL", server.url)
    out, _ = _env(monkeypatch, tmp_path, LIVE_MODEL="openrouter/nvidia/nemotron-3.5-super-vl:free", OPENROUTER_API_KEY=KEY, OPENCODE_PROVIDER_CREDENTIAL="c")
    assert preflight.main() == 0 and "available=true" in out.read_text()
    assert server.requests[0]["body"]["model"] == "nvidia/nemotron-3.5-super-vl:free"


def test_main_skips_without_credentials_and_makes_no_request(provider, monkeypatch, tmp_path):
    server = provider(200, {})
    monkeypatch.setattr(preflight, "OPENROUTER_BASE_URL", server.url)
    out, _ = _env(monkeypatch, tmp_path, LIVE_MODEL="openrouter/a/b:free", OPENROUTER_API_KEY=KEY)  # no Jev credential
    assert preflight.main() == 0
    text = out.read_text()
    assert "available=false" in text and "OPENCODE_PROVIDER_CREDENTIAL" in text and not server.requests


def test_require_live_turns_unavailability_into_a_failure(provider, monkeypatch, tmp_path):
    server = provider(429, {"error": {"message": "quota"}})
    monkeypatch.setattr(preflight, "OPENROUTER_BASE_URL", server.url)
    _env(monkeypatch, tmp_path, LIVE_MODEL="openrouter/a/b:free", OPENROUTER_API_KEY=KEY, OPENCODE_PROVIDER_CREDENTIAL="c", REQUIRE_LIVE="true")
    assert preflight.main() == 1


@pytest.mark.parametrize("bad", ["", "nvidia/nemotron", "openrouter/a b", "openrouter/$(id)", "openrouter/"])
def test_malformed_model_is_a_configuration_error_not_a_skip(monkeypatch, tmp_path, bad):
    _env(monkeypatch, tmp_path, LIVE_MODEL=bad, OPENROUTER_API_KEY=KEY, OPENCODE_PROVIDER_CREDENTIAL="c")
    assert preflight.main() == 1
