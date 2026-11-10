"""Optional Jev System One client with bounded behaviour and a deterministic fallback.

Jev is never on the critical path of a Dridh workflow. The client makes at most two
attempts per call, each with a hard timeout, and never retries indefinitely. Every
failure, including a missing configuration, returns an outcome that carries the
deterministic fallback answers. Nothing here raises into the workflow, except for
invalid questions, which are programming errors and are refused before any network
call.

Live behaviour is NOT measured by the tests in this repository. The tests use local
stub servers only.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact

ENDPOINT_PATH = "/inference/systemone/v1/systemone"
DEFAULT_BASE_URL = "https://opencode.ai"
DEFAULT_MODEL = "jev-1.13-free"
QUESTION_TYPES = frozenset({"noul", "choice", "score"})
MAX_STATE_CHARS = 4000
MAX_QUESTIONS = 8
MAX_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 5.0
RETRY_BACKOFF_SECONDS = 1.0


class JevStatus(str, Enum):
    ANSWERED = "ANSWERED"
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class JevQuestion:
    key: str
    type: str
    instructions: str
    criteria: Any = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            payload["criteria"] = self.criteria
        return payload


@dataclass(frozen=True)
class JevOutcome:
    status: JevStatus
    answers: dict[str, Any]
    used_fallback: bool
    attempts: int
    detail: str = ""
    details: tuple[str, ...] = field(default_factory=tuple)

    @property
    def decided_by_jev(self) -> bool:
        return self.status is JevStatus.ANSWERED


Fallback = Callable[[JevQuestion], Any]


def _validate(questions: list[JevQuestion], state: str) -> None:
    if not isinstance(state, str) or not state.strip() or len(state) > MAX_STATE_CHARS:
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            f"Jev state must be a non-empty string of at most {MAX_STATE_CHARS} characters.",
        )
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"Jev requires 1 to {MAX_QUESTIONS} questions.")
    keys = set()
    for question in questions:
        if question.type not in QUESTION_TYPES:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "Unsupported Jev question type.",
                details=(f"type: {question.type}", f"allowed: {', '.join(sorted(QUESTION_TYPES))}"),
            )
        if not question.key or question.key in keys:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Jev question keys must be unique and non-empty.")
        keys.add(question.key)


def _probabilities_are_valid(value: Any, expected_keys: set[str]) -> bool:
    if not isinstance(value, dict) or set(value) != expected_keys:
        return False
    probabilities = list(value.values())
    if not all(isinstance(item, int | float) and not isinstance(item, bool) and math.isfinite(float(item)) and 0.0 <= float(item) <= 1.0 for item in probabilities):
        return False
    return abs(sum(float(item) for item in probabilities) - 1.0) <= 0.02


def _answer_is_well_formed(question: JevQuestion, answer: Any) -> bool:
    if not isinstance(answer, dict) or answer.get("type") != question.type:
        return False
    if question.type == "noul":
        if set(answer) != {"type", "noul"}:
            return False
        value = answer.get("noul")
        return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0
    if question.type == "choice":
        if set(answer) != {"type", "choice", "confidence", "probabilities"}:
            return False
        choice = answer.get("choice")
        confidence = answer.get("confidence")
        criteria = question.criteria if isinstance(question.criteria, dict) else {}
        expected = set(criteria)
        if not isinstance(choice, str) or choice not in expected:
            return False
        if not isinstance(confidence, int | float) or isinstance(confidence, bool) or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
            return False
        return _probabilities_are_valid(answer.get("probabilities"), expected)
    if question.type == "score":
        if set(answer) != {"type", "score", "confidence", "legend", "probabilities"}:
            return False
        criteria = question.criteria if isinstance(question.criteria, list) else []
        expected = {str(index) for index in range(len(criteria))}
        score = answer.get("score")
        confidence = answer.get("confidence")
        legend = answer.get("legend")
        if not isinstance(score, int | float) or isinstance(score, bool) or not math.isfinite(float(score)):
            return False
        if not 0 <= float(score) <= max(0, len(criteria) - 1):
            return False
        if not isinstance(confidence, int | float) or isinstance(confidence, bool) or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
            return False
        if not isinstance(legend, dict) or set(legend) != expected:
            return False
        if any(legend[str(index)] != label for index, label in enumerate(criteria)):
            return False
        return _probabilities_are_valid(answer.get("probabilities"), expected)
    return False


class JevClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        configured = base_url if base_url is not None else os.environ.get("DRIDH_JEV_URL")
        if not configured and (api_key or os.environ.get("DRIDH_JEV_API_KEY") or os.environ.get("OPENCODE_PROVIDER_CREDENTIAL")):
            configured = DEFAULT_BASE_URL
        self.base_url = (configured or "").rstrip("/") or None
        self.model = model
        self.api_key = api_key if api_key is not None else (os.environ.get("DRIDH_JEV_API_KEY") or os.environ.get("OPENCODE_PROVIDER_CREDENTIAL"))
        raw_timeout = timeout_seconds if timeout_seconds is not None else float(os.environ.get("DRIDH_JEV_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        if not 0 < raw_timeout <= MAX_TIMEOUT_SECONDS:
            raise DridhError(
                ErrorCode.CONFIGURATION_ERROR,
                f"Jev timeout must be greater than zero and at most {MAX_TIMEOUT_SECONDS:g} seconds.",
            )
        self.timeout_seconds = raw_timeout
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    @property
    def enabled(self) -> bool:
        return self.base_url is not None

    def evaluate(self, state: str, questions: list[JevQuestion], fallback: Fallback) -> JevOutcome:
        _validate(questions, state)
        fallback_answers = {question.key: fallback(question) for question in questions}
        if not self.enabled:
            return JevOutcome(JevStatus.DISABLED, fallback_answers, True, 0, "Jev is not configured; the deterministic fallback was used.")

        body = json.dumps(
            {
                "model": self.model,
                "state": state,
                "questions": {question.key: question.to_wire() for question in questions},
            }
        ).encode("utf-8")
        status = JevStatus.UNAVAILABLE
        detail = "Jev did not answer."
        attempts = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            outcome = self._attempt(body)
            if isinstance(outcome, dict):
                parsed = self._parse(outcome, questions)
                if parsed is not None:
                    return JevOutcome(JevStatus.ANSWERED, parsed, False, attempts, "Jev answered.")
                status, detail = JevStatus.MALFORMED, "Jev returned a response that does not match the contract."
                break
            status, detail, retry_after, retryable = outcome
            if not retryable:
                break
            if attempt < MAX_ATTEMPTS:
                self._sleep(min(retry_after if retry_after is not None else RETRY_BACKOFF_SECONDS, MAX_RETRY_AFTER_SECONDS))
        return JevOutcome(status, fallback_answers, True, attempts, detail)

    def _attempt(self, body: bytes) -> dict[str, Any] | tuple[JevStatus, str, float | None, bool]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "dridh-jev-client/1.0"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}{ENDPOINT_PATH}", data=body, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(1_000_000)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after(exc.headers)
            try:
                error_body = exc.read(2000)
            except OSError:
                error_body = b""
            safe_detail = str(redact(error_body.decode("utf-8", errors="replace"))).strip()[:400]
            if exc.code == 429:
                # One bounded retry after the advertised delay; a second rate limit is reported.
                return JevStatus.QUOTA_EXHAUSTED, "Jev rate limit was reached.", retry_after if retry_after is not None else 0.0, True
            if exc.code in (402, 403) and b"quota" in error_body.lower():
                return JevStatus.QUOTA_EXHAUSTED, "Jev quota is exhausted.", None, False
            if exc.code >= 500:
                detail = f"Jev returned HTTP {exc.code}."
                if safe_detail:
                    detail += f" Provider detail: {safe_detail}"
                return JevStatus.PROVIDER_FAILURE, detail, retry_after, True
            detail = f"Jev rejected the request with HTTP {exc.code}."
            if safe_detail:
                detail += f" Provider detail: {safe_detail}"
            return JevStatus.PROVIDER_FAILURE, detail, None, False
        except TimeoutError:
            return JevStatus.TIMEOUT, f"Jev did not answer within {self.timeout_seconds:g} seconds.", None, True
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                return JevStatus.TIMEOUT, f"Jev did not answer within {self.timeout_seconds:g} seconds.", None, True
            return JevStatus.UNAVAILABLE, str(redact(f"Jev is unavailable: {exc.reason}")), None, True
        except OSError as exc:
            return JevStatus.UNAVAILABLE, str(redact(f"Jev is unavailable: {type(exc).__name__}")), None, True
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return JevStatus.MALFORMED, "Jev returned a body that is not JSON.", None, False
        if not isinstance(parsed, dict):
            return JevStatus.MALFORMED, "Jev returned a body that is not an object.", None, False
        return parsed

    @staticmethod
    def _parse(document: dict[str, Any], questions: list[JevQuestion]) -> dict[str, Any] | None:
        if set(document) - {"model", "answers", "usage"}:
            return None
        answers = document.get("answers")
        expected_keys = {question.key for question in questions}
        if not isinstance(answers, dict) or set(answers) != expected_keys:
            return None
        parsed: dict[str, Any] = {}
        for question in questions:
            answer = answers.get(question.key)
            if not _answer_is_well_formed(question, answer):
                return None
            parsed[question.key] = answer
        return parsed


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None
