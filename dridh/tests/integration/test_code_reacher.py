"""Code Reacher v1: Tree-sitter extraction, incremental indexing, honest resolution, search and the lifecycle handoff."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from dridh.code_reacher import embeddings as emb
from dridh.code_reacher.embeddings import HashingEmbedder, VectorStore
from dridh.code_reacher.indexer import CodeIndexer
from dridh.code_reacher.search import CodeSearch
from dridh.common.errors import DridhError
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "code_reacher" / "repo"


@pytest.fixture()
def repo(tmp_path: Path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    info, _ = init_workspace(project, mode="existing")
    with StateStore(info.state_db, info.workspace_id):
        pass
    return project, info


def _index(info, **kwargs):
    return CodeIndexer(info, provider=HashingEmbedder()).index(**kwargs)


def _search(info) -> CodeSearch:
    return CodeSearch(info, provider=HashingEmbedder())


def _rows(info, sql: str, *params):
    connection = sqlite3.connect(info.state_db)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------------- extraction


def test_indexes_all_three_languages_with_entities_parameters_and_endpoints(repo):
    _, info = repo
    result = _index(info)
    assert result["status"] == "SUCCEEDED" and result["unchanged"] is False and result["files_parsed"] == 11
    status = _search(info).status()
    assert status["indexed"] and status["files"]["by_language"] == {"java": 3, "python": 5, "typescript": 3}
    assert status["entities"]["class"] >= 5 and status["entities"]["method"] >= 6 and status["entities"]["interface"] == 1

    endpoints = {(e["method"], e["path"], e["framework"]) for e in _search(info).endpoints()["endpoints"]}
    assert endpoints >= {
        ("GET", "/orders/{order_id}", "fastapi"), ("POST", "/orders", "fastapi"),
        ("GET", "/orders/:id", "nestjs"), ("POST", "/orders", "nestjs"),
        ("GET", "/health", "express"), ("GET", "/orders/{id}", "spring"),
    }
    symbol = _search(info).symbol("app.service.OrderService.create")
    assert symbol["unique"] and symbol["matches"][0]["parameters"][0]["name"] == "payload"
    assert symbol["matches"][0]["parameters"][0]["type_name"] == "dict"
    java = _search(info).symbol("com.shop.OrderController.find")["matches"][0]
    assert java["signature"] == "find(String id): String" and java["endpoints"][0]["path"] == "/orders/{id}"


def test_unresolved_means_unresolved_not_guessed(repo):
    _, info = repo
    _index(info)
    search = _search(info)
    # `self.repo.load` has one definition in the index: resolved. `self.repo.save` matches two classes: left unresolved.
    find = search.dependencies("app.service.OrderService.find", direction="callees", depth=1)
    assert [c["qualified_name"] for c in find["callees"]] == ["app.repo.OrderRepository.load"]
    create = search.dependencies("app.service.OrderService.create", direction="callees", depth=1)
    assert [c["qualified_name"] for c in create["callees"]] == ["app.service.price_with_tax"]
    assert {u["target_name"] for u in create["unresolved_callees"]} == {"self.repo.save"}
    assert "Unresolved callees" in create["note"]


def test_call_graph_walks_depth_both_ways_and_resolves_imports_and_heritage(repo):
    _, info = repo
    _index(info)
    search = _search(info)
    deep = search.dependencies("app.main.read_order", direction="callees", depth=3)
    assert [(c["qualified_name"], c["depth"]) for c in deep["callees"]] == [
        ("app.service.OrderService.find", 1), ("app.repo.OrderRepository.load", 2)]
    callers = search.dependencies("app.service.price_with_tax", direction="callers", depth=2)
    assert {c["qualified_name"] for c in callers["callers"]} == {"app.service.OrderService.create", "app.main.quote", "app.main.create_order"}
    imports = _rows(info, "SELECT r.target_name, t.qualified_name AS resolved FROM code_relation r LEFT JOIN code_entity t ON t.id=r.target_entity_id "
                          "WHERE r.relation_type='IMPORTS' AND r.target_name IN ('app.service.OrderService','./service','./orders/service','fastapi.FastAPI')")
    resolved = {row["target_name"]: row["resolved"] for row in imports}
    assert resolved["app.service.OrderService"] == "app.service.OrderService"
    assert resolved["./service"] == "ts.src.orders.service" and resolved["./orders/service"] == "ts.src.orders.service"
    assert resolved["fastapi.FastAPI"] is None  # a third-party library is not in the index
    implements = _rows(info, "SELECT t.qualified_name FROM code_relation r JOIN code_entity t ON t.id=r.target_entity_id WHERE r.relation_type='IMPLEMENTS'")
    assert [row[0] for row in implements] == ["com.shop.Lookup"]


def test_ambiguous_and_unknown_symbols_are_rejected_clearly(repo):
    _, info = repo
    _index(info)
    search = _search(info)
    with pytest.raises(DridhError, match="ambiguous"):
        search.dependencies("save")
    with pytest.raises(DridhError, match="No symbol"):
        search.dependencies("does.not.exist")
    with pytest.raises(DridhError):
        search.dependencies("app.main.quote", direction="sideways")
    assert search.symbol("save")["unique"] is False


# -------------------------------------------------------------------- search


def test_lookup_combines_full_text_and_vectors_and_names_the_method(repo):
    _, info = repo
    _index(info)
    answer = _search(info).lookup("price with tax")
    top = answer["results"][0]
    assert top["qualified_name"] == "app.service.price_with_tax" and "fts" in top["matched_by"]
    assert answer["vector"]["degraded"] is True and answer["vector"]["model"] == "dridh-hash-v1"
    assert {"path", "start_line", "end_line", "source_sha"} <= set(top)
    camel = _search(info).lookup("normalise")  # identifiers are searchable by their words too
    assert any(r["qualified_name"].endswith("normalise") for r in camel["results"])
    lexical_only = _search(info).lookup("price tax", semantic=False)
    assert all(r["matched_by"] == ["fts"] for r in lexical_only["results"])
    with pytest.raises(DridhError):
        _search(info).lookup("   ")


def test_vector_search_gives_the_same_ranking_with_and_without_sqlite_vec(repo, monkeypatch):
    _, info = repo
    _index(info)
    provider = HashingEmbedder()

    def ranking() -> list[str]:
        with StateStore(info.state_db, info.workspace_id) as store:
            return [entity for entity, _ in VectorStore(store.read(), info.workspace_id).search(provider, "order lookup service", 8)]

    with StateStore(info.state_db, info.workspace_id) as store:
        assert VectorStore(store.read(), info.workspace_id).vec_available is True
    with_vec = ranking()
    monkeypatch.setattr(emb, "load_vec_extension", lambda connection: False)
    assert ranking() == with_vec and with_vec


def test_switching_embedding_model_drops_vectors_of_the_old_one(repo):
    _, info = repo
    _index(info)

    class OtherModel(HashingEmbedder):
        model_id, dimensions = "other-model", 64

    result = CodeIndexer(info, provider=OtherModel()).index()  # unchanged snapshot, but the vectors belong to the old model
    assert result["unchanged"] is True and result["embeddings"]["written"] > 0
    assert {r["model"] for r in _rows(info, "SELECT DISTINCT model FROM code_embedding")} == {"other-model"}


def test_an_interrupted_embedding_is_completed_by_the_next_index(repo):
    """Found on Django: a run killed while downloading the model left the structural index committed and no vectors;
    the next `dridh index` said "snapshot unchanged" and skipped embeddings, so semantic search stayed empty."""
    _, info = repo
    first = _index(info, embed=False)  # the structural index is committed; no vectors (as after a kill)
    assert _rows(info, "SELECT COUNT(*) AS n FROM code_embedding")[0]["n"] == 0
    again = _index(info)
    assert again["unchanged"] is True and again["run_id"] == first["run_id"] and again["embeddings"]["written"] > 0
    assert _index(info)["embeddings"]["written"] == 0  # nothing left to do


# --------------------------------------------------------------- incremental


def test_identical_snapshot_is_a_no_op_and_changes_reparse_only_what_changed(repo):
    project, info = repo
    first = _index(info)
    again = _index(info)
    assert again["unchanged"] is True and again["run_id"] == first["run_id"] and again["files_parsed"] == 0

    (project / "py/app/repo.py").write_text("class OrderRepository:\n    def load(self, order_id: int):\n        return None\n\n    def remove(self, order_id: int):\n        return None\n")
    changed = _index(info)
    assert changed["unchanged"] is False and changed["files_parsed"] == 1 and changed["files_reused"] == 10
    assert _search(info).symbol("app.repo.OrderRepository.remove")["unique"]
    assert not _search(info).symbol("app.repo.OrderRepository.save")["matches"] or all("OrderRepository" not in m["qualified_name"] for m in _search(info).symbol("app.repo.OrderRepository.save")["matches"])
    # relations from untouched files into the changed file were re-resolved, not left dangling
    assert [c["qualified_name"] for c in _search(info).dependencies("app.service.OrderService.find", direction="callees", depth=1)["callees"]] == ["app.repo.OrderRepository.load"]

    (project / "py/app/audit.py").unlink()
    removed = _index(info)
    assert removed["files_removed"] == 1 and removed["files_parsed"] == 0
    assert not _search(info).symbol("app.audit.AuditLog")["matches"]
    # with AuditLog gone, `save` is no longer ambiguous... but OrderRepository.save was removed too, so it stays unresolved
    create = _search(info).dependencies("app.service.OrderService.create", direction="callees", depth=1)
    assert {u["target_name"] for u in create["unresolved_callees"]} == {"self.repo.save"}


def test_search_index_and_embeddings_stay_consistent_with_the_entities(repo):
    project, info = repo
    _index(info)
    (project / "ts/src/orders/service.ts").write_text("export class OrderService {\n  find(id: string): string { return id }\n}\n")
    (project / "py/app/audit.py").unlink()
    _index(info)
    orphans = _rows(info, "SELECT COUNT(*) FROM code_search_fts x WHERE x.entity_id NOT IN (SELECT id FROM code_entity)")[0][0]
    missing = _rows(info, "SELECT COUNT(*) FROM code_entity WHERE id NOT IN (SELECT entity_id FROM code_search_fts)")[0][0]
    stale_vectors = _rows(info, "SELECT COUNT(*) FROM code_embedding WHERE entity_id NOT IN (SELECT id FROM code_entity)")[0][0]
    assert (orphans, missing, stale_vectors) == (0, 0, 0)
    import sqlite_vec

    with closing(sqlite3.connect(info.state_db)) as connection:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        assert connection.execute("SELECT COUNT(*) FROM code_vec").fetchone()[0] == connection.execute("SELECT COUNT(*) FROM code_embedding").fetchone()[0]


def test_snapshot_uses_the_git_head_when_clean_and_a_content_digest_otherwise(repo):
    project, info = repo
    run = lambda *a: subprocess.run(["git", "-C", str(project), *a], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")  # noqa: E702
    (project / ".gitignore").write_text(".opencode/\n")
    run("add", "-A"); run("commit", "-q", "-m", "init")  # noqa: E702
    clean = _index(info)
    assert clean["source_sha"] == run("rev-parse", "HEAD")
    (project / "py/app/repo.py").write_text("class OrderRepository:\n    pass\n")
    dirty = _index(info)
    assert dirty["source_sha"].startswith("tree:") and dirty["source_sha"] != clean["source_sha"]
    assert _index(info)["unchanged"] is True  # same dirty bytes, same snapshot


# -------------------------------------------------------------------- safety


def test_excluded_binary_oversized_and_symlinked_files_are_skipped_with_reasons(repo):
    project, info = repo
    (project / "node_modules/pkg").mkdir(parents=True)
    (project / "node_modules/pkg/index.js").write_text("function hidden() {}\n")
    (project / ".venv/lib").mkdir(parents=True)
    (project / ".venv/lib/x.py").write_text("def hidden(): pass\n")
    (project / "blob.py").write_bytes(b"def a():\n    pass\n\x00\x00binary")
    (project / "big.py").write_text("x = 1\n" * 300_000)
    outside = project.parent / "outside.py"
    outside.write_text("def secret(): pass\n")
    (project / "link.py").symlink_to(outside)
    result = _index(info)
    reasons = {s["path"]: s["reason"] for s in result["skipped"]}
    assert reasons["blob.py"] == "binary content" and reasons["link.py"] == "symlink" and "larger than" in reasons["big.py"]
    assert not _search(info).symbol("hidden")["matches"] and not _search(info).symbol("secret")["matches"]
    assert result["files_parsed"] == 11


def test_minified_and_generated_files_are_skipped(repo):
    """Found on Django: vendored jquery.min.js and friends were parsed as source, one entity per minified variable."""
    project, info = repo
    (project / "static").mkdir()
    (project / "static/vendor.min.js").write_text("function a(){return 1}\n")
    (project / "static/bundle.js").write_text("var x=" + ";".join(f"function f{i}(){{return {i}}}" for i in range(400)) + "\n")
    result = _index(info)
    reasons = {s["path"]: s["reason"] for s in result["skipped"]}
    assert reasons["static/vendor.min.js"] == reasons["static/bundle.js"] == "minified or generated"
    assert result["files_parsed"] == 11


def test_two_definitions_with_one_name_on_one_line_do_not_break_the_index(repo):
    """Found on Django (xregexp): the second same-line definition was ignored, then its parameters broke a foreign key."""
    project, info = repo
    (project / "same_line.js").write_text("var f = function (a) { return a }; if (x) { var g = 1 } var f = function (b, c) { return b }\n")
    result = _index(info)
    assert result["status"] == "SUCCEEDED"
    assert _search(info).symbol("same_line.f")["matches"]


def test_a_file_with_syntax_errors_is_indexed_best_effort_and_reported(repo):
    project, info = repo
    (project / "broken.py").write_text("def ok():\n    return 1\n\ndef bad(:\n    pass\n")
    result = _index(info)
    assert result["status"] == "SUCCEEDED" and result["syntax_errors"] == ["broken.py"]
    assert _search(info).symbol("broken.ok")["unique"]


def test_queries_require_an_index(tmp_path):
    (tmp_path / "empty").mkdir()
    info, _ = init_workspace(tmp_path / "empty", mode="new")
    with StateStore(info.state_db, info.workspace_id):
        pass
    assert _search(info).status()["indexed"] is False
    with pytest.raises(DridhError, match="No successful Code Reacher index"):
        _search(info).lookup("anything")


def test_importing_the_package_does_not_load_native_modules():
    code = ("import sys, dridh.code_reacher, dridh.cli.main; "
            "print(sorted(m for m in ('tree_sitter','sqlite_vec','fastembed','onnxruntime') if m in sys.modules))")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert out.stdout.strip() == "[]", out.stderr


# ----------------------------------------------------- lifecycle integration


def test_the_feature_lifecycle_leaves_codebase_indexing_only_after_a_real_index(repo):
    from dridh.coordinator.lifecycle import FeatureLifecycle

    _, info = repo
    lifecycle = FeatureLifecycle(info)
    lifecycle.create_feature("Order intake", actor="test", mode="existing")
    first = lifecycle.sync("order-intake", actor="test")
    assert first["status"]["feature"]["stage"] == "CODEBASE_INDEXING" and "index" in first["status"]["blockers"][0]
    _index(info)
    second = lifecycle.sync("order-intake", actor="test")
    assert [m["to"] for m in second["moves"]] == ["IDEATION"]
    assert second["status"]["feature"]["stage"] == "IDEATION"


# -------------------------------------------------------------------- bridge


def test_bridge_lets_dridh_agents_query_but_only_the_coordinator_index(repo):
    from tests.contract.test_orchestration_bridge_contract import CLI, COORDINATOR, _call

    project, info = repo
    planner = {"source": "plugin", "agent": "dridh/planner", "session_id": "ses_planner0001"}
    stranger = {"source": "plugin", "agent": "build", "session_id": "ses_other000001"}

    assert _call("code_reacher.index_status", project, caller=planner)["data"]["indexed"] is False
    assert _call("code_reacher.index", project, {"embed": False}, caller=planner)["error"]["code"] == "AUTHORIZATION_DENIED"
    indexed = _call("code_reacher.index", project, {"embed": False}, caller=COORDINATOR)
    assert indexed["ok"] and indexed["data"]["files_parsed"] == 11

    lookup = _call("code_reacher.lookup", project, {"query": "price tax", "limit": 3, "semantic": False}, caller=planner)
    assert lookup["ok"] and lookup["data"]["results"][0]["qualified_name"] == "app.service.price_with_tax"
    symbol = _call("code_reacher.symbol", project, {"name": "app.main.quote"}, caller=planner)
    assert symbol["ok"] and symbol["data"]["unique"]
    deps = _call("code_reacher.dependencies", project, {"symbol": "app.main.quote", "direction": "callees", "depth": 1}, caller=planner)
    assert deps["ok"] and [c["qualified_name"] for c in deps["data"]["callees"]] == ["app.service.price_with_tax"]

    for caller in (CLI, stranger, None):  # humans use the CLI; non-Dridh agents and anonymous callers are refused
        assert _call("code_reacher.lookup", project, {"query": "x"}, caller=caller)["ok"] is False


def test_bridge_validates_code_reacher_inputs(repo):
    from tests.contract.test_orchestration_bridge_contract import COORDINATOR, _call

    project, info = repo
    _index(info)
    for operation, payload in (
        ("code_reacher.lookup", {}), ("code_reacher.lookup", {"query": "x", "limit": 0}), ("code_reacher.lookup", {"query": "x", "limit": True}),
        ("code_reacher.lookup", {"query": "x", "semantic": "yes"}), ("code_reacher.symbol", {"name": 3}),
        ("code_reacher.dependencies", {"symbol": "a", "direction": "up"}), ("code_reacher.dependencies", {"symbol": "a", "depth": 99}),
        ("code_reacher.index", {"embed": "no"}),
    ):
        assert _call(operation, project, payload, caller=COORDINATOR)["ok"] is False, (operation, payload)


# ----------------------------------------------------------------------- CLI


def test_cli_index_and_code_commands(repo):
    import json

    from tests.integration.test_cli import run_module

    project, _ = repo
    env_args = ("--project", str(project))
    status = json.loads(run_module("index", "--status", *env_args).stdout)
    assert status["indexed"] is False
    indexed = run_module("index", "--no-embed", *env_args)
    assert indexed.returncode == 0, indexed.stderr
    assert json.loads(indexed.stdout)["files_parsed"] == 11
    again = json.loads(run_module("index", "--no-embed", *env_args).stdout)
    assert again["unchanged"] is True
    assert json.loads(run_module("index", "--status", *env_args).stdout)["files"]["total"] == 11

    found = json.loads(run_module("code", "search", "price tax", "--no-semantic", *env_args).stdout)
    assert found["results"][0]["qualified_name"] == "app.service.price_with_tax"
    symbol = json.loads(run_module("code", "symbol", "app.main.read_order", *env_args).stdout)
    assert symbol["matches"][0]["endpoints"][0]["path"] == "/orders/{order_id}"
    deps = json.loads(run_module("code", "deps", "app.main.read_order", "--direction", "callees", "--depth", "2", *env_args).stdout)
    assert [c["qualified_name"] for c in deps["callees"]] == ["app.service.OrderService.find", "app.repo.OrderRepository.load"]
    endpoints = json.loads(run_module("code", "endpoints", "health", *env_args).stdout)["endpoints"]
    assert [(e["method"], e["path"]) for e in endpoints] == [("GET", "/health")]

    assert run_module("code", "deps", "x", "--direction", "up", *env_args).returncode == 2
    missing = run_module("code", "symbol", "x", "--project", str(project / "nowhere"))
    assert missing.returncode != 0


# ---------------------------------------------------------------- embeddings


def test_unavailable_fastembed_degrades_visibly_never_silently(monkeypatch):
    monkeypatch.delenv("DRIDH_EMBEDDINGS", raising=False)
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import fastembed` raise ImportError
    provider = emb.default_provider()
    assert provider.model_id == "dridh-hash-v1" and provider.degraded is True
    assert "FastEmbed unavailable" in provider.reason and "not a language model" in provider.reason
    monkeypatch.setenv("DRIDH_EMBEDDINGS", "hash")
    assert emb.default_provider().degraded is True


def test_hashing_embedder_is_deterministic_normalised_and_ranks_shared_vocabulary():
    provider = HashingEmbedder()
    first, second = provider.embed(["getOrderById"]), provider.embed(["getOrderById"])
    assert first == second and abs(sum(x * x for x in first[0]) - 1.0) < 1e-9 and len(first[0]) == provider.dimensions
    query, related, unrelated = provider.embed(["order lookup"] + ["find order by id", "render pdf invoice template"])
    assert emb.cosine(query, related) > emb.cosine(query, unrelated)


def test_fastembed_provider_when_installed():
    """Runs only where the optional `embeddings` extra and its model are available (a developer machine)."""
    import importlib.util

    if importlib.util.find_spec("fastembed") is None:
        pytest.skip("Local-only (developer machine): needs the optional `embeddings` extra (pip install 'dridh[embeddings]')")
    try:
        provider = emb.FastEmbedProvider()
    except Exception as exc:  # noqa: BLE001 - e.g. the model cannot be downloaded offline
        pytest.skip(f"Local-only (developer machine): the FastEmbed model is unavailable ({type(exc).__name__})")
    vectors = provider.embed(["find an order by id", "render a pdf"])
    assert len(vectors[0]) == provider.dimensions == 384 and provider.degraded is False
