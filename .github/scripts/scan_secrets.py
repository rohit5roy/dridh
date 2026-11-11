#!/usr/bin/env python3
"""Fail when credential-shaped material appears in tracked files or job evidence.

Two modes:
    --tracked          scan every file tracked by git in the current repository
    --paths <dir...>  scan the given files or directories (for example job evidence)

Findings print the file, line number, pattern name and a masked preview only.
Secret values are never printed in full. Synthetic fixtures used by the redaction
tests are allowlisted by exact value, because they exist to prove that redaction
works. Exits 1 when any finding is present.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ALLOWLIST_FILE = Path(__file__).resolve().parent / "secret-scan-allowlist.txt"
BINARY_SUFFIXES = frozenset({".whl", ".zip", ".sqlite3", ".sqlite", ".png", ".jpg", ".gif", ".pdf", ".gz", ".tgz"})
PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key_block": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "openai_style_key": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "bearer_token": re.compile(r"Bearer\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{24,}"),
}
ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|password)\b"
    r"[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{16,})"
)


def tracked_files(root: Path) -> list[Path]:
    output = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [root / name for name in output.decode().split("\0") if name]


def expand(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(child for child in path.rglob("*") if child.is_file())
        elif path.is_file():
            files.append(path)
    return files


def mask(value: str) -> str:
    return value[:4] + "****" if len(value) > 4 else "****"


def load_allowlist() -> tuple[str, ...]:
    entries = []
    for raw in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return tuple(entries)


ALLOWLIST = load_allowlist()


def scan(path: Path) -> tuple[list[tuple[int, str, str]], int]:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return [], 0
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        return [], 0
    findings: list[tuple[int, str, str]] = []
    allowed = 0
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
        candidates: list[tuple[str, str]] = []
        for name, pattern in PATTERNS.items():
            match = pattern.search(line)
            if match:
                candidates.append((name, match.group(0)))
        for match in ASSIGNMENT.finditer(line):
            value = match.group(1)
            if "REDACTED" in value or value.startswith(("[", "$")):
                continue
            candidates.append(("credential_assignment", value))
        for name, value in candidates:
            if any(fixture in line for fixture in ALLOWLIST):
                allowed += 1
                continue
            findings.append((number, name, mask(value)))
    return findings, allowed


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tracked", action="store_true")
    group.add_argument("--paths", nargs="+", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    root = args.root.resolve()
    files = tracked_files(root) if args.tracked else expand([p.resolve() for p in args.paths])
    total = 0
    suppressed = 0
    scanned = 0
    for path in files:
        if not path.exists():
            continue
        scanned += 1
        findings, allowed = scan(path)
        suppressed += allowed
        for number, name, masked in findings:
            total += 1
            print(f"FINDING {path.relative_to(root) if path.is_relative_to(root) else path}:{number} {name} {masked}")
    print(f"scanned_files={scanned} findings={total} allowlisted_fixture_hits={suppressed} allowlist_entries={len(ALLOWLIST)}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
