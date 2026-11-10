"""Code Reacher queries: lookup (full-text + optional vectors), exact symbol, call graph, endpoints, status.

Every result carries the evidence needed to check it (path, lines, qualified name, source snapshot) and says
which method found it. Unresolved relations are returned as unresolved; they are never guessed (plan 1.2).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from dridh.code_reacher.embeddings import EmbeddingProvider, VectorStore, default_provider, describe, split_identifier
from dridh.code_reacher.indexer import require_index
from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo

MAX_DEPTH = 5
MAX_RESULTS = 50
_RRF_K = 60


def _entity_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "entity_id": row["id"], "type": row["entity_type"], "name": row["name"], "qualified_name": row["qualified_name"],
        "signature": row["signature"], "path": row["path"], "language": row["language"],
        "start_line": row["start_line"], "end_line": row["end_line"], "source_sha": row["source_sha"],
    }


_ENTITY_SELECT = (
    "SELECT e.id, e.entity_type, e.name, e.qualified_name, e.signature, e.start_line, e.end_line, e.source_sha, f.path, f.language "
    "FROM code_entity e JOIN code_file f ON f.id=e.file_id "
)


def _fts_expression(query: str) -> str | None:
    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z0-9_]+", query):
        tokens.extend(split_identifier(word) or [word.lower()])
        tokens.append(word.lower())
    unique = list(dict.fromkeys(token for token in tokens if token))
    return " OR ".join(f'"{token}"*' for token in unique) if unique else None


class CodeSearch:
    def __init__(self, workspace: WorkspaceInfo, *, provider: EmbeddingProvider | None = None) -> None:
        self.workspace = workspace
        self._provider = provider

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    @property
    def provider(self) -> EmbeddingProvider:
        if self._provider is None:
            self._provider = default_provider()
        return self._provider

    # ------------------------------------------------------------------ lookup

    def lookup(self, query: str, *, limit: int = 10, semantic: bool = True) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A non-empty query is required.")
        limit = max(1, min(int(limit), MAX_RESULTS))
        expression = _fts_expression(query)
        with self._store() as store:
            connection = store.read()
            run = require_index(connection, self.workspace.workspace_id)
            fts: list[str] = []
            if expression:
                rows = connection.execute(
                    "SELECT x.entity_id FROM code_search_fts x JOIN code_entity e ON e.id=x.entity_id JOIN code_file f ON f.id=e.file_id "
                    "WHERE code_search_fts MATCH ? AND f.index_run_id=? ORDER BY bm25(code_search_fts) LIMIT ?",
                    (expression, run["id"], limit * 3)).fetchall()
                fts = [row[0] for row in rows]
            vector: list[tuple[str, float]] = []
            vector_info: dict[str, Any] = {"used": False}
            if semantic:
                try:
                    provider = self.provider
                    vectors = VectorStore(connection, self.workspace.workspace_id)
                    vector = vectors.search(provider, query, limit * 3)
                    vector_info = {**describe(provider), "used": bool(vector), "sqlite_vec": vectors.vec_available}
                except Exception as exc:  # noqa: BLE001 - semantic search is optional; lexical results still stand
                    vector_info = {"used": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            scores: dict[str, float] = {}
            matched: dict[str, list[str]] = {}
            for rank, entity_id in enumerate(fts):
                scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
                matched.setdefault(entity_id, []).append("fts")
            for rank, (entity_id, _similarity) in enumerate(vector):
                scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
                matched.setdefault(entity_id, []).append("vector")
            ordered = sorted(scores, key=lambda key: scores[key], reverse=True)
            results = []
            for entity_id in ordered:
                row = connection.execute(_ENTITY_SELECT + "WHERE e.id=? AND f.index_run_id=? AND e.entity_type!='module'", (entity_id, run["id"])).fetchone()
                if row is not None:
                    results.append({**_entity_dict(row), "matched_by": matched[entity_id], "score": round(scores[entity_id], 6)})
                if len(results) >= limit:
                    break
        return {"query": query, "index": {"run_id": run["id"], "source_sha": run["source_sha"]}, "vector": vector_info, "results": results}

    # ------------------------------------------------------------------ symbol

    def symbol(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A symbol name is required.")
        with self._store() as store:
            connection = store.read()
            run = require_index(connection, self.workspace.workspace_id)
            rows = connection.execute(
                _ENTITY_SELECT + "WHERE f.index_run_id=? AND e.entity_type!='module' AND (e.qualified_name=? OR e.name=? OR e.qualified_name LIKE ?) "
                "ORDER BY (e.qualified_name=?) DESC, e.qualified_name LIMIT ?",
                (run["id"], name, name, f"%.{name}", name, MAX_RESULTS)).fetchall()
            matches = []
            for row in rows:
                entity = _entity_dict(row)
                entity["parameters"] = [dict(p) for p in connection.execute(
                    "SELECT ordinal, name, type_name, default_value FROM code_entity_parameter WHERE entity_id=? ORDER BY ordinal", (row["id"],))]
                entity["endpoints"] = [dict(p) for p in connection.execute(
                    "SELECT method, path, framework, line FROM code_api_endpoint WHERE entity_id=? ORDER BY line", (row["id"],))]
                matches.append(entity)
        exact = [m for m in matches if m["qualified_name"] == name]
        return {"symbol": name, "unique": len(exact or matches) == 1, "matches": exact or matches}

    # ------------------------------------------------------------- dependencies

    def dependencies(self, symbol: str, *, direction: str = "both", depth: int = 2, relation_types: tuple[str, ...] = ("CALLS",)) -> dict[str, Any]:
        if direction not in ("callers", "callees", "both"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "direction must be callers, callees or both.")
        depth = max(1, min(int(depth), MAX_DEPTH))
        found = self.symbol(symbol)
        if not found["matches"]:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"No symbol matches '{symbol}'.", recovery="Use `dridh code search` to find its qualified name.")
        if not found["unique"]:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"'{symbol}' is ambiguous ({len(found['matches'])} matches); use a qualified name.",
                             details=tuple(m["qualified_name"] for m in found["matches"][:10]))
        root = found["matches"][0]
        marks = ",".join("?" for _ in relation_types)
        with self._store() as store:
            connection = store.read()
            run = require_index(connection, self.workspace.workspace_id)
            result: dict[str, Any] = {"symbol": root["qualified_name"], "depth": depth, "relation_types": list(relation_types)}
            for side, (source, target) in (("callees", ("source_entity_id", "target_entity_id")), ("callers", ("target_entity_id", "source_entity_id"))):
                if direction not in (side, "both"):
                    continue
                rows = connection.execute(
                    f"""WITH RECURSIVE walk(entity_id, depth, path) AS (
                          SELECT ?, 0, ',' || ? || ','
                          UNION
                          SELECT r.{target}, walk.depth + 1, walk.path || r.{target} || ','
                          FROM code_relation r JOIN walk ON r.{source} = walk.entity_id
                          WHERE walk.depth < ? AND r.{target} IS NOT NULL AND r.relation_type IN ({marks})
                            AND instr(walk.path, ',' || r.{target} || ',') = 0
                        )
                        SELECT w.entity_id, MIN(w.depth) AS depth, e.entity_type, e.name, e.qualified_name, e.signature, e.start_line, e.end_line, e.source_sha,
                               f.path, f.language, e.id
                        FROM walk w JOIN code_entity e ON e.id = w.entity_id JOIN code_file f ON f.id = e.file_id
                        WHERE w.depth > 0 AND f.index_run_id = ? GROUP BY w.entity_id ORDER BY depth, e.qualified_name LIMIT ?""",  # noqa: S608 - columns are fixed literals
                    (root["entity_id"], root["entity_id"], depth, *relation_types, run["id"], MAX_RESULTS)).fetchall()
                result[side] = [{**_entity_dict(row), "depth": row["depth"]} for row in rows]
            if direction in ("callees", "both"):
                result["unresolved_callees"] = [dict(row) for row in connection.execute(
                    f"SELECT target_name, line, relation_type FROM code_relation WHERE source_entity_id=? AND target_entity_id IS NULL AND relation_type IN ({marks}) ORDER BY line",  # noqa: S608
                    (root["entity_id"], *relation_types))]
        result["note"] = "Unresolved callees are calls Code Reacher could not match to exactly one indexed definition (external libraries, dynamic dispatch, ambiguous names)."
        return result

    # --------------------------------------------------------------- endpoints

    def endpoints(self, query: str | None = None) -> dict[str, Any]:
        with self._store() as store:
            connection = store.read()
            run = require_index(connection, self.workspace.workspace_id)
            rows = connection.execute(
                "SELECT a.method, a.path, a.framework, a.line, f.path AS file, e.qualified_name FROM code_api_endpoint a "
                "JOIN code_file f ON f.id=a.source_file_id LEFT JOIN code_entity e ON e.id=a.entity_id WHERE f.index_run_id=? "
                "AND (? IS NULL OR a.path LIKE ? OR a.method=?) ORDER BY a.path, a.method LIMIT ?",
                (run["id"], query, f"%{query}%" if query else None, (query or "").upper(), MAX_RESULTS)).fetchall()
        return {"endpoints": [dict(row) for row in rows]}

    # ------------------------------------------------------------------ evidence

    def evidence_summary(self, *, max_entities: int = 200) -> str:
        """A deterministic, citable summary of the current index for design and planning prompts.

        Built from the persisted index only (never from an agent's description), so a design can cite what the
        repository actually contains: the index run and snapshot, files by language, and the public classes and
        functions with their signatures and locations, plus HTTP endpoints.
        """
        with self._store() as store:
            connection = store.read()
            run = connection.execute(
                "SELECT * FROM code_index_run WHERE workspace_id=? AND status='SUCCEEDED' ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (self.workspace.workspace_id,)).fetchone()
            if run is None:
                return ""
            files = connection.execute("SELECT path, language FROM code_file WHERE index_run_id=? ORDER BY path", (run["id"],)).fetchall()
            entities = connection.execute(
                "SELECT e.qualified_name, e.entity_type, e.signature, e.start_line, f.path FROM code_entity e JOIN code_file f ON f.id=e.file_id "
                "WHERE f.index_run_id=? AND e.entity_type IN ('class','function','method') ORDER BY f.path, e.start_line LIMIT ?",
                (run["id"], max_entities + 1)).fetchall()
            endpoints = connection.execute(
                "SELECT a.method, a.path, f.path AS file FROM code_api_endpoint a JOIN code_file f ON f.id=a.source_file_id WHERE f.index_run_id=? ORDER BY a.path LIMIT 50",
                (run["id"],)).fetchall()
        public = [e for e in entities if not any(part.startswith("_") and not part.startswith("__") for part in e["qualified_name"].split("."))]
        lines = [f"Code Reacher index run {run['id']} of snapshot {run['source_sha']} (completed {run['completed_at']}).",
                 f"Files ({len(files)}): " + ", ".join(f"{f['path']} [{f['language']}]" for f in files[:100]) + (" …" if len(files) > 100 else "")]
        lines.append(f"Public classes and functions ({len(public[:max_entities])}{'+' if len(entities) > max_entities else ''}):")
        lines += [f"- {e['entity_type']} {e['qualified_name']}: {e['signature'] or e['qualified_name']} ({e['path']}:{e['start_line']})" for e in public[:max_entities]]
        if endpoints:
            lines.append("HTTP endpoints: " + ", ".join(f"{e['method']} {e['path']} ({e['file']})" for e in endpoints))
        return "\n".join(lines)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        with self._store() as store:
            connection = store.read()
            run = connection.execute(
                "SELECT * FROM code_index_run WHERE workspace_id=? AND status='SUCCEEDED' ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (self.workspace.workspace_id,)).fetchone()
            if run is None:
                return {"indexed": False, "message": "No successful index. Run `dridh index`."}
            def count(sql: str, *params: Any) -> Any:
                return connection.execute(sql, params).fetchone()[0]
            by_language = {r[0]: r[1] for r in connection.execute("SELECT language, COUNT(*) FROM code_file WHERE index_run_id=? GROUP BY language", (run["id"],))}
            by_type = {r[0]: r[1] for r in connection.execute(
                "SELECT e.entity_type, COUNT(*) FROM code_entity e JOIN code_file f ON f.id=e.file_id WHERE f.index_run_id=? GROUP BY e.entity_type", (run["id"],))}
            relations = connection.execute(
                "SELECT COUNT(*), SUM(r.target_entity_id IS NULL) FROM code_relation r JOIN code_entity e ON e.id=r.source_entity_id JOIN code_file f ON f.id=e.file_id WHERE f.index_run_id=?",
                (run["id"],)).fetchone()
            embedded = connection.execute("SELECT model, dimensions, COUNT(*) FROM code_embedding WHERE workspace_id=? GROUP BY model, dimensions", (self.workspace.workspace_id,)).fetchall()
            vec = VectorStore(connection, self.workspace.workspace_id).vec_available
            return {
                "indexed": True,
                "run": {"id": run["id"], "source_sha": run["source_sha"], "completed_at": run["completed_at"], "files_seen": run["files_seen"], "note": run["error"]},
                "files": {"total": sum(by_language.values()), "by_language": by_language},
                "entities": by_type,
                "relations": {"total": relations[0], "unresolved": relations[1] or 0},
                "endpoints": count("SELECT COUNT(*) FROM code_api_endpoint a JOIN code_file f ON f.id=a.source_file_id WHERE f.index_run_id=?", run["id"]),
                "embeddings": [{"model": r[0], "dimensions": r[1], "entities": r[2]} for r in embedded],
                "sqlite_vec": vec,
            }
