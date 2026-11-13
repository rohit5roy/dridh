"""What the Executor may run and how.

Only test commands from a short allow-list run, always as an argument list (no shell), inside the story's
worktree, with a scrubbed environment (no credentials), a timeout and bounded redacted output. Network access
is allowed because real-dependency tests (Testcontainers, package downloads) need it.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.common.subprocess import ProcessExecutionError, run_process

# Command prefixes that may run. Anything after the prefix is checked by ``_ARGUMENT``.
ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "pytest"),
    ("python3", "-m", "unittest"),
    ("pytest",),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("yarn", "test"),
    ("mvn", "test"),
    ("mvn", "-q", "test"),
    ("gradle", "test"),
    ("./gradlew", "test"),
    ("go", "test"),
)
_ARGUMENT = re.compile(r"^[A-Za-z0-9_@%+=:,.\-/\[\]]+$")
MAX_ARGUMENTS = 30
MAX_OUTPUT_CHARS = 4000
TEST_ENV = {"CI": "1", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def validate_command(argv: list[str]) -> list[str]:
    """Return ``argv`` if it is an allowed test command, else raise ``VALIDATION_FAILED``."""
    if not argv or not all(isinstance(item, str) for item in argv) or len(argv) > MAX_ARGUMENTS:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "A test command must be a non-empty list of at most 30 strings.")
    if not any(tuple(argv[: len(prefix)]) == prefix for prefix in ALLOWED_PREFIXES):
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"'{' '.join(argv[:3])}' is not an allowed test command.",
                         details=tuple(" ".join(prefix) for prefix in ALLOWED_PREFIXES))
    for item in argv:
        if not _ARGUMENT.match(item) or ".." in Path(item).parts or item.startswith("/"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Test command argument {item!r} is not allowed.")
    return list(argv)


def parse_command(text: str) -> list[str]:
    """Split a free-text command (for example a Table 15 execution command) and validate it."""
    try:
        return validate_command(shlex.split(text))
    except ValueError as exc:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "The test command could not be parsed.", details=(str(exc),)) from None


def detect_test_command(root: Path) -> list[str] | None:
    """The conventional test command for the project in ``root``, or ``None`` if none is recognised."""
    if (root / "pom.xml").is_file():
        return ["mvn", "-q", "test"]
    if (root / "gradlew").is_file():
        return ["./gradlew", "test"]
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return ["gradle", "test"]
    if (root / "package.json").is_file():
        return ["npm", "test"]
    if (root / "go.mod").is_file():
        return ["go", "test", "-v", "./..."]  # -v names each test, so flows can be proven per test
    if any((root / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini")) or (root / "tests").is_dir():
        return ["python3", "-m", "pytest", "-q", "-rA"]  # -rA lists every test with its outcome
    return None


async def run_test_command(argv: list[str], cwd: Path, *, timeout: float) -> dict[str, Any]:
    """Run one validated test command and return its evidence."""
    argv = validate_command(argv)
    started = time.time()
    try:
        process = await run_process(
            argv[0], argv[1:], cwd=cwd, allowed_roots=(cwd,), env=TEST_ENV, timeout=timeout, clean_env=True,
        )
    except ProcessExecutionError as exc:
        return {"command": argv, "passed": False, "exit_code": None, "timed_out": exc.timed_out,
                "output": str(redact(exc.message))[:MAX_OUTPUT_CHARS]}
    full = (process.stdout + "\n" + process.stderr).strip()
    output = str(redact(full))[-MAX_OUTPUT_CHARS:]
    return {"command": argv, "passed": process.exit_code == 0, "exit_code": process.exit_code, "timed_out": False, "output": output,
            "test_results": per_test_results(full, cwd, started)}


def per_test_results(output: str, root: Path, since: float) -> list[dict[str, Any]] | None:
    """Each test's name and outcome, read from the whole output (before it is shortened) and fresh JUnit reports."""
    from dridh.executor.flow_coverage import parse_results

    results = parse_results(output, root, since)
    return None if results is None else [{"name": r.name[:300], "passed": r.passed, "skipped": r.skipped} for r in results]


MOCK_PATTERNS = (
    re.compile(r"\bunittest\.mock\b|\bMagicMock\b|\bmock\.patch\b|\bmonkeypatch\b|\bpytest_mock\b|\bmocker\b"),
    re.compile(r"\bjest\.(?:mock|fn|spyOn)\b|\bsinon\.(?:stub|mock|spy)\b|\bvi\.(?:mock|fn)\b"),
    re.compile(r"\bMockito\b|@Mock\b|@MockBean\b|\bmock\("),
    re.compile(r"\bWireMock\b|\bgomock\b|\bhttpmock\b"),
)


def new_mocks(diff_text: str) -> list[str]:
    """Added lines in a unified diff that introduce a mock, stub or fake dependency."""
    found: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") and any(p.search(line) for p in MOCK_PATTERNS):
            found.append(line[1:].strip()[:160])
    return found


_SKIPPED = (re.compile(r"\bOK \((?:[^)]*?)skipped=(\d+)"), re.compile(r"FAILED \((?:[^)]*?)skipped=(\d+)"),  # unittest
            re.compile(r"\b(\d+) skipped\b"),  # pytest
            re.compile(r"#\s*skip\s+(\d+)"), re.compile(r"\bTests:.*?(\d+) skipped"))  # node --test, jest


def skipped_tests(output: str) -> int:
    """How many tests the runner reports as skipped (0 when it does not say). Found in a real run: a story's
    acceptance test skipped itself where the container could not download a build tool, and "OK (skipped=1)" passed."""
    counts = [int(m.group(1)) for pattern in _SKIPPED for m in pattern.finditer(output)]
    return max(counts, default=0)
