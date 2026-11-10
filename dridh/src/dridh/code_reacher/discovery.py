"""Safe source discovery and snapshot identity for the Code Reacher index."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dridh.code_reacher.languages import language_for

EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", "target", ".gradle", ".idea", ".vscode", ".opencode", ".next", ".nuxt", "coverage",
    "site-packages", ".worktrees",
})
MAX_FILE_BYTES = 1_000_000
MAX_AVERAGE_LINE = 400  # bytes per line above which a file is minified or generated, not source a person maintains
_MINIFIED_NAME = re.compile(r"[.-]min\.(?:js|mjs|cjs)$")


@dataclass(frozen=True)
class SourceFile:
    path: str  # POSIX path relative to the repository root
    language: str
    absolute: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


def discover(root: Path) -> tuple[list[SourceFile], list[Skipped]]:
    """Supported source files under ``root``. Never follows symlinks; skips oversized and binary files."""
    root = root.resolve()
    found: list[SourceFile] = []
    skipped: list[Skipped] = []
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if name not in EXCLUDED_DIRECTORIES and not name.endswith(".egg-info"))
        for name in sorted(names):
            absolute = Path(directory) / name
            relative = absolute.relative_to(root).as_posix()
            language = language_for(relative)
            if language is None:
                continue
            if absolute.is_symlink():
                skipped.append(Skipped(relative, "symlink"))
                continue
            size = absolute.stat().st_size
            if size > MAX_FILE_BYTES:
                skipped.append(Skipped(relative, f"larger than {MAX_FILE_BYTES} bytes"))
                continue
            data = absolute.read_bytes()
            if b"\x00" in data[:8192]:
                skipped.append(Skipped(relative, "binary content"))
                continue
            if _MINIFIED_NAME.search(name) or (size > 4096 and size / (data.count(b"\n") + 1) > MAX_AVERAGE_LINE):
                skipped.append(Skipped(relative, "minified or generated"))
                continue
            found.append(SourceFile(relative, language, absolute, hashlib.sha256(data).hexdigest(), size))
    return found, skipped


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20, check=False)  # noqa: S603, S607
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def snapshot_id(root: Path, files: list[SourceFile]) -> str:
    """The Git HEAD when the tree is clean, else a digest of every indexed file's path and content hash.

    The digest form means the same bytes always give the same identifier, so re-indexing an unchanged
    working tree is a no-op even without Git.
    """
    head = _git(root, "rev-parse", "HEAD")
    if head:
        dirty = _git(root, "status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude).opencode")
        if dirty == "":
            return head
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda source: source.path):
        digest.update(f"{item.path}\0{item.sha256}\n".encode())
    return "tree:" + digest.hexdigest()[:40]
