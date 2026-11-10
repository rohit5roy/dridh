"""Git-backed worktree isolation integration test.

Proves that workers execute in isolated worktrees, not in the shared project root.
"""

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


def test_worktree_allocation_creates_real_worktree(git_project: Path) -> None:
    """Verify that WorktreeManager creates a real git worktree."""
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-test", "T-001")
    assert info.path.exists()
    assert info.path != git_project
    assert (info.path / "README.md").exists()
    # Verify it's a real git worktree
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=info.path, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "true"
    # Cleanup
    mgr.cleanup("wt-test")


def test_worktree_isolation_two_workers(git_project: Path) -> None:
    """Verify that two worktrees are isolated from each other."""
    mgr = WorktreeManager(git_project)
    info_a = mgr.allocate("wt-a", "T-A")
    info_b = mgr.allocate("wt-b", "T-B")

    # Write a file in worktree A
    (info_a.path / "file_a.txt").write_text("worker A was here")
    # Write a file in worktree B
    (info_b.path / "file_b.txt").write_text("worker B was here")

    # Verify isolation: A doesn't have B's file, B doesn't have A's file
    assert (info_a.path / "file_a.txt").exists()
    assert not (info_a.path / "file_b.txt").exists()
    assert (info_b.path / "file_b.txt").exists()
    assert not (info_b.path / "file_a.txt").exists()

    # Verify original project is unchanged
    assert not (git_project / "file_a.txt").exists()
    assert not (git_project / "file_b.txt").exists()

    # Cleanup
    mgr.cleanup("wt-a", force=True)  # scratch edits: discarding them is deliberate
    mgr.cleanup("wt-b", force=True)


def test_worktree_diff_capture(git_project: Path) -> None:
    """Verify that diffs are captured from the correct worktree."""
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-diff", "T-001")
    (info.path / "new_file.txt").write_text("new content")
    diff = mgr.capture_diff("wt-diff")
    assert "new_file.txt" in diff
    changed = mgr.capture_changed_files("wt-diff")
    assert "new_file.txt" in changed
    mgr.cleanup("wt-diff", force=True)


def test_worktree_cleanup_idempotent(git_project: Path) -> None:
    """Verify that cleanup is idempotent."""
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-cleanup", "T-001")
    assert info.path.exists()
    mgr.cleanup("wt-cleanup")
    assert not info.path.exists()
    # Second cleanup should not raise
    mgr.cleanup("wt-cleanup")


def test_worktree_cleanup_preserves_unrelated(git_project: Path) -> None:
    """Verify that cleanup only affects the specified worktree."""
    mgr = WorktreeManager(git_project)
    info_a = mgr.allocate("wt-a", "T-A")
    info_b = mgr.allocate("wt-b", "T-B")
    mgr.cleanup("wt-a", force=True)  # scratch edits: discarding them is deliberate
    assert not info_a.path.exists()
    assert info_b.path.exists()  # B should still exist
    mgr.cleanup("wt-b")
