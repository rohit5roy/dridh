"""Worktree manager: allocate, diff, cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dridh.workspace.worktree import WorktreeManager


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True)
    return project


def test_allocate_creates_worktree(git_project: Path) -> None:
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-1", "T-001")
    assert info.path.exists()
    assert info.path / "README.md" in list(info.path.rglob("*.md"))
    assert info.branch.startswith("dridh/")


def test_allocate_is_idempotent(git_project: Path) -> None:
    mgr = WorktreeManager(git_project)
    first = mgr.allocate("wt-1", "T-001")
    second = mgr.allocate("wt-1", "T-001")
    assert first.path == second.path
    assert first.base_commit == second.base_commit


def test_capture_diff_and_changed_files(git_project: Path) -> None:
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-1", "T-001")
    (info.path / "new.txt").write_text("hello")
    diff = mgr.capture_diff("wt-1")
    assert "new.txt" in diff
    changed = mgr.capture_changed_files("wt-1")
    assert "new.txt" in changed


def test_cleanup_removes_worktree(git_project: Path) -> None:
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-1", "T-001")
    assert info.path.exists()
    mgr.cleanup("wt-1")
    assert not info.path.exists()


def test_cleanup_nonexistent_is_noop(git_project: Path) -> None:
    mgr = WorktreeManager(git_project)
    mgr.cleanup("wt-nonexistent")  # should not raise


def test_list_worktrees(git_project: Path) -> None:
    mgr = WorktreeManager(git_project)
    mgr.allocate("wt-1", "T-001")
    worktrees = mgr.list_worktrees()
    paths = [w["path"] for w in worktrees]
    assert any("wt-1" in p for p in paths)
