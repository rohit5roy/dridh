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
