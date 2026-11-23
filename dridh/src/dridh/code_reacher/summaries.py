"""Model-written summaries for code with few doc comments (``code_summary``), so search finds it by what it does.

Code search embeds an entity's name, signature, doc comment and the start of its code. Code without doc comments is
found only by its names, so a search in plain words ("reserve a book for a member") can miss it. ``dridh code
summarize`` fills that gap, and only that gap:

- it picks entities of the current index that have a body (three lines or more) and less than ``MIN_DOC`` characters
  of doc comment, most-referenced first, and skips those already summarised for the same source;
- the Code Summarizer worker (no tools) gets their code in batches and returns one sentence each, checked by Python
  (every id answered once, one plain sentence, at most 200 characters) and repaired like any worker reply;
- each summary is stored in ``code_summary`` with the model and the index snapshot, added to full-text search, and
  appended to the entity's embedded text, whose vector is rebuilt.

It is opt-in and bounded (``--limit``): it runs only when asked, and an interrupted run keeps every batch it finished.
Summaries belong to an index snapshot: a changed file is re-indexed with new entities, which can be summarised again.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import StateStore, utc_now
from dridh.workspace.layout import WorkspaceInfo

AGENT = "dridh/workers/code-summarizer"
MIN_DOC = 40  # characters of doc comment below which an entity "has few doc comments"
MIN_LINES = 3
BATCH = 20
DEFAULT_LIMIT = 200
CODE_CHARS = 1500
SUMMARY_CHARS = 200
KINDS = ("function", "method", "class", "interface", "constructor")


def _doc_part(fts_summary: str, head: str) -> str:
    """The doc comment inside a full-text row, which is ``entity_text`` followed by the short doc comment."""
    return fts_summary[len(head):].strip() if fts_summary.startswith(head) else ""


def candidates(connection: Any, workspace_id: str, run_id: str, *, limit: int, min_doc: int = MIN_DOC) -> list[dict[str, Any]]:
    """Entities of the index run worth summarising, most-referenced first."""
    from dridh.code_reacher.indexer import entity_text

    rows = connection.execute(
        "SELECT e.id, e.entity_type, e.name, e.qualified_name, e.signature, e.start_line, e.end_line, e.source_sha, "
        "f.path, f.language, f.content_sha256, x.summary AS fts, "
        "(SELECT COUNT(*) FROM code_relation r WHERE r.target_entity_id=e.id) AS refs "
        "FROM code_entity e JOIN code_file f ON f.id=e.file_id LEFT JOIN code_search_fts x ON x.entity_id=e.id "
        f"WHERE f.index_run_id=? AND e.entity_type IN ({','.join('?' * len(KINDS))}) AND e.end_line - e.start_line + 1 >= ? "
        "AND NOT EXISTS (SELECT 1 FROM code_summary s WHERE s.entity_id=e.id AND s.source_sha=e.source_sha) "
        "ORDER BY refs DESC, f.path, e.start_line", (run_id, *KINDS, MIN_LINES)).fetchall()
    found = []
    for row in rows:
        head = entity_text(row, row["path"])
        if len(_doc_part(row["fts"] or "", head)) < min_doc:
            found.append(dict(row))
            if len(found) >= limit:
                break
    return found


def _code(root: Any, row: dict[str, Any], cache: dict[str, list[str] | None]) -> str | None:
    """The entity's source lines (cut to ``CODE_CHARS``); None when the file changed since it was indexed."""
    import hashlib

    if row["path"] not in cache:
        try:
            data = (root / row["path"]).read_bytes()
            cache[row["path"]] = data.decode("utf-8", "replace").splitlines() if hashlib.sha256(data).hexdigest() == row["content_sha256"] else None
        except OSError:
            cache[row["path"]] = None
    lines = cache[row["path"]]
    if lines is None:
        return None
    return "\n".join(lines[row["start_line"] - 1:row["end_line"]])[:CODE_CHARS]


def prompt(batch: list[dict[str, Any]]) -> str:
    entities = [{"id": f"e{i + 1}", "kind": r["entity_type"], "name": r["qualified_name"], "path": r["path"],
                 "signature": r["signature"] or "", "code": r["code"]} for i, r in enumerate(batch)]
    return ("Summarise each code entity in one plain sentence (at most 200 characters): what it does, in words a person "
            "searching for it would use. Return JSON only: {\"summaries\": [{\"id\": \"e1\", \"summary\": \"...\"}]}, one per "
            "entity, ids copied exactly.\nINPUT:\n" + json.dumps({"entities": entities}, ensure_ascii=False))


def check(reply: dict[str, Any], ids: list[str]) -> dict[str, str]:
    """``id -> summary`` when the reply answers every id once with one plain sentence; else a DridhError to repair."""
    items = reply.get("summaries")
    if not isinstance(items, list):
        raise DridhError(ErrorCode.WORKER_FAILED, "The reply must be {\"summaries\": [{\"id\", \"summary\"}]}.")
    found: dict[str, str] = {}
    problems: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("summary"), str):
            problems.append("every item needs an id and a summary string")
            continue
        text = re.sub(r"\s+", " ", item["summary"]).strip().strip('"').strip()
        if item["id"] not in ids:
            problems.append(f"{item['id']} was not asked for")
        elif item["id"] in found:
            problems.append(f"{item['id']} is answered twice")
        elif not text or len(text) > SUMMARY_CHARS or "\n" in item["summary"].strip() or text.startswith(("#", "-", "*", "`")):
            problems.append(f"{item['id']}: one plain sentence of at most {SUMMARY_CHARS} characters, no Markdown")
        else:
            found[item["id"]] = text
    missing = [i for i in ids if i not in found and not any(p.startswith(i + ":") for p in problems)]
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if problems:
        raise DridhError(ErrorCode.WORKER_FAILED, "The summaries do not match the request.", details=tuple(problems[:20]))
    return found


def _runner(workspace: WorkspaceInfo) -> Any:
    from dridh.integrations.opencode import OpenCodeRunner

    env = {key: os.environ[key] for key in ("OPENCODE_CONFIG_DIR", "OPENCODE_PROVIDER_CREDENTIAL", "OPENCODE_AUTH_FILE", "OPENROUTER_API_KEY")
           if os.environ.get(key)}
    return OpenCodeRunner(executable=os.environ.get("DRIDH_OPENCODE_BIN", "opencode"), workspace_root=workspace.project_root,
                          config_dir=os.environ.get("OPENCODE_CONFIG_DIR"),
                          timeout=float(os.environ.get("DRIDH_SUMMARY_WORKER_TIMEOUT", "300")), env=env)


def summarize(workspace: WorkspaceInfo, *, limit: int = DEFAULT_LIMIT, model: str | None = None, runner: Any = None,
              embed: bool = True, progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    """Summarise up to ``limit`` entities with few doc comments; returns what was done."""
    from dridh.code_reacher.indexer import require_index
    from dridh.observability.models import resolve
    from dridh.orchestration.repair import call_with_repair

    progress = progress or (lambda phase, done, total: None)
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        run = require_index(store.read(), workspace.workspace_id)
        chosen = candidates(store.read(), workspace.workspace_id, run["id"], limit=limit)
    root = _folder(workspace)
    cache: dict[str, list[str] | None] = {}
    rows = []
    for row in chosen:
        code = _code(root, row, cache)
        if code is not None and code.strip():
            rows.append({**row, "code": code})
    label = resolve(workspace.project_root, AGENT, model)[0]
    runner = runner or _runner(workspace)
    written: list[str] = []
    failed: list[str] = []
    progress("summaries", 0, len(rows))
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        ids = [f"e{i + 1}" for i in range(len(batch))]

        def run_worker(call_prompt: str, **kwargs: Any) -> Any:
            return runner.run_sync(agent=AGENT, prompt=call_prompt, model=model, title="Dridh Code Summarizer", **kwargs)

        try:
            outcome = call_with_repair(run_worker, prompt(batch), label="Code Summarizer", validate=lambda reply, ids=ids: check(reply, ids))
        except Exception as exc:  # noqa: BLE001 - one failed batch is reported; the batches before it are kept
            failed.append(f"entities {start + 1}-{start + len(batch)}: {getattr(exc, 'message', None) or type(exc).__name__}"[:300])
            continue
        summaries = outcome.value
        with StateStore(workspace.state_db, workspace.workspace_id) as store, store.transaction() as connection:
            for short, row in zip(ids, batch, strict=True):
                _store(connection, workspace.workspace_id, row, summaries[short], label)
                written.append(row["id"])
        progress("summaries", start + len(batch), len(rows))
    embedded: dict[str, Any] = {"skipped": True}
    if embed and written:
        embedded = reembed(workspace, written)
    return {"candidates": len(chosen), "summarised": len(written), "model": label, "failed_batches": failed,
            "skipped_changed_files": len(chosen) - len(rows), "embeddings": embedded,
            "note": "Run again to summarise more (each run takes at most --limit entities)." if len(chosen) >= limit else None}


def _store(connection: Any, workspace_id: str, row: dict[str, Any], summary: str, model: str) -> None:
    import uuid

    connection.execute(
        "INSERT OR REPLACE INTO code_summary(id,workspace_id,entity_id,summary,model,source_sha,created_at) VALUES(?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), workspace_id, row["id"], summary, model, row["source_sha"], utc_now()))
    fts = (row.get("fts") or "").strip()
    connection.execute("DELETE FROM code_search_fts WHERE entity_id=?", (row["id"],))
    connection.execute("INSERT INTO code_search_fts(entity_id,qualified_name,summary) VALUES(?,?,?)",
                       (row["id"], row["qualified_name"], f"{fts} {summary}".strip()))


def _folder(workspace: WorkspaceInfo) -> Any:
    """The folder the current index was built from (another checkout of the project, after /cd)."""
    from dridh.code_reacher.indexer import index_freshness

    try:
        return Path(index_freshness(workspace)["folder"])
    except Exception:  # noqa: BLE001 - fall back to the project itself
        return workspace.project_root


def summaries_for(connection: Any, entity_ids: list[str]) -> dict[str, str]:
    """``entity_id -> latest summary`` for the given entities."""
    found: dict[str, str] = {}
    for start in range(0, len(entity_ids), 500):
        chunk = entity_ids[start:start + 500]
        for entity, summary in connection.execute(
                f"SELECT entity_id, summary FROM code_summary WHERE entity_id IN ({','.join('?' * len(chunk))}) ORDER BY created_at", chunk):
            found[entity] = summary
    return found


def reembed(workspace: WorkspaceInfo, entity_ids: list[str]) -> dict[str, Any]:
    """Rebuild the vectors of ``entity_ids`` so their embedded text includes the new summaries."""
    from dridh.code_reacher.indexer import CodeIndexer, require_index

    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        run = require_index(store.read(), workspace.workspace_id)
        with store.transaction() as connection:
            for start in range(0, len(entity_ids), 500):
                chunk = entity_ids[start:start + 500]
                connection.execute(f"DELETE FROM code_embedding WHERE entity_id IN ({','.join('?' * len(chunk))})", chunk)
    indexer = CodeIndexer(workspace)
    indexer._root = _folder(workspace)
    return indexer._embed(run["id"], run["source_sha"])
