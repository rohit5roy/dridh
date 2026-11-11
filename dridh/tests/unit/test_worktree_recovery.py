"""A worktree with uncommitted changes is never silently destroyed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dridh.common.errors import DridhError
from dridh.workspace.worktree import WorktreeManager


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def manager(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        git(root, *args)
    (root / ".gitignore").write_text(".opencode/\n*.log\n")
    (root / "a.txt").write_text("original\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return WorktreeManager(root)


def dirty(manager, worktree_id="wt-1"):
    info = manager.allocate(worktree_id, "S-1", "branch-1")
    (info.path / "a.txt").write_text("edited but not committed\n")
    (info.path / "new.py").write_text("print('untracked')\n")
    (info.path / "debug.log").write_text("ignored noise\n")
    return info


def test_a_dirty_worktree_is_not_removed_without_force(manager):
    info = dirty(manager)
    with pytest.raises(DridhError) as caught:
        manager.cleanup("wt-1")
    assert "uncommitted" in caught.value.message and set(caught.value.details) == {"a.txt", "new.py"}  # ignored files are not work
    assert info.path.is_dir() and (info.path / "new.py").is_file()
    assert manager.recovery_snapshots() == []  # refusing saves nothing


def test_forced_removal_first_saves_everything_including_untracked_files(manager):
    info = dirty(manager)
    result = manager.cleanup("wt-1", force=True, reason="test")
    assert result["removed"] is True and not info.path.exists()
    snapshot = result["snapshot"]
    assert snapshot["file_count"] == 2 and snapshot["ref"].startswith("refs/dridh/recovery/wt-1/")
    repo = manager.project_root
    assert git(repo, "show", f"{snapshot['ref']}:a.txt") == "edited but not committed"
    assert git(repo, "show", f"{snapshot['ref']}:new.py") == "print('untracked')"
    assert subprocess.run(["git", "cat-file", "-e", f"{snapshot['ref']}:debug.log"], cwd=repo, capture_output=True).returncode != 0
    assert git(repo, "log", "-1", "--format=%P", snapshot["ref"]) == git(repo, "rev-parse", "branch-1")  # a child of the worktree's HEAD


def test_the_snapshot_changes_neither_the_branch_nor_the_working_tree(manager):
    info = dirty(manager)
    head_before, status_before = git(info.path, "rev-parse", "HEAD"), git(info.path, "status", "--porcelain")
    manager.snapshot("wt-1", "inspection")
    assert git(info.path, "rev-parse", "HEAD") == head_before and git(info.path, "status", "--porcelain") == status_before
    assert (info.path / "a.txt").read_text() == "edited but not committed\n"


def test_a_clean_worktree_is_removed_without_a_snapshot(manager):
    manager.allocate("wt-2", "S-2", "branch-2")
    result = manager.cleanup("wt-2")
    assert result == {"removed": True, "snapshot": None} and manager.recovery_snapshots() == []


def test_a_snapshot_can_be_restored_into_a_new_directory(manager, tmp_path):
    dirty(manager)
    ref = manager.cleanup("wt-1", force=True)["snapshot"]["ref"]
    restored = manager.restore_snapshot(ref, tmp_path / "restored")
    assert (restored / "a.txt").read_text() == "edited but not committed\n" and (restored / "new.py").is_file()
    assert [s["ref"] for s in manager.recovery_snapshots()] == [ref]  # the snapshot stays
    with pytest.raises(DridhError):
        manager.restore_snapshot(ref, tmp_path / "restored")  # never overwrites
    with pytest.raises(DridhError, match="recovery refs"):
        manager.restore_snapshot("refs/heads/branch-1", tmp_path / "other")


def test_a_missing_worktree_is_a_no_op(manager):
    assert manager.cleanup("never-existed") == {"removed": False, "snapshot": None}


def test_the_cli_lists_and_restores_recovery_refs(manager, tmp_path):
    import json

    from tests.e2e.planner_test_helpers import seed_approved_baseline
    from tests.integration.test_cli import run_module

    info, _ = seed_approved_baseline(tmp_path / "project")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        git(info.project_root, *args)
    (info.project_root / ".gitignore").write_text(".opencode/\n")
    git(info.project_root, "add", ".gitignore")
    git(info.project_root, "commit", "-q", "-m", "base")
    local = WorktreeManager(info.project_root)
    work = local.allocate("wt-cli", "S-1", "branch-cli")
    (work.path / "kept.txt").write_text("keep me\n")
    ref = local.cleanup("wt-cli", force=True)["snapshot"]["ref"]
    args = ("--project", str(info.project_root))
    listed = json.loads(run_module("recovery", "list", *args, cwd=info.project_root).stdout)
    assert [s["ref"] for s in listed["snapshots"]] == [ref]
    target = tmp_path / "back"
    restored = json.loads(run_module("recovery", "restore", ref, str(target), *args, cwd=info.project_root).stdout)
    assert (target / "kept.txt").read_text() == "keep me\n" and restored["restored"] == ref
