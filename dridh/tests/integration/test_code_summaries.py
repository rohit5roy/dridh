"""Model-written summaries for code with few doc comments: picked, checked, stored, searchable and embedded."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dridh.code_reacher.embeddings import HashingEmbedder
from dridh.code_reacher.indexer import CodeIndexer, require_index
from dridh.code_reacher.summaries import candidates, check, summarize
from dridh.common.errors import DridhError
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace

SOURCE = '''
def place_hold(member, item, queue):
    if item in queue:
        queue[item].append(member)
    else:
        queue[item] = [member]
    return len(queue[item])


def renew(loan, today):
    """Extend a loan by fourteen days unless another member is waiting for the item, in which case refuse."""
    if loan.waiting:
        raise ValueError("held")
    loan.due = today
    return loan


def tiny(x):
    return x
'''


@pytest.fixture()
def indexed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DRIDH_EMBEDDINGS", "hash")
    project = tmp_path / "project"
    project.mkdir()
    (project / "library.py").write_text(SOURCE)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    info, _ = init_workspace(project, mode="existing")
    CodeIndexer(info, provider=HashingEmbedder()).index()
    return info


class Summarizer:
    def __init__(self, replies=None) -> None:
        self.prompts: list[str] = []
        self.replies = list(replies or [])

    def run_sync(self, *, agent, prompt, model=None, title=None, **_):
        assert agent == "dridh/workers/code-summarizer"
        self.prompts.append(prompt)
        if "INPUT:\n" in prompt:
            self.entities = json.loads(prompt.split("INPUT:\n", 1)[1])["entities"]
        if self.replies:
            return SimpleNamespace(text=self.replies.pop(0), session_id="ses_1")
        if "INPUT:\n" in prompt:  # a repair continues the session, so only the first prompt carries the input
            self.entities = json.loads(prompt.split("INPUT:\n", 1)[1])["entities"]
        entities = self.entities
        return SimpleNamespace(text=json.dumps({"summaries": [
            {"id": e["id"], "summary": f"Reserves a library item for a member, queueing behind earlier holds ({e['name']})."} for e in entities]}),
            session_id="ses_1")


def test_only_code_with_a_body_and_few_doc_comments_is_picked(indexed) -> None:
    with StateStore(indexed.state_db, indexed.workspace_id) as store:
        run = require_index(store.read(), indexed.workspace_id)
        names = [r["qualified_name"] for r in candidates(store.read(), indexed.workspace_id, run["id"], limit=50)]
    assert any(n.endswith("place_hold") for n in names)
    assert not any(n.endswith("renew") for n in names)  # documented
    assert not any(n.endswith("tiny") for n in names)  # two lines: nothing to summarise


def test_summaries_are_stored_searchable_embedded_and_not_asked_for_twice(indexed) -> None:
    worker = Summarizer()
    result = summarize(indexed, runner=worker, model="test/cheap")
    assert result["summarised"] == 1 and result["model"] == "test/cheap" and result["failed_batches"] == []
    assert result["embeddings"].get("written") == 1
    assert "place_hold" in worker.prompts[0] and "queue[item].append(member)" in worker.prompts[0]
    with StateStore(indexed.state_db, indexed.workspace_id) as store:
        connection = store.read()
        row = connection.execute("SELECT s.summary, s.model FROM code_summary s JOIN code_entity e ON e.id=s.entity_id "
                                 "WHERE e.name='place_hold'").fetchone()
        assert row["model"] == "test/cheap" and row["summary"].startswith("Reserves a library item")
        hits = connection.execute("SELECT x.qualified_name FROM code_search_fts x WHERE code_search_fts MATCH 'reserves'").fetchall()
        assert [h[0] for h in hits] and hits[0][0].endswith("place_hold")  # found by words that are not in its name
    from dridh.code_reacher.search import CodeSearch

    top = CodeSearch(indexed).lookup("reserves a library item", limit=3, semantic=False)["results"][0]
    assert top["name"] == "place_hold" and top["summary"].startswith("Reserves a library item")
    again = summarize(indexed, runner=worker)
    assert again["candidates"] == 0 and len(worker.prompts) == 1


def test_a_bad_reply_is_repaired_and_a_failed_batch_keeps_the_rest(indexed) -> None:
    worker = Summarizer(replies=['{"summaries": [{"id": "e9", "summary": "x"}]}'])  # wrong id, then a good reply
    assert summarize(indexed, runner=worker)["summarised"] == 1 and len(worker.prompts) == 2


def test_the_reply_check_names_every_problem() -> None:
    assert check({"summaries": [{"id": "e1", "summary": " Stores an order. "}]}, ["e1"]) == {"e1": "Stores an order."}
    with pytest.raises(DridhError) as raised:
        check({"summaries": [{"id": "e1", "summary": "x" * 201}, {"id": "e3", "summary": "ok"}]}, ["e1", "e2"])
    details = " ".join(raised.value.details)
    assert "e1: one plain sentence" in details and "e3 was not asked for" in details and "missing: e2" in details


def test_the_cli_dry_run_lists_candidates_without_a_model(indexed) -> None:
    from typer.testing import CliRunner

    from dridh.cli.main import app

    out = CliRunner().invoke(app, ["code", "summarize", "--dry-run", "--project", str(indexed.project_root)])
    assert out.exit_code == 0, out.output
    payload = json.loads(out.output)
    assert payload["dry_run"] and payload["candidates"] == 1 and "place_hold" in payload["entities"][0]


def test_an_exhausted_quota_stops_the_run_keeps_what_is_done_and_says_how_to_continue(indexed, monkeypatch) -> None:
    from dridh.code_reacher import summaries
    from dridh.integrations.opencode import AgentErrorCode, AgentRunError

    for index in range(3):  # enough code for three batches of one entity
        (indexed.project_root / f"extra_{index}.py").write_text(f"def extra_{index}(a, b):\n    c = a + b\n    return c * {index}\n")
    CodeIndexer(indexed, provider=HashingEmbedder()).index()
    monkeypatch.setattr(summaries, "BATCH", 1)

    class RunsOut(Summarizer):
        def run_sync(self, **kwargs):
            if len(self.prompts) >= 1:
                self.prompts.append(kwargs["prompt"])
                raise AgentRunError(AgentErrorCode.QUOTA_EXHAUSTED, "The model's quota or free usage limit is exhausted.")
            return super().run_sync(**kwargs)

    worker = RunsOut()
    result = summarize(indexed, runner=worker, embed=False)
    assert result["summarised"] == 1 and len(worker.prompts) == 2  # stopped at the first exhausted batch, not one call per batch
    assert result["failed_batches"] == [] and "--model" in result["stopped"] and result["note"] == result["stopped"]
    again = summarize(indexed, runner=Summarizer(), embed=False)  # another model: continues with the rest
    assert again["summarised"] == 3 and again["stopped"] is None
