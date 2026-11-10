"""Category B live smoke tests: real OpenCode model execution through OpenRouter.

The Phase 11 NVIDIA Nemotron acceptance test is mandatory in the live-provider
workflow: provider, quota and authentication failures fail with redacted
diagnostics. Only generic smoke/resume checks and Jev's separate quota condition
remain skippable. No skipped test is counted as a successful model invocation.

Required environment for execution:
    DRIDH_REAL_OPENCODE=1
    DRIDH_OPENCODE_BIN=<path to the OpenCode CLI>
    DRIDH_REAL_OPENCODE_CONFIG=<disposable configuration directory>
    DRIDH_REAL_OPENCODE_WORKSPACE=<existing disposable workspace directory>
    OPENROUTER_API_KEY=<OpenRouter API key>
"""

from __future__ import annotations

import json
import os

import pytest

from dridh.common.errors import DridhError
from dridh.common.redaction import redact
from dridh.integrations.opencode import AgentErrorCode, AgentRunError, OpenCodeRunner
from tests.integration.live_replies import ReplyRecorder, failure_message

OPENCODE = os.environ.get("DRIDH_OPENCODE_BIN", "")
CONFIG = os.environ.get("DRIDH_REAL_OPENCODE_CONFIG", "")
WORKSPACE = os.environ.get("DRIDH_REAL_OPENCODE_WORKSPACE", "")


def _missing() -> list[str]:
    missing = [] if os.environ.get("DRIDH_REAL_OPENCODE") == "1" else ["DRIDH_REAL_OPENCODE=1"]
    for name, value in (
        ("DRIDH_OPENCODE_BIN", OPENCODE),
        ("DRIDH_REAL_OPENCODE_CONFIG", CONFIG),
        ("DRIDH_REAL_OPENCODE_WORKSPACE", WORKSPACE),
        ("OPENROUTER_API_KEY", os.environ.get("OPENROUTER_API_KEY", "")),
    ):
        if not value:
            missing.append(name)
    return missing


_MISSING = _missing()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        bool(_MISSING),
        reason="Category B (live model execution, not enabled): missing " + ", ".join(_MISSING),
    ),
]


def _diagnostic_message(exc: AgentRunError) -> str:
    """Include provider stderr while ensuring neither credential alias can leak."""
    message = "\n".join((exc.code.value, exc.message, *exc.details))
    for name in ("OPENROUTER_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL"):
        secret = os.environ.get(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return str(redact(message))[:6000]


def _run_or_skip(runner: OpenCodeRunner, **kwargs):
    """Legacy smoke/resume checks may skip on exhausted provider quota."""
    try:
        return runner.run_sync(**kwargs)
    except AgentRunError as exc:
        if exc.code is AgentErrorCode.QUOTA_EXHAUSTED:
            pytest.skip(f"Category B (live smoke): provider quota exhausted: {_diagnostic_message(exc)}")
        raise


def _run_required_openrouter(runner: OpenCodeRunner, **kwargs):
    """A required OpenRouter model invocation must pass or fail with evidence."""
    try:
        return runner.run_sync(**kwargs)
    except AgentRunError as exc:
        pytest.fail(
            "Required live OpenRouter invocation failed; this is not a pass or skip:\n"
            + _diagnostic_message(exc),
            pytrace=False,
        )


def _opencode_provider_env() -> dict[str, str]:
    """Explicitly pass the OpenRouter key and preserve Jev's separate credential."""
    return {
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
        "OPENCODE_PROVIDER_CREDENTIAL": os.environ.get("OPENCODE_PROVIDER_CREDENTIAL", ""),
        "OPENCODE_CONFIG_DIR": CONFIG,
    }


def test_live_invocation_and_extraction() -> None:
    runner = OpenCodeRunner(executable=OPENCODE, workspace_root=WORKSPACE, config_dir=CONFIG, timeout=90)
    result = _run_or_skip(
        runner,
        agent="dridh/test-worker",
        prompt="Answer with exactly WORKER_OK and nothing else.",
    )
    assert result.text == "WORKER_OK"
    assert result.session_id.startswith("ses_")
    assert result.exit_code == 0


def test_live_session_resume() -> None:
    runner = OpenCodeRunner(executable=OPENCODE, workspace_root=WORKSPACE, config_dir=CONFIG, timeout=90)
    first = _run_or_skip(
        runner,
        agent="dridh/test-worker",
        prompt="Answer with exactly WORKER_OK and nothing else.",
    )
    second = _run_or_skip(
        runner,
        agent="dridh/test-worker",
        prompt="Reply with exactly RESUMED_OK and nothing else.",
        session_id=first.session_id,
    )
    assert second.text == "RESUMED_OK"
    assert second.session_id == first.session_id
    assert second.resumed is True


def test_live_jev_decision_uses_the_same_opencode_provider_credential() -> None:
    """Make a real typed Jev System One request; never substitute the local HTTP stub."""
    if os.environ.get("DRIDH_REAL_JEV") != "1":
        pytest.skip("Category B (live Jev): DRIDH_REAL_JEV is not enabled.")
    if not os.environ.get("OPENCODE_PROVIDER_CREDENTIAL"):
        pytest.skip("Category B (live Jev): OPENCODE_PROVIDER_CREDENTIAL is unavailable.")
    from dridh.jev import JevClient, JevQuestion, JevStatus

    question = JevQuestion(
        "discovery_completeness",
        "score",
        "Score whether this short requirements discovery is complete from 0 (incomplete) to 2 (complete).",
        ["Incomplete", "Partially complete", "Complete"],
    )
    def fallback(item):  # type: ignore[no-untyped-def]
        return {"type": "score", "score": 0, "legend": {"0": "Fallback"}}
    outcome = JevClient(timeout_seconds=20).evaluate(
        "A product owner identified the problem, primary actor, trigger, success outcome, exception path, scope and constraints.",
        [question],
        fallback,
    )
    if outcome.status is JevStatus.QUOTA_EXHAUSTED:
        pytest.skip(f"Category B (live Jev): provider quota exhausted: {outcome.detail}")
    assert outcome.status is JevStatus.ANSWERED, f"Jev request did not return a valid live answer: {outcome.status.value}: {outcome.detail}"
    answer = outcome.answers["discovery_completeness"]
    assert answer["type"] == "score"
    assert 0 <= float(answer["score"]) <= 2
    assert outcome.used_fallback is False


def test_live_ideator_complete_pipeline_uses_nvidia_nemotron_via_openrouter() -> None:
    """Require all three packaged Phase 11 workers to succeed on NVIDIA Nemotron via OpenRouter."""
    from pathlib import Path

    from dridh.ideator.service import TOPICS, IdeatorService
    from dridh.jev import JevClient
    from dridh.state.store import StateStore
    from dridh.workspace.layout import init_workspace

    project = Path(WORKSPACE) / "ideator-live-project"
    project.mkdir(parents=True, exist_ok=True)
    info, _ = init_workspace(project)
    with StateStore(info.state_db, info.workspace_id):
        pass
    runner = OpenCodeRunner(
        executable=OPENCODE,
        workspace_root=project,
        config_dir=CONFIG,
        timeout=180,
        env=_opencode_provider_env(),
    )
    # Jev has its own live-provider test below; disable it here to isolate the
    # three NVIDIA Nemotron workers and avoid spending the Jev quota twice.
    replies = ReplyRecorder(runner)
    service = IdeatorService(info, runner=replies, jev_client=JevClient(base_url=""))
    started = service.start(
        "Allow a customer to submit a valid order and receive a clear outcome.",
        existing_context="Customers currently send orders by email.",
    )
    session_id = started["session_id"]
    for topic in TOPICS:
        service.record_answer(
            session_id,
            topic=topic,
            question=f"Clarify the {topic.replace('_', ' ')} requirement.",
            answer=f"The product owner confirmed the {topic.replace('_', ' ')} requirement for the first release.",
            source="Category B live acceptance",
        )
    try:
        generated = service.generate(
            session_id,
            model=os.environ.get("DRIDH_PLANNER_LIVE_MODEL") or "openrouter/nvidia/nemotron-3.5-super-vl:free",
        )
    except AgentRunError as exc:
        pytest.fail(
            "Required live NVIDIA Nemotron via OpenRouter Ideator pipeline failed; this is not a pass or skip:\n"
            + _diagnostic_message(exc) + "\n--- raw model replies ---\n" + replies.dump(),
            pytrace=False,
        )
    except DridhError as exc:  # e.g. WORKER_FAILED "returned no JSON object"; details hold the reply head
        pytest.fail(
            failure_message(
                "Ideator worker output was rejected by validation",
                {"code": exc.code.value, "message": exc.message, "details": list(exc.details)},
                replies,
            ),
            pytrace=False,
        )

    assert generated["status"] == "IN_REVIEW"
    assert generated["validation"]["ok"] is True
    assert generated["technical_designer"]["unlocked"] is False
    assert service.parity(session_id)["ok"] is True
    canonical = project / "001-USE-CASES.md"
    assert canonical.is_file()
    assert canonical.stat().st_size > 0
    with (info.root / "artifacts" / "ideator" / session_id / "inquiry.json").open(encoding="utf-8") as handle:
        inquiry = json.load(handle)
    assert inquiry["status"] == "IN_REVIEW"
    assert inquiry["generated"]["sha256"] == generated["sha256"]
    assert inquiry["handoff"]["summary"]
