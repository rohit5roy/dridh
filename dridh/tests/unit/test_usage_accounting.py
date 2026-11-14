"""Token and cost accounting: summed from OpenCode's step_finish events, logged per invocation, summarised by `dridh observe`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dridh.integrations.opencode import AgentErrorCode, AgentRunError, OpenCodeRunner
from dridh.observability.usage import USAGE_LOG, read_usage, record_usage, stage_of, summarise, usage_from_events, usage_from_stdout

SESSION = "ses_fake1234567890"


def _step(input_=0, output=0, reasoning=0, read=0, write=0, cost=0.0):
    return {"type": "step_finish", "sessionID": SESSION,
            "part": {"type": "step-finish", "cost": cost, "tokens": {"input": input_, "output": output, "reasoning": reasoning, "cache": {"read": read, "write": write}}}}


FAKE = '''#!/usr/bin/env python3
import json, os, sys
for line in json.loads(os.environ["FAKE_EVENTS"]):
    print(json.dumps(line))
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
'''


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".opencode" / "dridh").mkdir(parents=True)
    return root


@pytest.fixture()
def fake(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-opencode"
    executable.write_text(FAKE)
    executable.chmod(0o755)
    return executable


def test_every_finished_step_is_summed_and_other_events_are_ignored() -> None:
    events = [{"type": "step_start"}, _step(100, 20, 5, 1000, 50, 0.01), {"type": "text", "part": {"text": "x"}}, _step(10, 2, 0, 1100, 0, 0.002),
              {"type": "step_finish", "part": {"tokens": "broken"}}, {"type": "step_finish"}]
    assert usage_from_events(events) == {"steps": 3, "input": 110, "output": 22, "reasoning": 5, "cache_read": 2100, "cache_write": 50, "cost": 0.012}
    stdout = "not json\n" + "\n".join(json.dumps(event) for event in events)
    assert usage_from_stdout(stdout) == usage_from_events(events)


def test_an_agent_maps_to_its_stage() -> None:
    assert [stage_of(a) for a in ("dridh/workers/ideator-inquirer", "dridh/workers/designer-handoff", "dridh/workers/planner-testing",
                                  "dridh/executor", "dridh/coordinator", "dridh/code-reacher", "build")] == \
        ["ideator", "designer", "planner", "executor", "coordinator", "code-reacher", "other"]


def test_a_successful_run_returns_its_usage_and_logs_one_line_without_any_text(project, fake, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_EVENTS", json.dumps([{"type": "text", "sessionID": SESSION, "part": {"text": "SECRET-ANSWER"}}, _step(7, 3, 1, 40, 9, 0.5)]))
    result = OpenCodeRunner(executable=str(fake), workspace_root=project, timeout=10).run_sync(
        agent="dridh/executor", prompt="SECRET-PROMPT", model="claude-agent/haiku#low", title="Dridh Executor ST-1.2")
    assert result.usage == {"steps": 1, "input": 7, "output": 3, "reasoning": 1, "cache_read": 40, "cache_write": 9, "cost": 0.5}
    assert result.to_dict()["usage"] == result.usage
    raw = (project / USAGE_LOG).read_text()
    assert "SECRET" not in raw
    (entry,) = read_usage(project)
    assert (entry["agent"], entry["stage"], entry["model"], entry["story_id"], entry["outcome"], entry["session_id"]) == \
        ("dridh/executor", "executor", "claude-agent/haiku#low", "ST-1.2", "OK", SESSION)
    assert entry["input"] == 7 and entry["cost"] == 0.5


def test_a_failed_run_is_still_charged(project, fake, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_EVENTS", json.dumps([_step(500, 0, 0, 0, 0, 0.1)]))  # tokens were spent, but there is no assistant text
    with pytest.raises(AgentRunError) as failure:
        OpenCodeRunner(executable=str(fake), workspace_root=project, timeout=10).run_sync(agent="dridh/workers/planner-indexer", prompt="p")
    assert failure.value.code is AgentErrorCode.MALFORMED_OUTPUT
    (entry,) = read_usage(project)
    assert entry["outcome"] == "MALFORMED_OUTPUT" and entry["input"] == 500 and entry["stage"] == "planner" and entry["model"] == "opencode-default"


def test_nothing_is_written_outside_a_dridh_workspace(tmp_path, fake, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_EVENTS", json.dumps([{"type": "text", "sessionID": SESSION, "part": {"text": "ok"}}, _step(1, 1)]))
    OpenCodeRunner(executable=str(fake), workspace_root=tmp_path, timeout=10).run_sync(agent="dridh/executor", prompt="p")
    assert not (tmp_path / ".opencode").exists() and read_usage(tmp_path) == []


def test_the_summary_groups_by_stage_model_and_story(project) -> None:
    for agent, model, title, outcome, tokens in (("dridh/workers/planner-indexer", "m/a", None, "OK", 100), ("dridh/executor", "m/a", "Dridh Executor ST-1.1", "OK", 10),
                                                 ("dridh/executor", "m/b", "Dridh Executor ST-1.1", "MALFORMED_OUTPUT", 5)):
        record_usage(project, agent=agent, model=model, session_id=SESSION, title=title, outcome=outcome, duration_seconds=1.5,
                     usage={"steps": 1, "input": tokens, "output": 1, "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.25}, resumed=False)
    (project / USAGE_LOG).open("a").write("half a line that is not JSON\n")
    summary = summarise(read_usage(project))
    assert (summary["invocations"], summary["failed"], summary["input"], summary["total_tokens"], summary["cost"]) == (3, 1, 115, 118, 0.75)
    assert summary["by_stage"]["executor"]["invocations"] == 2 and summary["by_stage"]["planner"]["input"] == 100
    assert summary["by_model"]["m/b"]["failed"] == 1 and summary["by_story"]["ST-1.1"]["total_tokens"] == 17
    assert summarise([]) ["recorded"] is False
