"""Incremental Code Reacher indexing (plan section 14).

One *run* describes one snapshot of the repository (the Git HEAD when clean, else a digest of the file
contents). The latest SUCCEEDED run is the current index. A new run adopts the file rows of unchanged
files (same path and content hash), re-parses new and changed files, and drops deleted ones, so re-indexing
costs work only for what changed and an identical snapshot is a no-op.

The structural index is written in one transaction. Embeddings are written afterwards in a second one, so
a slow or failing embedder never leaves the index half-built; it is reported as a warning instead.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dridh.code_reacher import extract_java, extract_python, extract_typescript
from dridh.code_reacher.discovery import SourceFile, change_fingerprint, discover, snapshot_id
from dridh.code_reacher.embeddings import EmbeddingProvider, VectorStore, default_provider, describe, split_identifier, vector_key
from dridh.code_reacher.model import Entity, ParsedFile
from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import StateStore, utc_now
from dridh.workspace.layout import WorkspaceInfo

EMBED_CHUNK = 512
PROGRESS_STEP = 64  # embeddings between progress reports

Progress = Callable[[str, int, int], None]
# The folder the current index was built from (the project, or a feature's integration worktree during delivery) and
# the change fingerprint of that folder when it was indexed.
ROOT_KEY, FINGERPRINT_KEY = "code_index_root", "code_index_fingerprint"


@contextmanager
def _index_lock(workspace: WorkspaceInfo) -> Iterator[None]:
    """Serialises index runs across processes (the OpenCode panel, the Coordinator's bridge, delivery): a second run
    waits, then usually finds nothing left to do."""
    try:
        import fcntl
    except ImportError:  # Windows: SQLite's own locking still keeps each transaction consistent
        yield
        return
    path = workspace.state_db.parent / "code-index.lock"
    with open(path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def index_freshness(workspace: WorkspaceInfo) -> dict[str, Any]:
    """Whether code changed since it was indexed, by anyone (a person, an editor, ``git pull``, another tool). Dridh's own
    delivery indexes what it merges, so its changes are already in the index and never show up here.

    The folder checked is the one the index was built from, so a feature's integration worktree is not swapped for the
    person's checkout in the middle of delivery. ``fingerprint`` lets a caller wait until the files stop changing."""
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        connection = store.read()
        if _current_run(connection, workspace.workspace_id, ".") is None:
            return {"indexed": False, "changed": False}
        meta = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM meta WHERE key IN (?, ?)", (ROOT_KEY, FINGERPRINT_KEY))}
    folder = Path(meta.get(ROOT_KEY) or workspace.project_root)
    if not folder.is_dir():  # a removed integration worktree: the project is what is left
        folder = workspace.project_root
    fingerprint = change_fingerprint(folder)
    return {"indexed": True, "changed": fingerprint != meta.get(FINGERPRINT_KEY), "folder": str(folder), "fingerprint": fingerprint}


BODY_CHARS = 600  # of an entity's code, after its first line, in the embedded text
TEXT_CHARS = 2000  # the whole embedded text; the model reads about 512 tokens (1,500-2,000 characters) of it anyway


def parse(item: SourceFile, python_packages: frozenset[str] = frozenset(), source: bytes | None = None) -> ParsedFile:
    source = item.absolute.read_bytes() if source is None else source
    if item.language == "python":
        return extract_python.extract(source, item.path, python_packages)
    if item.language == "java":
        return extract_java.extract(source, item.path)
    return extract_typescript.extract(source, item.path, item.language)


def entity_text(row: Any, path: str) -> str:
    """The text that represents an entity in full-text search (deterministic): its kind, name, signature and path."""
    words = " ".join(split_identifier(row["name"]))
    return " ".join(part for part in (row["entity_type"], row["qualified_name"], row["signature"] or "", words, path) if part)


def code_start(lines: list[str], start_line: int, end_line: int, language: str) -> str:
    """The start of an entity's code after its first line, comments and docstrings removed, whitespace collapsed."""
    code = "\n".join(lines[start_line:end_line])  # start_line is 1-based, so this skips the first line
    if language == "python":
        code = re.sub(r'("""|\'\'\')[\s\S]*?\1', " ", code)
        code = re.sub(r"#[^\n]*", " ", code)
    else:
        code = re.sub(r"/\*[\s\S]*?\*/", " ", code)
        code = re.sub(r"//[^\n]*", " ", code)
    return re.sub(r"\s+", " ", code).strip()[:BODY_CHARS]


def embedding_text(entity: Entity, path: str, lines: list[str], language: str) -> str:
    """The text that is embedded for semantic search: what full-text search sees, plus the entity's doc comment (its
    purpose) and the start of its code. Measured on CodeSearchNet (Python and Java, 200 queries each), this richer text
    ranked the right method first far more often than the name and signature alone."""
    head = entity_text({"entity_type": entity.entity_type, "name": entity.name, "qualified_name": entity.qualified_name,
                        "signature": entity.signature}, path)
    body = code_start(lines, entity.start_line, entity.end_line, language)
    return " ".join(part for part in (head, entity.doc_text, body) if part)[:TEXT_CHARS]


def embedding_texts(parsed: ParsedFile, source: bytes) -> dict[tuple[str, int], str]:
    """``(qualified_name, start_line) -> embedded text`` for every entity of one parsed file (modules are not embedded)."""
    lines = source.decode("utf-8", "replace").splitlines()
    return {(entity.qualified_name, entity.start_line): embedding_text(entity, parsed.path, lines, parsed.language)
            for entity in parsed.entities if entity.entity_type != "module"}


def _current_run(connection: Any, workspace_id: str, repository: str) -> Any:
    return connection.execute(
        "SELECT * FROM code_index_run WHERE workspace_id=? AND repository_path=? AND status='SUCCEEDED' ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (workspace_id, repository),
    ).fetchone()


class CodeIndexer:
    def __init__(self, workspace: WorkspaceInfo, *, provider: EmbeddingProvider | None = None) -> None:
        self.workspace = workspace
        self._provider = provider
        self._progress: Progress = lambda phase, done, total: None
        self._texts: dict[str, str] = {}  # entity id -> embedded text, for the files parsed by this run
        self._root: Path = workspace.project_root
        self._python_packages: frozenset[str] = frozenset()

    @property
    def provider(self) -> EmbeddingProvider:
        if self._provider is None:
            self._provider = default_provider()
        return self._provider

    def index(self, *, embed: bool = True, root: Path | None = None, progress: Progress | None = None) -> dict[str, Any]:
        """Index ``root`` (default: the project). A clean Git tree is identified by its HEAD commit.

        ``progress(phase, done, total)`` is called as work advances: ``files`` while files are parsed, ``embeddings``
        while vectors are written. It must not raise; it is for display only."""
        self._progress = progress or (lambda phase, done, total: None)
        folder = (root or self.workspace.project_root).resolve()
        with _index_lock(self.workspace):  # one index run at a time per workspace (the panel, the Coordinator, delivery)
            # Taken before reading any file: a file saved while this run reads the tree changes the fingerprint again,
            # so the change is picked up by the next check instead of being lost.
            fingerprint = change_fingerprint(folder)
            result = self._index(embed=embed, root=folder)
            if result["unchanged"] and embed:
                # Embedding is incremental: an unchanged snapshot still gets the vectors an interrupted run never wrote.
                result["embeddings"] = {**self._embed(result["run_id"], result["source_sha"]), "snapshot_unchanged": True}
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store, store.transaction() as connection:
                for key, value in ((ROOT_KEY, str(folder)), (FINGERPRINT_KEY, fingerprint)):
                    connection.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        return result

    def _index(self, *, embed: bool, root: Path | None) -> dict[str, Any]:
        root = (root or self.workspace.project_root).resolve()
        repository = "."
        files, skipped = discover(root)
        snapshot = snapshot_id(root, files)
        python_packages = frozenset(posixpath.dirname(f.path) for f in files if f.language == "python" and posixpath.basename(f.path) == "__init__.py")
        self._root, self._python_packages, self._texts = root, python_packages, {}
        workspace_id = self.workspace.workspace_id
        parsed_paths: list[str] = []
        adopted = removed = 0
        run_id = str(uuid.uuid4())

        with StateStore(self.workspace.state_db, workspace_id) as store:
            with store.transaction() as connection:
                previous = _current_run(connection, workspace_id, repository)
                same = connection.execute(
                    "SELECT id, status FROM code_index_run WHERE workspace_id=? AND repository_path=? AND source_sha=?",
                    (workspace_id, repository, snapshot),
                ).fetchone()
                if same is not None and previous is not None and same["id"] == previous["id"]:
                    return self._unchanged(connection, previous, snapshot, len(files), skipped)
                if same is not None:  # an older snapshot being indexed again: its rows were adopted elsewhere
                    connection.execute("DELETE FROM code_index_run WHERE id=?", (same["id"],))
                connection.execute(
                    "INSERT INTO code_index_run(id,workspace_id,repository_path,source_sha,status,started_at,files_seen) VALUES(?,?,?,?,'RUNNING',?,?)",
                    (run_id, workspace_id, repository, snapshot, utc_now(), len(files)),
                )
                known = {}
                if previous is not None:
                    known = {row["path"]: row for row in connection.execute(
                        "SELECT id, path, content_sha256, language FROM code_file WHERE index_run_id=?", (previous["id"],))}
                current_paths = {item.path for item in files}
                for path, row in known.items():
                    if path not in current_paths:
                        self._drop_file(connection, row["id"])
                        removed += 1
                changed: list[tuple[SourceFile, ParsedFile, dict[tuple[str, int], str]]] = []
                self._progress("files", 0, len(files))
                for done, item in enumerate(files, start=1):
                    self._progress("files", done, len(files))
                    row = known.get(item.path)
                    if row is not None and row["content_sha256"] == item.sha256 and row["language"] == item.language:
                        connection.execute("UPDATE code_file SET index_run_id=? WHERE id=?", (run_id, row["id"]))
                        adopted += 1
                        continue
                    if row is not None:
                        self._drop_file(connection, row["id"])
                    source = item.absolute.read_bytes()
                    result = parse(item, python_packages, source)
                    changed.append((item, result, embedding_texts(result, source)))
                for item, result, texts in changed:
                    self._store_file(connection, run_id, snapshot, item, result, texts)
                    parsed_paths.append(item.path)
                self._resolve_relations(connection)
                unresolved = connection.execute(
                    "SELECT COUNT(*) FROM code_relation r JOIN code_entity e ON e.id=r.source_entity_id JOIN code_file f ON f.id=e.file_id "
                    "WHERE f.index_run_id=? AND r.target_entity_id IS NULL", (run_id,)).fetchone()[0]
                connection.execute(
                    "UPDATE code_index_run SET status='SUCCEEDED', completed_at=?, files_indexed=?, error=? WHERE id=?",
                    (utc_now(), len(parsed_paths), self._skip_note(skipped), run_id),
                )
        result_doc: dict[str, Any] = {
            "run_id": run_id, "source_sha": snapshot, "status": "SUCCEEDED", "unchanged": False,
            "files_seen": len(files), "files_parsed": len(parsed_paths), "files_reused": adopted, "files_removed": removed,
            "skipped": [{"path": s.path, "reason": s.reason} for s in skipped], "unresolved_relations": unresolved,
            "syntax_errors": [p.path for _, p, _ in changed if p.has_syntax_errors],
        }
        result_doc["embeddings"] = self._embed(run_id, snapshot) if embed else {"skipped": True}
        return result_doc

    # ------------------------------------------------------------------ pieces

    @staticmethod
    def _skip_note(skipped: list[Any]) -> str | None:
        return None if not skipped else f"{len(skipped)} file(s) skipped: " + ", ".join(f"{s.path} ({s.reason})" for s in skipped[:5])

    def _unchanged(self, connection: Any, run: Any, snapshot: str, seen: int, skipped: list[Any]) -> dict[str, Any]:
        return {"run_id": run["id"], "source_sha": snapshot, "status": "SUCCEEDED", "unchanged": True, "files_seen": seen,
                "files_parsed": 0, "files_reused": seen, "files_removed": 0,
                "skipped": [{"path": s.path, "reason": s.reason} for s in skipped], "unresolved_relations": None,
                "syntax_errors": [], "embeddings": {"skipped": True, "reason": "snapshot unchanged"}}

    @staticmethod
    def _drop_file(connection: Any, file_id: str) -> None:
        connection.execute("DELETE FROM code_search_fts WHERE entity_id IN (SELECT id FROM code_entity WHERE file_id=?)", (file_id,))
        connection.execute("DELETE FROM code_file WHERE id=?", (file_id,))  # cascades entities, parameters, relations, endpoints, embeddings

    def _store_file(self, connection: Any, run_id: str, snapshot: str, item: SourceFile, parsed: ParsedFile,
                    texts: dict[tuple[str, int], str]) -> None:
        workspace_id = self.workspace.workspace_id
        file_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO code_file(id,workspace_id,index_run_id,path,language,content_sha256,source_sha,size_bytes,indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (file_id, workspace_id, run_id, item.path, item.language, item.sha256, snapshot, item.size, utc_now()),
        )
        ids: list[str] = []
        for entity in parsed.entities:
            entity_id = str(uuid.uuid4())
            inserted = connection.execute(
                "INSERT OR IGNORE INTO code_entity(id,workspace_id,file_id,entity_type,name,qualified_name,signature,start_line,end_line,source_sha) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (entity_id, workspace_id, file_id, entity.entity_type, entity.name, entity.qualified_name, entity.signature,
                 entity.start_line, entity.end_line, snapshot),
            ).rowcount
            if not inserted:  # the same name defined twice on one line (e.g. `a = () => 1, a = () => 2`): keep the first
                ids.append(connection.execute("SELECT id FROM code_entity WHERE file_id=? AND qualified_name=? AND start_line=?",
                                              (file_id, entity.qualified_name, entity.start_line)).fetchone()[0])
                continue
            ids.append(entity_id)
            if (entity.qualified_name, entity.start_line) in texts:
                self._texts[entity_id] = texts[(entity.qualified_name, entity.start_line)]
            for parameter in entity.parameters:
                connection.execute(
                    "INSERT INTO code_entity_parameter(id,entity_id,ordinal,name,type_name,default_value) VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), entity_id, parameter.ordinal, parameter.name, parameter.type_name, parameter.default),
                )
            summary = " ".join(part for part in (entity_text({"entity_type": entity.entity_type, "name": entity.name,
                                                              "qualified_name": entity.qualified_name, "signature": entity.signature}, item.path),
                                                 entity.doc) if part)
            connection.execute("INSERT INTO code_search_fts(entity_id,qualified_name,summary) VALUES(?,?,?)", (entity_id, entity.qualified_name, summary))
        for relation in parsed.relations:
            connection.execute(
                "INSERT INTO code_relation(id,workspace_id,source_entity_id,target_entity_id,relation_type,target_name,evidence_file_id,line,source_sha) VALUES(?,?,?,NULL,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, ids[relation.owner], relation.relation_type, relation.target_name, file_id, relation.line, snapshot),
            )
        for endpoint in parsed.endpoints:
            connection.execute(
                "INSERT OR IGNORE INTO code_api_endpoint(id,workspace_id,entity_id,method,path,framework,source_file_id,line,source_sha) VALUES(?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, ids[endpoint.owner] if endpoint.owner is not None else None, endpoint.method,
                 endpoint.path, endpoint.framework, file_id, endpoint.line, snapshot),
            )

    def _resolve_relations(self, connection: Any) -> None:
        """Link relations to entities only when the match is unambiguous; everything else stays unresolved."""
        workspace_id = self.workspace.workspace_id
        entities = connection.execute(
            "SELECT e.id, e.name, e.qualified_name, e.entity_type, e.file_id, f.path, f.language FROM code_entity e JOIN code_file f ON f.id=e.file_id WHERE e.workspace_id=?",
            (workspace_id,),
        ).fetchall()
        by_name: dict[str, list[Any]] = {}
        by_qualified: dict[str, list[Any]] = {}
        modules: dict[str, str] = {}
        for entity in entities:
            by_name.setdefault(entity["name"], []).append(entity)
            by_qualified.setdefault(entity["qualified_name"], []).append(entity)
            if entity["entity_type"] == "module":
                modules[entity["path"]] = entity["id"]
        owner_file = {entity["id"]: (entity["file_id"], entity["path"], entity["qualified_name"]) for entity in entities}
        # A call or inheritance edge can only target code in the caller's own language family (TypeScript/JavaScript are one).
        family = {"typescript": "js", "tsx": "js", "javascript": "js", "python": "python", "java": "java"}
        entity_family = {entity["id"]: family.get(entity["language"], entity["language"]) for entity in entities}
        callable_types = ("function", "method", "constructor")
        type_types = ("class", "interface", "enum")

        def pick(candidates: list[Any], source_id: str, qualifier: str | None) -> str | None:
            candidates = [c for c in candidates if entity_family[c["id"]] == entity_family[source_id]]
            if not candidates:
                return None
            source_file, _path, source_qualified = owner_file[source_id]
            if qualifier in ("self", "this", "cls"):
                owner_class = source_qualified.rsplit(".", 1)[0]
                scoped = [c for c in candidates if c["qualified_name"].rsplit(".", 1)[0] == owner_class]
                if len(scoped) == 1:
                    return scoped[0]["id"]
            elif qualifier:
                scoped = [c for c in candidates if c["qualified_name"].rsplit(".", 1)[0].rsplit(".", 1)[-1] == qualifier]
                if len(scoped) == 1:
                    return scoped[0]["id"]
            local = [c for c in candidates if c["file_id"] == source_file]
            if len(local) == 1:
                return local[0]["id"]
            if len(candidates) == 1:
                return candidates[0]["id"]
            return None

        updates: list[tuple[str | None, str]] = []
        rows = connection.execute(
            "SELECT r.id, r.source_entity_id, r.relation_type, r.target_name, r.target_entity_id FROM code_relation r WHERE r.workspace_id=?",
            (workspace_id,),
        ).fetchall()
        for row in rows:
            target_name, kind = row["target_name"] or "", row["relation_type"]
            resolved: str | None = None
            if kind == "CALLS":
                cleaned = target_name.replace("()", "")
                parts = cleaned.split(".")
                simple, qualifier = parts[-1], (parts[-2] if len(parts) > 1 else None)
                resolved = pick([c for c in by_name.get(simple, []) if c["entity_type"] in callable_types], row["source_entity_id"], qualifier)
            elif kind in ("EXTENDS", "IMPLEMENTS"):
                resolved = pick([c for c in by_name.get(target_name.split(".")[-1], []) if c["entity_type"] in type_types], row["source_entity_id"], None)
            elif kind == "IMPORTS":
                resolved = self._resolve_import(target_name, owner_file[row["source_entity_id"]][1], by_qualified, modules)
            if resolved != row["target_entity_id"]:
                updates.append((resolved, row["id"]))
        for target, relation_id in updates:
            connection.execute("UPDATE code_relation SET target_entity_id=? WHERE id=?", (target, relation_id))

    @staticmethod
    def _resolve_import(target: str, source_path: str, by_qualified: dict[str, list[Any]], modules: dict[str, str]) -> str | None:
        if target.startswith("."):  # TypeScript/JavaScript relative import
            base = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), target))
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js"):
                if base + suffix in modules:
                    return modules[base + suffix]
            return None
        matches = by_qualified.get(target, [])
        return matches[0]["id"] if len(matches) == 1 else None

    def _embed(self, run_id: str, snapshot: str) -> dict[str, Any]:
        try:
            provider = self.provider
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                connection = store.read()
                have = {row[0] for row in connection.execute(
                    "SELECT entity_id FROM code_embedding WHERE workspace_id=? AND model=? AND dimensions=?",
                    (self.workspace.workspace_id, vector_key(provider), provider.dimensions))}
                rows = connection.execute(
                    "SELECT e.id, e.name, e.qualified_name, e.entity_type, e.signature, e.start_line, f.path, f.language, f.content_sha256 "
                    "FROM code_entity e JOIN code_file f ON f.id=e.file_id WHERE f.index_run_id=? AND e.entity_type!='module'", (run_id,)).fetchall()
                rows = [row for row in rows if row["id"] not in have]
                texts = self._texts_for(rows)
                items = [(row["id"], texts[row["id"]], snapshot) for row in rows]
                written, vec_available = 0, False
                self._progress("embeddings", 0, len(items))
                # In chunks, each committed: memory stays bounded on large repositories (Django: 44k entities), the
                # database is not locked for the whole run, and an interrupted run keeps what it embedded.
                for start in range(0, max(len(items), 1), EMBED_CHUNK):
                    with store.transaction() as connection:
                        vectors = VectorStore(connection, self.workspace.workspace_id)
                        chunk = items[start:start + EMBED_CHUNK]
                        # Embedded in smaller steps so progress moves every few seconds with the real model; still committed
                        # once per chunk.
                        for step in range(0, len(chunk), PROGRESS_STEP):
                            written += vectors.upsert(provider, chunk[step:step + PROGRESS_STEP])
                            self._progress("embeddings", start + min(step + PROGRESS_STEP, len(chunk)), len(items))
                        vectors.sync_vec_table(provider.dimensions)
                        vec_available = vectors.vec_available
            return {**describe(provider), "written": written, "sqlite_vec": vec_available}
        except Exception as exc:  # noqa: BLE001 - the structural index is already committed; report, don't fail
            return {"error": f"{type(exc).__name__}: {exc}"[:300], "written": 0}

    def _texts_for(self, rows: list[Any]) -> dict[str, str]:
        """The embedded text of each entity row. Files parsed by this run already have theirs; files adopted from an
        earlier run (or a model change on an unchanged snapshot) are parsed again from disk. A file that changed since it
        was indexed falls back to the name-and-signature text; the next index run re-parses it anyway."""
        texts = {row["id"]: self._texts[row["id"]] for row in rows if row["id"] in self._texts}
        by_file: dict[str, list[Any]] = {}
        for row in rows:
            if row["id"] not in texts:
                by_file.setdefault(row["path"], []).append(row)
        for path, file_rows in by_file.items():
            reread = _reread(self._root, path, file_rows[0]["language"], file_rows[0]["content_sha256"], self._python_packages)
            for row in file_rows:
                texts[row["id"]] = reread.get((row["qualified_name"], row["start_line"])) or entity_text(row, path)
        return texts


def _reread(root: Path, path: str, language: str, sha256: str, python_packages: frozenset[str]) -> dict[tuple[str, int], str]:
    """The embedded texts of one indexed file, parsed again from disk; empty when the file changed since it was indexed."""
    try:
        source = (root / path).read_bytes()
    except OSError:
        return {}
    if hashlib.sha256(source).hexdigest() != sha256:
        return {}
    item = SourceFile(path=path, absolute=root / path, language=language, sha256=sha256, size=len(source))
    return embedding_texts(parse(item, python_packages, source), source)


def require_index(connection: Any, workspace_id: str) -> Any:
    run = _current_run(connection, workspace_id, ".")
    if run is None:
        raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "No successful Code Reacher index exists for this workspace.",
                         recovery="The Dridh Coordinator builds the index with code_reacher.index; from a terminal, run `dridh index`.")
    return run
