"""Dridh-managed Git worktree lifecycle for parallel worker isolation.

Workers must not modify the shared project directory. This module allocates a
task-specific worktree, records it in the state store, and cleans it up only
when the orchestration service authorises removal. Workers never remove
worktrees themselves.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows: only the in-process lock applies
    fcntl = None  # type: ignore[assignment]

from dridh.common.errors import DridhError, ErrorCode

_WORKTREE_THREAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class WorktreeInfo:
    worktree_id: str
    path: Path
    branch: str
    base_commit: str


_BYPRODUCT_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def is_test_byproduct(path: str) -> bool:
    """Bytecode and tool caches that running tests creates; never a story's own change."""
    parts = path.split("/")
    return path.endswith((".pyc", ".pyo", ".pyd")) or any(part in _BYPRODUCT_DIRS for part in parts[:-1]) or parts[-1] == ".coverage"


class WorktreeManager:
    """Allocates and removes Git worktrees under a Dridh-managed directory."""

    def __init__(self, project_root: Path, managed_root: Path | None = None) -> None:
        self.project_root = project_root
        self.managed_root = managed_root or (project_root / ".opencode" / "dridh" / "worktrees")
        self.managed_root.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _worktree_set_lock(self) -> Iterator[None]:
        """Serialise changes to the repository's set of worktrees.

        Two concurrent ``git worktree add`` calls can fail: while one is still writing its ``commondir`` file, the other scans every
        worktree and dies with "failed to read .git/worktrees/<id>/commondir". A thread lock covers parallel stories in this process
        and a file lock covers a second Dridh process on the same repository.
        """
        with _WORKTREE_THREAD_LOCK:
            handle = None
            try:
                if fcntl is not None:
                    handle = (self.managed_root / ".worktree.lock").open("a")
                    fcntl.flock(handle, fcntl.LOCK_EX)
                yield
            finally:
                if handle is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        if args[:1] == ("worktree",) and args[1:2] in (("add",), ("remove",), ("prune",), ("move",)):
            with self._worktree_set_lock():
                return self._run_git(*args, cwd=cwd)
        return self._run_git(*args, cwd=cwd)

    def _run_git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.project_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DridhError(
                ErrorCode.WORKER_FAILED,
                f"Git command failed: {' '.join(args)}",
                details=(result.stderr.strip()[:300],),
            )
        return result.stdout.strip()

    def allocate(self, worktree_id: str, task_id: str, branch_name: str | None = None) -> WorktreeInfo:
        """Create a worktree for a task. Idempotent: returns existing if present."""
        target = self.managed_root / worktree_id
        if target.exists():
            commit = self._git("rev-parse", "HEAD", cwd=target)
            branch = self._git("branch", "--show-current", cwd=target)
            return WorktreeInfo(worktree_id=worktree_id, path=target, branch=branch, base_commit=commit)

        branch = branch_name or f"dridh/{task_id}/{worktree_id}"
        base_commit = self._git("rev-parse", "HEAD")
        self._git("worktree", "add", "-b", branch, str(target), base_commit)
        return WorktreeInfo(worktree_id=worktree_id, path=target, branch=branch, base_commit=base_commit)

    def capture_diff(self, worktree_id: str) -> str:
        """Return the uncommitted diff in the worktree."""
        target = self.managed_root / worktree_id
        if not target.exists():
            raise DridhError(ErrorCode.STATE_CORRUPT, f"Worktree {worktree_id} does not exist.")
        tracked = self._git("diff", "HEAD", cwd=target)
        untracked = self._git("ls-files", "--others", "--exclude-standard", cwd=target)
        if untracked:
            tracked = tracked + "\n" + "\n".join(f"new file: {line}" for line in untracked.splitlines())
        return tracked

    def capture_changed_files(self, worktree_id: str) -> list[str]:
        """Return list of changed files (staged + unstaged) relative to HEAD."""
        target = self.managed_root / worktree_id
        if not target.exists():
            raise DridhError(ErrorCode.STATE_CORRUPT, f"Worktree {worktree_id} does not exist.")
        tracked = self._git("diff", "--name-only", "HEAD", cwd=target)
        untracked = self._git("ls-files", "--others", "--exclude-standard", cwd=target)
        result = [line for line in tracked.splitlines() if line.strip()]
        result.extend(line for line in untracked.splitlines() if line.strip())
        return result

    def commit_changes(self, worktree_id: str, message: str = "Dridh task changes") -> str:
        """Commit all changes in the worktree. Returns the commit SHA."""
        target = self.managed_root / worktree_id
        if not target.exists():
            raise DridhError(ErrorCode.STATE_CORRUPT, f"Worktree {worktree_id} does not exist.")
        self._git("add", "-A", cwd=target)
        # Check if there are changes to commit
        status = self._git("status", "--porcelain", cwd=target)
        if status:
            self._git("commit", "-q", "-m", message, cwd=target)
        return self._git("rev-parse", "HEAD", cwd=target)

    def allocate_from_commit(self, worktree_id: str, task_id: str, base_commit: str, branch_name: str | None = None) -> WorktreeInfo:
        """Allocate a worktree based on a specific commit (for dependent tasks)."""
        target = self.managed_root / worktree_id
        if target.exists():
            commit = self._git("rev-parse", "HEAD", cwd=target)
            branch = self._git("branch", "--show-current", cwd=target)
            return WorktreeInfo(worktree_id=worktree_id, path=target, branch=branch, base_commit=commit)

        branch = branch_name or f"dridh/{task_id}/{worktree_id}"
        self._git("worktree", "add", "-b", branch, str(target), base_commit)
        return WorktreeInfo(worktree_id=worktree_id, path=target, branch=branch, base_commit=base_commit)

    def integrate_prerequisites(self, integration_id: str, prerequisite_commits: list[str], base_commit: str | None = None) -> str:
        """Create an integration commit from multiple prerequisite commits.

        Creates a new branch, merges each prerequisite commit, and returns the
        resulting commit SHA. Raises DridhError on conflict.
        """
        if not prerequisite_commits:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "No prerequisite commits to integrate.")
        if base_commit is None:
            if len(prerequisite_commits) == 1:
                base_commit = self._git("rev-parse", f"{prerequisite_commits[0]}^")
            else:
                base_commit = self._git("merge-base", *prerequisite_commits)
        # Create a temporary worktree for integration.
        target = self.managed_root / integration_id
        if target.exists():
            # Reuse existing.
            pass
        else:
            branch = f"dridh/integration/{integration_id}"
            self._git("worktree", "add", "-b", branch, str(target), base_commit)
        # Merge each prerequisite commit.
        for commit in prerequisite_commits:
            try:
                self._git("merge", "--no-ff", "-m", f"Integrate {commit[:8]}", commit, cwd=target)
            except DridhError as exc:
                # Conflict - clean up and raise.
                try:
                    self._git("checkout", "--", ".", cwd=target)
                except DridhError:
                    pass
                raise DridhError(
                    ErrorCode.WORKER_FAILED,
                    f"Integration conflict merging {commit[:8]}.",
                    details=(str(exc.details)[:200],),
                ) from None
        result_sha = self._git("rev-parse", "HEAD", cwd=target)
        return result_sha

    BYPRODUCT_PATTERNS = ("__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".coverage")
    _EXCLUDE_MARKER = "# dridh: test byproducts are never a story's changes"

    def exclude_test_byproducts(self) -> None:
        """Keep files that running tests creates (bytecode, tool caches) out of every story's changes.

        Found in a real run: tests left ``__pycache__/*.pyc`` in the worktree, the next repair attempt staged them, and
        the ownership check rejected every repair. The patterns go into the repository's local exclude file
        (``.git/info/exclude``, shared by all worktrees and never committed), once."""
        common = Path(self._git("rev-parse", "--git-common-dir", cwd=self.project_root))
        exclude = (common if common.is_absolute() else self.project_root / common) / "info" / "exclude"
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if self._EXCLUDE_MARKER in current:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        block = "\n".join([self._EXCLUDE_MARKER, *self.BYPRODUCT_PATTERNS]) + "\n"
        exclude.write_text(current + ("" if not current or current.endswith("\n") else "\n") + block, encoding="utf-8")

    def unstage_test_byproducts(self, worktree: Path) -> list[str]:
        """Drop byproducts an earlier attempt already staged (an exclude rule does not unstage). Only newly added
        files are touched; anything tracked in HEAD stays as it is."""
        added = self._git("diff", "--cached", "--name-only", "--diff-filter=A", "HEAD", cwd=worktree).splitlines()
        byproducts = [name for name in added if is_test_byproduct(name)]
        if byproducts:
            self._git("rm", "-q", "--cached", "--", *byproducts, cwd=worktree)
        return byproducts

    RECOVERY_NAMESPACE = "refs/dridh/recovery"

    def dirty_files(self, worktree_id: str) -> list[str]:
        """Paths with uncommitted changes (modified, staged or untracked and not ignored)."""
        target = self.managed_root / worktree_id
        if not target.exists():
            return []
        tracked = self._git("diff", "--name-only", "HEAD", cwd=target).splitlines()  # modified, staged or deleted
        untracked = self._git("ls-files", "--others", "--exclude-standard", cwd=target).splitlines()  # new and not ignored
        return sorted({name for name in [*tracked, *untracked] if name})

    def keep_ref(self, worktree_id: str, name: str, commit: str) -> str:
        """Pin a commit under ``refs/dridh/recovery/<worktree-id>/`` so it survives branch deletion and garbage collection."""
        ref = f"{self.RECOVERY_NAMESPACE}/{worktree_id}/{name}"
        self._git("update-ref", ref, commit)
        return ref

    def snapshot(self, worktree_id: str, reason: str) -> dict[str, Any] | None:
        """Save the whole working tree (tracked changes and untracked files) as a commit on a recovery ref. ``None`` if clean.

        A temporary index is used, so the worktree, its real index and its branch are left exactly as they were.
        """
        target = self.managed_root / worktree_id
        changed = self.dirty_files(worktree_id)
        if not changed:
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            def run(*args: str) -> str:
                done = subprocess.run(["git", *args], cwd=str(target), capture_output=True, text=True, check=False, env=env, timeout=300)
                if done.returncode != 0:
                    raise DridhError(ErrorCode.PROCESS_EXECUTION_FAILED, f"git {args[0]} failed while saving a recovery snapshot.", details=(done.stderr.strip()[:300],))
                return done.stdout.strip()

            run("read-tree", "HEAD")
            run("add", "-A")
            tree = run("write-tree")
            message = f"Dridh recovery snapshot of {worktree_id} ({reason})\n\n" + "\n".join(changed[:200])
            commit = run("-c", "user.name=dridh", "-c", "user.email=dridh@localhost", "commit-tree", tree, "-p", "HEAD", "-m", message)
        ref = self.keep_ref(worktree_id, stamp, commit)
        return {"ref": ref, "commit": commit, "files": changed[:50], "file_count": len(changed), "reason": reason}

    def cleanup(self, worktree_id: str, *, force: bool = False, reason: str = "cleanup") -> dict[str, Any]:
        """Remove a worktree without ever losing uncommitted work.

        A dirty worktree is refused unless ``force`` is set; with ``force`` its whole working tree is first saved on a recovery ref
        (see :meth:`snapshot`). Only the orchestration service should call this.
        """
        target = self.managed_root / worktree_id
        if not target.exists():
            return {"removed": False, "snapshot": None}
        dirty = self.dirty_files(worktree_id)
        if dirty and not force:
            raise DridhError(
                ErrorCode.CONFIGURATION_CONFLICT,
                f"Worktree {worktree_id} has {len(dirty)} uncommitted change(s); it was not removed.",
                details=tuple(dirty[:10]),
                recovery="Review or commit the changes, or remove it deliberately with force (a recovery snapshot is saved first).",
            )
        saved = self.snapshot(worktree_id, reason) if dirty else None
        self._git("worktree", "remove", "--force", str(target))
        self._git("worktree", "prune")
        return {"removed": True, "snapshot": saved}

    def recovery_snapshots(self) -> list[dict[str, str]]:
        """Every recovery ref, newest last."""
        out = self._git("for-each-ref", "--format=%(refname) %(objectname) %(committerdate:iso8601) %(subject)", self.RECOVERY_NAMESPACE)
        rows = []
        for line in out.splitlines():
            ref, commit, rest = line.split(" ", 2)
            rows.append({"ref": ref, "commit": commit, "detail": rest})
        return rows

    def restore_snapshot(self, ref: str, directory: Path) -> Path:
        """Check a recovery ref out into a new directory (a detached worktree), leaving the snapshot in place."""
        if not ref.startswith(self.RECOVERY_NAMESPACE + "/"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Only recovery refs can be restored.", details=(ref,))
        if directory.exists():
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The restore directory already exists.", details=(str(directory),))
        self._git("worktree", "add", "--detach", str(directory), ref)
        return directory

    def list_worktrees(self) -> list[dict[str, Any]]:
        """List all worktrees managed by this project."""
        output = self._git("worktree", "list", "--porcelain")
        result = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if line.startswith("worktree "):
                if current:
                    result.append(current)
                current = {"path": line[len("worktree "):]}
            elif line.startswith("HEAD "):
                current["commit"] = line[len("HEAD "):]
            elif line.startswith("branch "):
                current["branch"] = line[len("branch "):]
        if current:
            result.append(current)
        return result
