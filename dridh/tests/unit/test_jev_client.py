"""Optional Jev client: bounded behaviour, deterministic fallback and no leakage. Local stubs only."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.jev import JevClient, JevQuestion, JevStatus

API_KEY = "synthetic-jev-key-7f3a9c"
QUESTIONS = [
    JevQuestion("is_urgent", "noul", "Does this need urgent attention?"),
    JevQuestion("route", "choice", "Which team?", {"billing": "Charges", "shipping": "Delivery"}),
]
GOOD_ANSWERS = {
    "is_urgent": {"type": "noul", "noul": 0.96},
    "route": {"type": "choice", "choice": "billing", "confidence": 1, "probabilities": {"billing": 1, "shipping": 0}},
}


def fallback(question: JevQuestion):
    return {"type": question.type, "fallback": True}


class Stub:
    """A local HTTP server whose behaviour is chosen per test."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.requests: list[dict] = []
        self.delay = 0.0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                stub.requests.append({"body": json.loads(body), "auth": self.headers.get("Authorization"), "user_agent": self.headers.get("User-Agent")})
                if stub.delay:
                    time.sleep(stub.delay)
                mode = stub.mode
                if mode == "ok":
                    self._send(200, {"model": "jev-1.13-free", "answers": GOOD_ANSWERS, "usage": {}})
                elif mode == "rate":
                    self._send(429, {"error": "rate limited"}, headers={"Retry-After": "0"})
                elif mode == "quota":
                    self._send(403, {"detail": "quota exhausted for this account"})
                elif mode == "server":
                    self._send(500, {"error": f"upstream echoed {self.headers.get('Authorization')}"})
                elif mode == "bad-request":
                    self._send(400, {"detail": {"error_type": "api_usage_error"}})
                elif mode == "forbidden":
                    self._send(403, {"error": "credential is not authorised for this endpoint"})
                elif mode == "extra-answer-field":
                    extra = json.loads(json.dumps(GOOD_ANSWERS))
                    extra["route"]["unexpected"] = "value"
                    self._send(200, {"model": "jev-1.13-free", "answers": extra, "usage": {}})
                elif mode == "extra-top-level-field":
                    self._send(200, {"model": "jev-1.13-free", "answers": GOOD_ANSWERS, "usage": {}, "unexpected": True})
                elif mode == "not-json":
                    self.send_response(200)
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"oops!")
                elif mode == "wrong-type":
                    self._send(200, {"answers": {"is_urgent": {"type": "score", "score": 1}, "route": GOOD_ANSWERS["route"]}})
                elif mode == "missing-question":
                    self._send(200, {"answers": {"is_urgent": GOOD_ANSWERS["is_urgent"]}})

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.close()


def client_for(stub: Stub, **kwargs) -> JevClient:
    options = {"base_url": stub.base_url, "api_key": API_KEY, "timeout_seconds": 2.0, "sleep": lambda _: None}
    options.update(kwargs)
    return JevClient(**options)


def test_disabled_by_default_makes_no_network_call(monkeypatch) -> None:
    monkeypatch.delenv("DRIDH_JEV_URL", raising=False)
    client = JevClient(opener=lambda *a, **k: pytest.fail("network must not be used"))
    outcome = client.evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.DISABLED
    assert outcome.used_fallback is True and outcome.attempts == 0
    assert outcome.answers["route"] == {"type": "choice", "fallback": True}


def test_answered_request_sends_the_contract_envelope(stub: Stub) -> None:
    outcome = client_for(stub).evaluate("Payments failed for three days.", QUESTIONS, fallback)
    assert outcome.status is JevStatus.ANSWERED
    assert outcome.decided_by_jev and outcome.used_fallback is False
    assert outcome.answers == GOOD_ANSWERS
    sent = stub.requests[0]["body"]
    assert set(sent) == {"model", "state", "questions"}
    assert sent["questions"]["route"]["criteria"] == {"billing": "Charges", "shipping": "Delivery"}
    assert stub.requests[0]["auth"] == f"Bearer {API_KEY}"
    assert stub.requests[0]["user_agent"] == "dridh-jev-client/1.0"


def test_slow_service_times_out_within_the_bound(stub: Stub) -> None:
    stub.delay = 3.0
    started = time.monotonic()
    outcome = client_for(stub, timeout_seconds=0.5).evaluate("state", QUESTIONS, fallback)
    elapsed = time.monotonic() - started
    assert outcome.status is JevStatus.TIMEOUT
    assert outcome.used_fallback is True
    assert outcome.attempts == 2
    assert elapsed < 2.5, "two bounded attempts only"


def test_quota_exhaustion_is_reported_and_not_retried(stub: Stub) -> None:
    stub.mode = "quota"
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.QUOTA_EXHAUSTED
    assert outcome.attempts == 1 and len(stub.requests) == 1


def test_rate_limit_is_retried_once_then_reported(stub: Stub) -> None:
    stub.mode = "rate"
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.QUOTA_EXHAUSTED
    assert outcome.attempts == 2 and len(stub.requests) == 2


def test_provider_failure_is_bounded_and_falls_back(stub: Stub) -> None:
    stub.mode = "server"
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.PROVIDER_FAILURE
    assert outcome.used_fallback is True
    assert len(stub.requests) == 2


def test_client_error_is_not_retried(stub: Stub) -> None:
    stub.mode = "bad-request"
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.PROVIDER_FAILURE
    assert outcome.attempts == 1 and len(stub.requests) == 1


def test_forbidden_provider_error_has_redacted_diagnostic(stub: Stub) -> None:
    stub.mode = "forbidden"
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.PROVIDER_FAILURE
    assert "HTTP 403" in outcome.detail
    assert "not authorised" in outcome.detail
    assert API_KEY not in outcome.detail
    assert outcome.attempts == 1 and len(stub.requests) == 1


@pytest.mark.parametrize("mode", ["not-json", "wrong-type", "missing-question", "extra-answer-field", "extra-top-level-field"])
def test_malformed_responses_are_rejected_and_fall_back(stub: Stub, mode: str) -> None:
    stub.mode = mode
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.MALFORMED
    assert outcome.used_fallback is True
    assert outcome.answers["is_urgent"] == {"type": "noul", "fallback": True}


def test_unavailable_service_falls_back_without_raising() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    client = JevClient(base_url=f"http://127.0.0.1:{closed_port}", timeout_seconds=1.0, sleep=lambda _: None)
    outcome = client.evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.UNAVAILABLE
    assert outcome.used_fallback is True


def test_api_key_never_appears_in_outcomes(stub: Stub) -> None:
    stub.mode = "server"
    outcome = client_for(stub).evaluate("state", QUESTIONS, fallback)
    rendered = json.dumps({"detail": outcome.detail, "details": list(outcome.details), "answers": outcome.answers})
    assert API_KEY not in rendered


def test_invalid_questions_are_refused_before_any_network_call(stub: Stub) -> None:
    client = client_for(stub)
    with pytest.raises(DridhError) as unsupported:
        client.evaluate("state", [JevQuestion("flag", "boolean", "yes or no?")], fallback)
    assert unsupported.value.code is ErrorCode.VALIDATION_FAILED
    with pytest.raises(DridhError):
        client.evaluate("   ", QUESTIONS, fallback)
    assert stub.requests == []


def test_timeout_configuration_is_bounded() -> None:
    with pytest.raises(DridhError) as unbounded:
        JevClient(base_url="http://127.0.0.1:9", timeout_seconds=600)
    assert unbounded.value.code is ErrorCode.CONFIGURATION_ERROR


def test_opencode_provider_credential_is_used_when_jev_key_is_unset(stub: Stub, monkeypatch) -> None:
    monkeypatch.delenv("DRIDH_JEV_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_PROVIDER_CREDENTIAL", API_KEY)
    client = JevClient(base_url=stub.base_url, timeout_seconds=2.0, sleep=lambda _: None)
    outcome = client.evaluate("state", QUESTIONS, fallback)
    assert outcome.status is JevStatus.ANSWERED
    assert stub.requests[0]["auth"] == f"Bearer {API_KEY}"
