"""Deterministic acceptance checks. A worker's self-report is never accepted as proof."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dridh.common.redaction import redact
from dridh.common.subprocess import ProcessExecutionError, run_process
from dridh.orchestration.plan import AcceptanceCheck
from dridh.workspace.paths import ensure_within_root

MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class CheckResult:
    index: int
    check: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "check": self.check, "passed": self.passed, "detail": self.detail}


async def run_checks(
    checks: tuple[AcceptanceCheck, ...],
    workspace_root: Path,
    *,
    timeout_seconds: float,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for index, check in enumerate(checks):
        results.append(await _run_one(index, check, workspace_root, timeout_seconds))
    return results


async def _run_one(index: int, check: AcceptanceCheck, root: Path, timeout_seconds: float) -> CheckResult:
    label = check.describe()
    if check.type in ("file_exists", "file_contains"):
        try:
            target = ensure_within_root(check.path or "", root)
        except Exception:  # noqa: BLE001 - reported as a failed check, never raised
            return CheckResult(index, label, False, "path escapes the workspace")
        if not target.is_file():
            return CheckResult(index, label, False, "file does not exist")
        if check.type == "file_exists":
            return CheckResult(index, label, True, "file exists")
        if target.stat().st_size > MAX_FILE_BYTES:
            return CheckResult(index, label, False, "file is too large to inspect")
        content = target.read_text(encoding="utf-8", errors="replace")
        if check.text and check.text in content:
            return CheckResult(index, label, True, "text found")
        return CheckResult(index, label, False, "text not found")
    try:
        process = await run_process(
            "python3",
            list(check.argv[1:]),
            cwd=root,
            allowed_roots=(root,),
            env={"PYTHONDONTWRITEBYTECODE": "1", "CI": "1", "NO_COLOR": "1"},
            timeout=timeout_seconds,
            clean_env=True,
        )
    except ProcessExecutionError as exc:
        reason = "timed out" if exc.timed_out else "could not run"
        return CheckResult(index, label, False, f"command {reason}")
    if process.exit_code == 0:
        return CheckResult(index, label, True, "exit code 0")
    tail = str(redact((process.stderr or process.stdout)[-400:]))
    return CheckResult(index, label, False, f"exit code {process.exit_code}: {tail.strip()}")
