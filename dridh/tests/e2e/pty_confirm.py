"""Drive a Dridh command that asks for a typed confirmation, through a real pseudo-terminal.

Used by the E2E scenario and by tests. The confirmation string is read from the
prompt the command prints, so the driver types exactly what a person would see.
"""

from __future__ import annotations

import os
import pty
import re
import select
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfirmResult:
    exit_code: int
    output: str
    prompted: bool


def run_with_typed_confirmation(argv: list[str], *, timeout: float = 30.0, answer: str | None = None) -> ConfirmResult:
    """Run ``argv`` in a pty, wait for the 'Type exactly:' prompt, and type the expected confirmation."""

    pid, descriptor = pty.fork()
    if pid == 0:
        os.execv(argv[0], argv)
    collected = b""
    prompted = False
    deadline = time.monotonic() + timeout
    expected: str | None = None
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(descriptor, 4096)
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
            text = collected.decode(errors="replace")
            match = re.search(r"Type exactly: (.+)\r?\n", text)
            if match and not prompted:
                prompted = True
                expected = match.group(1).strip()
                typed = answer if answer is not None else expected
                os.write(descriptor, (typed + "\n").encode())
    try:
        _, status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        exit_code = -1
    return ConfirmResult(exit_code=exit_code, output=collected.decode(errors="replace"), prompted=prompted)
