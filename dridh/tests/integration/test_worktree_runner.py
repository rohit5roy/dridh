"""Git-backed worktree isolation through the actual OpenCodeRunner path.

Proves that workers execute in isolated worktrees, not in the shared project root.
Uses a deterministic fake OpenCode executable that records its cwd and creates files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dridh.integrations.opencode import OpenCodeRunner
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


def _make_fake_opencode(tmp_path: Path, marker_file: str = "worker_output.txt") -> Path:
    """Create a fake OpenCode executable that records cwd and creates a file."""
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "CWD=$(pwd)" > {marker_file}\n'
        f'echo "created in $(pwd)" > result.txt\n'
        'echo \'{"type":"step_start","timestamp":1,"sessionID":"ses_test12345678","part":{"type":"step-start","sessionID":"ses_test12345678"}}\'\n'
        'echo \'{"type":"text","timestamp":2,"sessionID":"ses_test12345678","part":{"type":"text","text":"WORKER_OK","sessionID":"ses_test12345678"}}\'\n'
        'echo \'{"type":"step_finish","timestamp":3,"sessionID":"ses_test12345678","part":{"type":"step-finish","cost":0,"tokens":{"input":0,"output":0,"reasoning":0},"sessionID":"ses_test12345678"}}\'\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.asyncio
async def test_runner_uses_worktree_as_cwd(git_project: Path, tmp_path: Path) -> None:
    """Verify that OpenCodeRunner uses worktree_path as cwd."""
    mgr = WorktreeManager(git_project)
    info = mgr.allocate("wt-runner", "T-001")
    fake = _make_fake_opencode(tmp_path)

    runner = OpenCodeRunner(
        workspace_root=info.path,
        executable=str(fake),
        timeout=30,
    )
    result = await runner.run(agent="dridh/test", prompt="test prompt")
    assert result.exit_code == 0
    assert result.text == "WORKER_OK"

    # Verify the fake opencode ran in the worktree
    cwd_record = (info.path / "worker_output.txt").read_text()
    assert str(info.path) in cwd_record, f"Expected worktree path in cwd record, got: {cwd_record}"

    # Verify the file was created in the worktree, not the project root
    assert (info.path / "result.txt").exists()
    assert not (git_project / "result.txt").exists()

    mgr.cleanup("wt-runner", force=True)


@pytest.mark.asyncio
async def test_runner_fails_closed_without_worktree(git_project: Path, tmp_path: Path) -> None:
    """Verify that runner uses project_root when no worktree is provided."""
    fake = _make_fake_opencode(tmp_path)
    runner = OpenCodeRunner(
        workspace_root=git_project,
        executable=str(fake),
        timeout=30,
    )
    result = await runner.run(agent="dridh/test", prompt="test prompt")
    assert result.exit_code == 0
    assert (git_project / "result.txt").exists()


def test_worktree_allocation_fails_closed(git_project: Path) -> None:
    """Verify that worktree allocation failure is handled (non-git project)."""
    non_git = git_project.parent / "non_git_project"
    non_git.mkdir()
    (non_git / "README.md").write_text("test\n")
    from dridh.workspace.worktree import WorktreeManager
    mgr = WorktreeManager(non_git)
    try:
        mgr.allocate("wt-fail", "T-001")
        raise AssertionError("Expected DridhError")
    except Exception as e:
        assert "Git command failed" in str(e) or "not a git" in str(e).lower() or "fatal" in str(e).lower()
