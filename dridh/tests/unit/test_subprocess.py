from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.subprocess import ProcessExecutionError, run_process


def test_success_captures_stdout_and_stderr_separately() -> None:
    result = asyncio.run(
        run_process(
            sys.executable,
            ["-c", "import sys; print('OUT'); print('ERR', file=sys.stderr)"],
        )
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "OUT"
    assert result.stderr.strip() == "ERR"
    assert result.duration_seconds >= 0


def test_exit_code_is_preserved_without_check() -> None:
    result = asyncio.run(run_process(sys.executable, ["-c", "raise SystemExit(7)"]))
    assert result.exit_code == 7


def test_check_raises_redacted_process_error() -> None:
    code = "import sys; print('token=supersecretvalue', file=sys.stderr); raise SystemExit(9)"
    with pytest.raises(ProcessExecutionError) as exc_info:
        asyncio.run(run_process(sys.executable, ["-c", code], check=True))
    payload = exc_info.value.to_dict()
    assert payload["code"] == "PROCESS_EXECUTION_FAILED"
    assert exc_info.value.exit_code == 9
    assert "supersecretvalue" not in str(payload)


def test_timeout_terminates_process() -> None:
    started = time.perf_counter()
    with pytest.raises(ProcessExecutionError) as exc_info:
        asyncio.run(run_process(sys.executable, ["-c", "import time; time.sleep(10)"], timeout=0.2))
    assert "timed out" in exc_info.value.message
    assert exc_info.value.retryable is True
    assert time.perf_counter() - started < 5


def test_cancellation_terminates_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"

    async def scenario() -> None:
        code = (
            "import os, time; "
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )
        task = asyncio.create_task(run_process(sys.executable, ["-c", code]))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())


def test_input_text_is_passed_on_stdin() -> None:
    result = asyncio.run(
        run_process(
            sys.executable,
            ["-c", "import sys; print(sys.stdin.read().upper())"],
            input_text="hello",
        )
    )
    assert result.stdout.strip() == "HELLO"


def test_shell_metacharacters_are_not_interpreted() -> None:
    result = asyncio.run(
        run_process(
            sys.executable,
            ["-c", "import sys; print(sys.argv[1])", "; echo HACKED"],
        )
    )
    assert result.stdout.splitlines() == ["; echo HACKED"]


def test_string_arguments_are_rejected() -> None:
    with pytest.raises(DridhError) as exc_info:
        asyncio.run(run_process(sys.executable, "print('x')"))
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED


def test_missing_executable_is_rejected() -> None:
    with pytest.raises(ProcessExecutionError):
        asyncio.run(run_process("/nonexistent/dridh-executable"))


def test_cwd_outside_allowed_roots_is_rejected(workspace: Path, tmp_path: Path) -> None:
    with pytest.raises(DridhError) as exc_info:
        asyncio.run(
            run_process(
                sys.executable,
                ["-c", "print('x')"],
                cwd=tmp_path,
                allowed_roots=(workspace,),
            )
        )
    assert exc_info.value.code is ErrorCode.VALIDATION_FAILED


def test_timeout_error_has_timed_out_flag() -> None:
    with pytest.raises(ProcessExecutionError) as exc_info:
        asyncio.run(run_process(sys.executable, ["-c", "import time; time.sleep(5)"], timeout=0.1))
    assert exc_info.value.timed_out is True
    assert exc_info.value.retryable is True
