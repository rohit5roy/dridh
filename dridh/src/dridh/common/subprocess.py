"""Safe asynchronous subprocess execution.

The abstraction always uses an explicit executable plus an argument array. It
never invokes a shell, captures stdout and stderr separately, preserves exit
codes and supports timeout and cancellation with process-group cleanup.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.workspace.paths import ensure_within_root


@dataclass(frozen=True)
class ProcessResult:
    executable: str
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False


class ProcessExecutionError(DridhError):
    """A subprocess could not be started or exited unsuccessfully."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        details: Sequence[str] | None = None,
        retryable: bool = False,
        timed_out: bool = False,
    ) -> None:
        super().__init__(
            ErrorCode.PROCESS_EXECUTION_FAILED,
            message,
            details=details,
            retryable=retryable,
        )
        self.exit_code = exit_code
        self.timed_out = timed_out


_CLEAN_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL")


def _resolve_executable(executable: str) -> str:
    if not executable or "\x00" in executable:
        raise ProcessExecutionError("executable path is empty or invalid.")
    candidate = Path(executable)
    if candidate.is_absolute() or os.sep in executable or (os.altsep and os.altsep in executable):
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise ProcessExecutionError(
                "executable does not exist.",
                details=(f"executable: {executable}",),
            ) from None
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ProcessExecutionError(
                "executable is not an executable file.",
                details=(f"executable: {executable}",),
            )
        return str(resolved)

    found = shutil.which(executable)
    if not found:
        raise ProcessExecutionError(
            "executable was not found on PATH.",
            details=(f"executable: {executable}",),
        )
    return str(Path(found).resolve())


def _validate_args(args: Sequence[str]) -> tuple[str, ...]:
    if isinstance(args, str | bytes):
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "arguments must be provided as an array, never as a shell string.",
        )
    validated: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "every subprocess argument must be a string.",
                details=(f"received: {type(arg).__name__}",),
            )
        if "\x00" in arg:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "subprocess arguments must not contain NUL bytes.")
        validated.append(arg)
    return tuple(validated)


def _redacted_command(executable: str, args: Sequence[str]) -> str:
    return str(redact(" ".join([executable, *args])))


def _within_root(path: Path, root: str | Path) -> bool:
    try:
        ensure_within_root(path, root)
        return True
    except (DridhError, OSError):
        return False


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - host is macOS in Phase 0/1
            process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, ProcessLookupError):
        pass


async def run_process(
    executable: str,
    args: Sequence[str] = (),
    *,
    cwd: str | Path | None = None,
    allowed_roots: Sequence[str | Path] = (),
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
    check: bool = False,
    clean_env: bool = False,
) -> ProcessResult:
    """Run one executable and return captured output.

    ``clean_env=True`` starts the child with a minimal allow-listed environment
    instead of inheriting the parent's, so credentials in the parent are not passed on.

    ``check=True`` raises :class:`ProcessExecutionError` for a non-zero exit.
    Timeout raises the same error; cancellation cleans up the process group and
    re-raises ``asyncio.CancelledError``.
    """

    resolved_executable = _resolve_executable(executable)
    resolved_args = _validate_args(args)
    if timeout is not None and timeout <= 0:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "timeout must be greater than zero.")

    working_directory: Path | None = None
    if cwd is not None:
        working_directory = Path(cwd)
        if not working_directory.is_absolute():
            working_directory = Path.cwd() / working_directory
        try:
            working_directory = working_directory.resolve(strict=True)
        except FileNotFoundError:
            raise DridhError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                "subprocess working directory does not exist.",
                details=(f"cwd: {working_directory}",),
            ) from None
        if not working_directory.is_dir():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "subprocess cwd must be a directory.")
        if allowed_roots and not any(
            _within_root(working_directory, root) for root in allowed_roots
        ):
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "subprocess cwd is outside the allowed workspace roots.",
                details=(f"cwd: {working_directory}",),
            )

    if clean_env:
        child_env = {key: os.environ[key] for key in _CLEAN_ENV_KEYS if key in os.environ}
    else:
        child_env = dict(os.environ)
    if env:
        child_env.update({str(key): str(value) for key, value in env.items()})

    command_text = _redacted_command(resolved_executable, resolved_args)
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            resolved_executable,
            *resolved_args,
            cwd=str(working_directory) if working_directory else None,
            env=child_env,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ProcessExecutionError(
            "subprocess could not be started.",
            details=(f"command: {command_text}", f"reason: {type(exc).__name__}"),
        ) from None

    payload = input_text.encode("utf-8") if input_text is not None else None
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=payload),
            timeout=timeout,
        )
    except TimeoutError:
        await _terminate(process)
        raise ProcessExecutionError(
            "subprocess timed out.",
            details=(f"command: {command_text}", f"timeout_seconds: {timeout}"),
            retryable=True,
            timed_out=True,
        ) from None
    except (asyncio.CancelledError, KeyboardInterrupt):
        await _terminate(process)
        raise

    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    result = ProcessResult(
        executable=resolved_executable,
        args=resolved_args,
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=round(time.perf_counter() - started, 6),
    )

    if check and result.exit_code != 0:
        raise ProcessExecutionError(
            "subprocess exited unsuccessfully.",
            exit_code=result.exit_code,
            details=(
                f"command: {command_text}",
                f"exit_code: {result.exit_code}",
                f"stderr: {str(redact(result.stderr[-2000:]))}",
            ),
        )
    return result
