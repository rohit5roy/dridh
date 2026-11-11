#!/usr/bin/env python3
"""Validate the dridh wheel before installation.

Checks the required runtime modules, the console entry point, the pinned runtime
dependencies and the Python requirement. Rejects tests, caches, virtualenvs,
local databases and plugin sources. Prints a JSON report and exits 1 on any
violation.

Usage:
    .github/scripts/check_wheel.py <path-to-wheel>
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

REQUIRED = (
    "dridh/__init__.py",
    "dridh/__main__.py",
    "dridh/cli/main.py",
    "dridh/common/bridge.py",
    "dridh/common/errors.py",
    "dridh/common/logging.py",
    "dridh/common/protocol.py",
    "dridh/common/redaction.py",
    "dridh/common/subprocess.py",
    "dridh/integrations/__init__.py",
    "dridh/integrations/opencode.py",
    "dridh/workspace/paths.py",
)
FORBIDDEN = (
    re.compile(r"(^|/)tests/"),
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)\.venv/"),
    re.compile(r"(^|/)node_modules/"),
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"\.sqlite3?$"),
    re.compile(r"(^|/)opencode/(agents|plugins|commands|skills)/"),
)
METADATA_REQUIRED = (
    "Name: dridh",
    "Requires-Dist: pydantic==2.14.3",
    "Requires-Dist: typer==0.27.3",
)
REQUIRES_PYTHON = frozenset({">=3.13", "<3.14"})
ENTRY_POINT = "dridh = dridh.cli.main:main"


def inspect(wheel: Path) -> dict:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        dist_info = next((name.split("/")[0] for name in names if name.endswith(".dist-info/METADATA")), None)
        if dist_info is None:
            raise SystemExit(json.dumps({"error": "wheel has no METADATA file"}))
        metadata = archive.read(f"{dist_info}/METADATA").decode("utf-8")
        entry_points_name = f"{dist_info}/entry_points.txt"
        entry_points = archive.read(entry_points_name).decode("utf-8") if entry_points_name in names else ""

    missing = [path for path in REQUIRED if path not in names]
    forbidden = sorted({name for name in names for pattern in FORBIDDEN if pattern.search(name)})
    metadata_missing = [line for line in METADATA_REQUIRED if line not in metadata.splitlines()]
    requires_python = next(
        (line.split(":", 1)[1].strip() for line in metadata.splitlines() if line.startswith("Requires-Python:")),
        "",
    )
    # Compare specifier sets: hatchling may write the same range in a different order.
    if frozenset(part.strip() for part in requires_python.split(",")) != REQUIRES_PYTHON:
        metadata_missing.append(f"Requires-Python: expected {sorted(REQUIRES_PYTHON)}, found {requires_python!r}")
    entry_point_ok = ENTRY_POINT in [line.strip() for line in entry_points.splitlines()]
    violations = []
    if missing:
        violations.append({"rule": "required_modules", "items": missing})
    if forbidden:
        violations.append({"rule": "forbidden_files", "items": forbidden})
    if metadata_missing:
        violations.append({"rule": "metadata", "items": metadata_missing})
    if not entry_point_ok:
        violations.append({"rule": "entry_point", "items": [ENTRY_POINT]})
    return {
        "wheel": wheel.name,
        "sha256": digest,
        "size_bytes": wheel.stat().st_size,
        "file_count": len(names),
        "required_modules_checked": len(REQUIRED),
        "forbidden_patterns_checked": len(FORBIDDEN),
        "metadata_checks": len(METADATA_REQUIRED),
        "entry_point_present": entry_point_ok,
        "violations": violations,
        "passed": not violations,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_wheel.py <path-to-wheel>", file=sys.stderr)
        return 2
    report = inspect(Path(argv[1]))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
