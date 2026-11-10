from __future__ import annotations

from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.workspace.paths import ensure_within_root, validate_workspace_root


def test_valid_workspace_is_resolved(workspace: Path) -> None:
    assert validate_workspace_root(workspace) == workspace.resolve()


def test_relative_workspace_is_rejected() -> None:
    with pytest.raises(DridhError) as exc_info:
        validate_workspace_root("relative/path")
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED


def test_missing_workspace_is_workspace_not_found(tmp_path: Path) -> None:
    with pytest.raises(DridhError) as exc_info:
        validate_workspace_root(tmp_path / "missing")
    assert exc_info.value.code is ErrorCode.WORKSPACE_NOT_FOUND


def test_file_workspace_is_rejected(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")
    with pytest.raises(DridhError) as exc_info:
        validate_workspace_root(file_path)
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED


def test_within_root_accepts_child(workspace: Path) -> None:
    child = workspace / "src" / "app.py"
    child.parent.mkdir()
    child.write_text("x")
    assert ensure_within_root(child, workspace) == child.resolve()


def test_within_root_rejects_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    with pytest.raises(DridhError) as exc_info:
        ensure_within_root(outside, workspace)
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
