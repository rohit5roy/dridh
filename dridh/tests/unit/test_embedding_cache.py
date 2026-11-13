"""The FastEmbed model is kept in a persistent cache, not FastEmbed's default temporary directory."""

from __future__ import annotations

import os

from dridh.code_reacher.embeddings import model_cache_dir


def test_model_cache_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert model_cache_dir() == str(tmp_path / "xdg" / "dridh" / "fastembed")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert model_cache_dir() == os.path.join(str(tmp_path / "home"), ".cache", "dridh", "fastembed")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "explicit"))
    assert model_cache_dir() == str(tmp_path / "explicit")


def test_model_files_are_downloaded_once_at_the_pinned_revision_with_a_usable_context_length(tmp_path, monkeypatch):
    """thenlper/gte-small's tokenizer_config.json says model_max_length 1e30 ("unknown"), which FastEmbed refuses; the
    downloaded copy gets 512. A second call finds the files ready and never touches the network (works offline)."""
    import json
    import sys
    import types

    from dridh.code_reacher import embeddings

    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))
    downloads: list[dict] = []

    def snapshot_download(repo_id, *, revision, local_dir, allow_patterns):
        downloads.append({"repo_id": repo_id, "revision": revision, "files": allow_patterns})
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, "tokenizer_config.json"), "w") as handle:
            json.dump({"model_max_length": 1000000000000000019884624838656, "do_lower_case": True}, handle)
        return local_dir

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    target = embeddings.model_files()
    assert embeddings.model_files() == target and len(downloads) == 1
    assert downloads[0]["repo_id"] == "thenlper/gte-small" and downloads[0]["revision"] == embeddings.FASTEMBED_REVISION
    assert "onnx/model.onnx" in downloads[0]["files"]
    with open(os.path.join(target, "tokenizer_config.json")) as handle:
        assert json.load(handle) == {"model_max_length": 512, "do_lower_case": True}
