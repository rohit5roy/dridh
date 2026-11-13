"""The first-run steps the OpenCode TUI shows before the first feature, and the index progress it reads.

ui state reports where setup stands (set up, index for existing codebases, start a feature) and the one next action;
`dridh index --progress` reports parsing and embedding progress as JSON lines on stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from dridh.code_reacher.embeddings import HashingEmbedder
from dridh.code_reacher.indexer import CodeIndexer
from dridh.ui.state import workspace_state
from dridh.workspace.layout import init_workspace


def _project(tmp_path: Path, files: int = 3) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    for i in range(files):
        (project / f"mod{i}.py").write_text(f"def f{i}(x):\n    return x + {i}\n")
    return project


def _steps(state: dict) -> list[tuple[str, str]]:
    return [(step["id"], step["state"]) for step in state["setup"]["steps"]]


def test_a_new_codebase_has_two_steps_and_starts_with_a_feature(tmp_path: Path) -> None:
    info, _ = init_workspace(_project(tmp_path), mode="new")
    state = workspace_state(info)
    assert _steps(state) == [("workspace", "done"), ("feature", "current")]
    assert (state["setup"]["step"], state["setup"]["total"]) == (2, 2)
    assert state["human_action"] == {"kind": "create", "text": "Select Dridh/Coordinator (Shift+Tab) and describe a feature."}


def test_an_existing_codebase_is_indexed_before_the_first_feature(tmp_path: Path) -> None:
    info, _ = init_workspace(_project(tmp_path), mode="existing")
    before = workspace_state(info)
    assert _steps(before) == [("workspace", "done"), ("index", "current"), ("feature", "todo")]
    assert before["setup"]["step"] == 2 and before["human_action"]["kind"] == "index"
    assert before["setup"]["index"] == {"indexed": False, "files": 0}

    CodeIndexer(info, provider=HashingEmbedder()).index()
    after = workspace_state(info)
    assert _steps(after) == [("workspace", "done"), ("index", "done"), ("feature", "current")]
    assert after["setup"]["steps"][1]["label"] == "Index the code (3 files)"
    assert after["human_action"]["kind"] == "create"


def test_indexing_reports_progress_for_files_and_embeddings(tmp_path: Path) -> None:
    info, _ = init_workspace(_project(tmp_path, files=4), mode="existing")
    events: list[tuple[str, int, int]] = []
    CodeIndexer(info, provider=HashingEmbedder()).index(progress=lambda phase, done, total: events.append((phase, done, total)))
    files = [e for e in events if e[0] == "files"]
    assert files[0] == ("files", 0, 4) and files[-1] == ("files", 4, 4)
    assert [done for _, done, _ in files] == sorted(done for _, done, _ in files), "progress only moves forward"
    embeddings = [e for e in events if e[0] == "embeddings"]
    assert embeddings[0][1] == 0 and embeddings[-1][1] == embeddings[-1][2] > 0


def test_dridh_index_progress_is_json_lines_on_stderr_and_the_result_stays_on_stdout(tmp_path: Path) -> None:
    project = _project(tmp_path)
    init_workspace(project, mode="existing")
    env = {**os.environ, "DRIDH_OUTPUT": "json"}
    result = subprocess.run([sys.executable, "-m", "dridh", "index", "--progress", "--no-embed", "--json", "--project", str(project)],  # noqa: S603
                            env=env, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    progress = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{"progress"')]
    assert progress[0] == {"progress": "files", "done": 0, "total": 3}
    assert progress[-1] == {"progress": "files", "done": 3, "total": 3}
    assert json.loads(result.stdout)["files_seen"] == 3


def test_the_embedding_provider_quiets_onnx_runtime_warnings(monkeypatch) -> None:
    """ONNX Runtime printed "Failed to persist telemetry device ID" in the middle of the index output on macOS."""
    import types

    from dridh.code_reacher import embeddings

    calls: list[int] = []
    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(set_default_logger_severity=calls.append))
    loaded: list[dict] = []

    class TextEmbedding:
        add_custom_model = staticmethod(lambda **kwargs: None)

        def __init__(self, **kwargs) -> None:
            loaded.append(kwargs)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding))
    monkeypatch.setitem(sys.modules, "fastembed.common", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "fastembed.common.model_description",
                        types.SimpleNamespace(ModelSource=lambda **kwargs: kwargs, PoolingType=types.SimpleNamespace(MEAN="mean")))
    monkeypatch.setattr(embeddings, "model_files", lambda: "/models/gte-small")
    embeddings.FastEmbedProvider()
    assert calls == [3], "warnings and below are not printed"
    assert loaded == [{"model_name": "thenlper/gte-small", "cache_dir": embeddings.model_cache_dir(), "specific_model_path": "/models/gte-small"}]
