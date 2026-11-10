"""Bounded contract-repair loop shared by the Ideator, Technical Designer and Planner services.

Implementation plan rule 9: a generation/validation correction loop gets at most three repair
attempts; after that the blocker is persisted and a human is asked to intervene.

A worker reply is accepted only after the stage's own deterministic validator accepts it. When the
reply is not a JSON object, or the validator rejects it with a repairable error, the exact problem
is sent back and the worker is asked for a corrected reply. Real-model runs showed that without
this loop a single contract slip (a missing field, a wrong enum value, a null component) fails the
whole stage even though the model can fix it when told what is wrong.

Repair preferably continues the worker's own OpenCode session (the model already sees its previous
answer, so only the problem is sent). When no session is available, or resuming it fails, the full
original prompt is resent together with the feedback.

What is *not* repaired: provider, quota, timeout and other runner failures (``AgentRunError``) and
validator errors that are not content problems (for example a configuration conflict). They
propagate unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.integrations.opencode import AgentErrorCode, AgentRunError
from dridh.orchestration.plan import extract_json_object

MAX_REPAIRS = 3
"""Repair attempts after the initial call (plan rule 9): at most four worker calls in total."""

# Content problems a worker can fix. Anything else raised by a validator is a real error.
REPAIRABLE_CODES = frozenset({ErrorCode.WORKER_FAILED, ErrorCode.VALIDATION_FAILED})

_FEEDBACK_CHARS = 1200
_DETAIL_ITEMS = 8


@dataclass(frozen=True)
class RepairAttempt:
    """One rejected worker reply."""

    attempt: int
    problem: str
    details: tuple[str, ...]
    session_continued: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "problem": self.problem,
            "details": list(self.details),
            "session_continued": self.session_continued,
        }


@dataclass(frozen=True)
class RepairOutcome:
    """An accepted worker reply plus the rejected replies that preceded it."""

    value: Any
    attempts: tuple[RepairAttempt, ...] = ()

    @property
    def repaired(self) -> bool:
        return bool(self.attempts)


def _clip(text: object, limit: int = _FEEDBACK_CHARS) -> str:
    cleaned = " ".join(str(redact(str(text))).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _feedback(problem: str, details: tuple[str, ...], repair_number: int, max_repairs: int) -> str:
    lines = [
        "Your previous reply was rejected by Dridh's deterministic validation.",
        f"Problem: {_clip(problem)}",
    ]
    shown = [_clip(item, 300) for item in details[:_DETAIL_ITEMS]]
    if shown:
        lines.append("Details: " + "; ".join(shown))
    lines.append(
        "Return the complete corrected JSON object only (no commentary, no code fence, not a diff). "
        "Keep everything that was already valid and change only what the problem requires. "
        f"Repair attempt {repair_number} of {max_repairs}."
    )
    return "\n".join(lines)


def _judge(text: str, validate: Callable[[dict[str, Any]], Any] | None) -> tuple[Any, str | None, tuple[str, ...]]:
    """Return (value, problem, details); ``problem`` is None when the reply is acceptable."""

    parsed = extract_json_object(text)
    if not isinstance(parsed, dict):
        return None, "The reply was not a single valid JSON object.", ()
    if validate is None:
        return parsed, None, ()
    try:
        validated = validate(parsed)
    except DridhError as exc:
        if exc.code not in REPAIRABLE_CODES:
            raise
        return None, exc.message, exc.details
    return (parsed if validated is None else validated), None, ()


def call_with_repair(
    run: Callable[..., Any],
    prompt: str,
    *,
    label: str,
    validate: Callable[[dict[str, Any]], Any] | None = None,
    max_repairs: int = MAX_REPAIRS,
) -> RepairOutcome:
    """Run a worker until its reply is valid, repairing at most ``max_repairs`` times.

    ``run(prompt)`` performs the first call and ``run(prompt, session_id=...)`` continues a session;
    both return an object with ``text`` and ``session_id``. ``validate`` receives the parsed JSON
    object, raises ``DridhError`` for a content problem and may return a normalised value (``None``
    means "use the parsed object").
    """

    failed: list[RepairAttempt] = []
    feedback: str | None = None
    session_id: str | None = None
    for number in range(1, max_repairs + 2):
        continued = feedback is not None and bool(session_id)
        if feedback is None:
            call_prompt = prompt
        elif continued:
            call_prompt = feedback
        else:
            call_prompt = f"{prompt}\n\n{feedback}"
        try:
            result = run(call_prompt, session_id=session_id) if continued else run(call_prompt)
        except AgentRunError as exc:
            # Only a failed *resume* is recoverable here: retry once in a fresh session with the full prompt.
            if not continued or exc.code is not AgentErrorCode.SESSION_RESUME_FAILED:
                raise
            continued = False
            result = run(f"{prompt}\n\n{feedback}")
        session_id = getattr(result, "session_id", None) or None
        value, problem, details = _judge(result.text, validate)
        if problem is None:
            return RepairOutcome(value, tuple(failed))
        failed.append(RepairAttempt(number, problem, tuple(str(item) for item in details), continued))
        feedback = _feedback(problem, tuple(str(item) for item in details), number, max_repairs)

    last = failed[-1]
    raise DridhError(
        ErrorCode.WORKER_FAILED,
        f"{label} did not return valid output after {max_repairs} repair attempts: {_clip(last.problem, 300)}",
        details=tuple(f"attempt {item.attempt}: {_clip(item.problem, 300)}" for item in failed),
        recovery=(
            "Human intervention is required: review the recorded attempts, adjust the inputs or clarifications, "
            "then resume. Do not retry automatically."
        ),
        retryable=False,
    )
