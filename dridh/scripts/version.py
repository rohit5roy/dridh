#!/usr/bin/env python3
"""The Dridh version: read it from its single source, ``src/dridh/__init__.py``, and work out the next one.

Versions are ``X.Y.Z`` (a release) or ``X.Y.ZrcN`` (a release candidate; PEP 440's normal form of ``X.Y.Z-rc.N`` or
``X.Y.Z.RC-N``). ``main`` always carries the next version to release, and every release moves it forward:

    after releasing X.Y.Z       main becomes X.(Y+1).0rc1      (0.7.9 -> 0.8.0rc1)
    after releasing X.Y.ZrcN    main becomes X.Y.Zrc(N+1)      (0.8.0rc1 -> 0.8.0rc2)
    releasing "stable" from X.Y.ZrcN publishes X.Y.Z           (0.8.0rc3 -> 0.8.0, then main becomes 0.9.0rc1)

Usage (no dependencies, no import of Dridh):
    version.py                      the current version
    version.py semver [VERSION]     the npm/SemVer form (0.8.0rc1 -> 0.8.0-rc.1)
    version.py release rc|stable    the version a release of that kind publishes from the current one
    version.py next [VERSION]       the version main moves to after releasing VERSION (default: the current one)
    version.py set VERSION          write VERSION into src/dridh/__init__.py
    version.py check-newer TAG...   fail unless the current version is newer than every released tag (vX.Y.Z...)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "dridh" / "__init__.py"
_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$")


def version() -> str:
    match = _LINE.search(SOURCE.read_text(encoding="utf-8"))
    if not match:
        sys.exit(f"no __version__ in {SOURCE}")
    return match.group(1)


def parse(value: str) -> tuple[int, int, int, int | None]:
    """``"0.8.0rc1"`` -> ``(0, 8, 0, 1)``; ``"0.7.9"`` -> ``(0, 7, 9, None)``. Accepts a leading ``v``."""
    match = _VERSION.match(value.removeprefix("v"))
    if not match:
        raise ValueError(f"{value!r} is not X.Y.Z or X.Y.ZrcN")
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), int(rc) if rc is not None else None


def _format(major: int, minor: int, patch: int, rc: int | None) -> str:
    return f"{major}.{minor}.{patch}" + (f"rc{rc}" if rc is not None else "")


def order_key(value: str) -> tuple[int, int, int, int, int]:
    """Sorts versions correctly: a release candidate comes before the release it leads to."""
    major, minor, patch, rc = parse(value)
    return (major, minor, patch, 0 if rc is not None else 1, rc or 0)


def semver(value: str) -> str:
    major, minor, patch, rc = parse(value)
    return f"{major}.{minor}.{patch}" + (f"-rc.{rc}" if rc is not None else "")


def release_of(current: str, kind: str) -> str:
    major, minor, patch, rc = parse(current)
    if kind == "rc":
        if rc is None:
            raise ValueError(f"{current} is not a release candidate; release it as stable")
        return current
    if kind == "stable":
        return _format(major, minor, patch, None)
    raise ValueError(f"unknown release kind {kind!r} (rc or stable)")


def next_after(released: str) -> str:
    major, minor, patch, rc = parse(released)
    if rc is None:
        return _format(major, minor + 1, 0, 1)
    return _format(major, minor, patch, rc + 1)


def set_version(value: str) -> None:
    parse(value)
    text = SOURCE.read_text(encoding="utf-8")
    if not _LINE.search(text):
        sys.exit(f"no __version__ in {SOURCE}")
    SOURCE.write_text(_LINE.sub(f'__version__ = "{value}"', text, count=1), encoding="utf-8")


def check_newer(current: str, tags: list[str]) -> str | None:
    """An error message when ``current`` is not newer than every released tag, else None."""
    released = [tag for tag in tags if _VERSION.match(tag.removeprefix("v"))]
    if not released:
        return None
    latest = max(released, key=order_key)
    if order_key(current) <= order_key(latest):
        return f"version {current} is not newer than the released {latest}; versions only move forward"
    return None


def main(argv: list[str]) -> int:
    try:
        if not argv:
            print(version())
        elif argv[0] == "semver":
            print(semver(argv[1] if len(argv) > 1 else version()))
        elif argv[0] == "release" and len(argv) == 2:
            print(release_of(version(), argv[1]))
        elif argv[0] == "next":
            print(next_after(argv[1] if len(argv) > 1 else version()))
        elif argv[0] == "set" and len(argv) == 2:
            set_version(argv[1])
        elif argv[0] == "check-newer":
            error = check_newer(version(), argv[1:])
            if error:
                print(error, file=sys.stderr)
                return 1
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
