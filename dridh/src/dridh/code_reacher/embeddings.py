"""Embedding providers and the vector store (decisions D-12, D-15, D-41).

* ``FastEmbedProvider`` is the real local model (optional extra ``embeddings``); it is imported and
  initialised only when semantic indexing or querying actually needs it.
* ``HashingEmbedder`` is a deterministic, dependency-free fallback (hashed token features). It is NOT a
  language model: it ranks by shared vocabulary. Anything that uses it reports ``degraded: true``.
* ``VectorStore`` keeps the canonical vectors in ``code_embedding`` and mirrors them into a sqlite-vec
  ``vec0`` table when the extension loads. Without the extension an exact cosine search over the stored
  vectors gives the same ranking, just slower.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import uuid
from typing import Any, Protocol

from dridh.state.store import utc_now

FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_BATCH = 16
HASH_MODEL = "dridh-hash-v1"
HASH_DIMENSIONS = 128
_TOKEN = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int
    degraded: bool
    reason: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def split_identifier(value: str) -> list[str]:
    """``getOrderById`` / ``get_order_by_id`` -> ``['get', 'order', 'by', 'id']``."""
    return [token.lower() for token in _TOKEN.findall(value.replace("_", " "))]


class HashingEmbedder:
    model_id = HASH_MODEL
    dimensions = HASH_DIMENSIONS
    degraded = True
    reason = "deterministic hashing fallback (shared-vocabulary ranking, not a language model)"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for value in texts:
            vector = [0.0] * self.dimensions
            for token in split_identifier(value) + re.findall(r"[a-z0-9]+", value.lower()):
                digest = hashlib.sha1(token.encode()).digest()  # noqa: S324 - non-cryptographic feature hashing
                bucket = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[bucket] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


def model_cache_dir() -> str:
    """Where the FastEmbed model is kept: ``FASTEMBED_CACHE_PATH``, else the user's cache directory. FastEmbed's own
    default is the temporary directory, which is emptied on reboot, so the model would be downloaded again and search
    would degrade while offline."""
    explicit = os.environ.get("FASTEMBED_CACHE_PATH")
    if explicit:
        return explicit
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "dridh", "fastembed")


class FastEmbedProvider:
    model_id = FASTEMBED_MODEL
    dimensions = 384
    degraded = False
    reason = "local FastEmbed model"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # lazy: pulls in ONNX Runtime

        try:  # ONNX Runtime logs warnings (e.g. "Failed to persist telemetry device ID" on macOS) that are not Dridh's
            import onnxruntime

            onnxruntime.set_default_logger_severity(3)
        except Exception:  # noqa: BLE001, S110 - only quieter logs; never a reason to fail
            pass
        self._model = TextEmbedding(model_name=FASTEMBED_MODEL, cache_dir=model_cache_dir())

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Small batches: FastEmbed pads every text in a batch to the longest one, so its default of 256 made memory peak
        # at 2-3 GB on real repositories and ran slower (Shopizer, 2,048 entities: 256 -> 2.1 GB, 57 s; 16 -> 0.4 GB, 38 s).
        # Texts of similar length are batched together, so each batch pads less (same vectors, 23% faster on Shopizer).
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        vectors: list[list[float]] = [[] for _ in texts]
        for index, vector in zip(order, self._model.embed([texts[i] for i in order], batch_size=EMBED_BATCH), strict=True):
            vectors[index] = [float(x) for x in vector]
        return vectors


def default_provider() -> EmbeddingProvider:
    """FastEmbed when installed and usable, else the labelled hashing fallback (never silently)."""
    if os.environ.get("DRIDH_EMBEDDINGS", "").lower() != "hash":
        try:
            return FastEmbedProvider()
        except Exception as exc:  # noqa: BLE001 - not installed, no cached model, offline, ...
            fallback = HashingEmbedder()
            fallback.reason = f"FastEmbed unavailable ({type(exc).__name__}); {HashingEmbedder.reason}"
            return fallback
    return HashingEmbedder()


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def load_vec_extension(connection: sqlite3.Connection) -> bool:
    """Load sqlite-vec into this connection. False when the package or loadable extensions are unavailable."""
    try:
        import sqlite_vec

        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        return True
    except (ImportError, AttributeError, sqlite3.Error):
        return False


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


class VectorStore:
    """Entity embeddings for one workspace. Writes need a transaction opened by the caller."""

    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id
        self.vec_available = load_vec_extension(connection)

    def _ensure_vec_table(self, dimensions: int) -> None:
        if not self.vec_available:
            return
        row = self.connection.execute("SELECT sql FROM sqlite_master WHERE name='code_vec'").fetchone()
        if row is not None and f"float[{dimensions}]" not in row[0]:
            self.connection.execute("DROP TABLE code_vec")
            row = None
        if row is None:
            self.connection.execute(f"CREATE VIRTUAL TABLE code_vec USING vec0(embedding float[{dimensions}])")

    def upsert(self, provider: EmbeddingProvider, items: list[tuple[str, str, str]]) -> int:
        """Store embeddings for ``(entity_id, text, source_sha)`` items; returns how many were written."""
        if not items:
            return 0
        if not self.connection.in_transaction:
            raise RuntimeError("VectorStore writes require a transaction.")
        # One active model per workspace: vectors from another model are not comparable, so they are dropped.
        stale = self.connection.execute(
            "SELECT rowid FROM code_embedding WHERE workspace_id=? AND (model!=? OR dimensions!=?)",
            (self.workspace_id, provider.model_id, provider.dimensions),
        ).fetchall()
        if stale:
            self.connection.execute("DELETE FROM code_embedding WHERE workspace_id=? AND (model!=? OR dimensions!=?)",
                                    (self.workspace_id, provider.model_id, provider.dimensions))
        self._ensure_vec_table(provider.dimensions)
        vectors = provider.embed([text for _, text, _ in items])
        now = utc_now()
        for (entity_id, _text, source_sha), vector in zip(items, vectors, strict=True):
            self.connection.execute("DELETE FROM code_embedding WHERE entity_id=? AND model=?", (entity_id, provider.model_id))
            self.connection.execute(
                "INSERT INTO code_embedding(id,workspace_id,entity_id,model,dimensions,vector_json,source_sha,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.workspace_id, entity_id, provider.model_id, provider.dimensions, json.dumps(vector), source_sha, now),
            )
        self.sync_vec_table(provider.dimensions)
        return len(items)

    def sync_vec_table(self, dimensions: int) -> None:
        """Make the vec0 mirror equal to ``code_embedding`` (drops orphans, adds missing)."""
        if not self.vec_available:
            return
        self._ensure_vec_table(dimensions)
        self.connection.execute("DELETE FROM code_vec WHERE rowid NOT IN (SELECT rowid FROM code_embedding)")
        missing = self.connection.execute(
            "SELECT rowid, vector_json FROM code_embedding WHERE workspace_id=? AND dimensions=? AND rowid NOT IN (SELECT rowid FROM code_vec)",
            (self.workspace_id, dimensions),
        ).fetchall()
        for rowid, vector_json in missing:
            self.connection.execute("INSERT INTO code_vec(rowid, embedding) VALUES(?, ?)", (rowid, _pack(json.loads(vector_json))))

    def search(self, provider: EmbeddingProvider, query: str, limit: int, allowed_entities: set[str] | None = None) -> list[tuple[str, float]]:
        """(entity_id, similarity 0..1) nearest to ``query``, best first."""
        stored = self.connection.execute(
            "SELECT 1 FROM code_embedding WHERE workspace_id=? AND model=? AND dimensions=? LIMIT 1",
            (self.workspace_id, provider.model_id, provider.dimensions),
        ).fetchone()
        if stored is None:
            return []
        vector = provider.embed([query])[0]
        if self.vec_available and allowed_entities is None:
            rows = self.connection.execute(
                "SELECT e.entity_id, v.distance FROM code_vec v JOIN code_embedding e ON e.rowid=v.rowid "
                "WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance", (_pack(vector), limit)).fetchall()
            return [(row[0], 1.0 - float(row[1]) ** 2 / 2.0) for row in rows]  # vec0 returns L2 distance; vectors are unit length
        rows = self.connection.execute(
            "SELECT entity_id, vector_json FROM code_embedding WHERE workspace_id=? AND model=? AND dimensions=?",
            (self.workspace_id, provider.model_id, provider.dimensions)).fetchall()
        scored = [(row[0], cosine(vector, json.loads(row[1]))) for row in rows if allowed_entities is None or row[0] in allowed_entities]
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]


def describe(provider: EmbeddingProvider) -> dict[str, Any]:
    return {"model": provider.model_id, "dimensions": provider.dimensions, "degraded": provider.degraded, "reason": provider.reason}
