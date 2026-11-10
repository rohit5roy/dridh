"""Workspace-root and path containment validation."""

from __future__ import annotations

from pathlib import Path

from dridh.common.errors import DridhError, ErrorCode


def validate_workspace_root(value: str | Path) -> Path:
    """Validate that ``value`` is an existing absolute directory.

    The returned path is resolved. Relative paths, missing paths and non-directory
    paths are rejected before any workspace file or database access can occur.
    """

    raw = Path(value)
    if not raw.is_absolute():
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "workspace_root must be an absolute path.",
            details=(f"received: {raw}",),
        )
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError:
        raise DridhError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            "workspace_root does not exist.",
            details=(f"path: {raw}",),
        ) from None
    except OSError as exc:
        raise DridhError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            "workspace_root could not be resolved.",
            details=(f"path: {raw}", f"reason: {type(exc).__name__}"),
        ) from None
    if not resolved.is_dir():
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "workspace_root must be a directory.",
            details=(f"path: {resolved}",),
        )
    return resolved


def ensure_within_root(path: str | Path, root: str | Path) -> Path:
    """Resolve ``path`` and reject it when it escapes ``root``."""

    resolved_root = Path(root).resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "path could not be resolved safely.",
            details=(f"reason: {type(exc).__name__}",),
        ) from None
    if not resolved.is_relative_to(resolved_root):
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "path escapes the allowed workspace root.",
            details=(f"root: {resolved_root}",),
        )
    return resolved
