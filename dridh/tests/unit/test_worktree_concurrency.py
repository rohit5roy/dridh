"""Concurrent worktree creation must not race (found by the delivery tests: 'failed to read .git/worktrees/<id>/commondir')."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from dridh.workspace.worktree import WorktreeManager


def _repo(path: Path) -> Path:
    path.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)
    return path


def test_many_worktrees_created_at_once_all_succeed(tmp_path):
    root = _repo(tmp_path / "repo")
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def make(index: int) -> None:
        manager = WorktreeManager(root)  # separate instances, like the story runner and the delivery engine
        barrier.wait()
        try:
            manager.allocate(f"wt-{index}", f"S-{index}", f"branch-{index}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{index}: {exc} {getattr(exc, 'details', '')}")

    threads = [threading.Thread(target=make, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True, check=True).stdout
    assert listed.count("worktree ") == 9  # the main checkout plus eight
