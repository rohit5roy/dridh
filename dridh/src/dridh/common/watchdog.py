"""Worker-side parent watchdog for bounded orphan termination.

Phase 2 verified SIGTERM and parent-orphan paths, but a SIGKILL of the bridge
could leave the child worker running until it finished (SF-01). This module
provides a watchdog that monitors the parent process and terminates the worker's
process group if the parent disappears.

The watchdog uses os.getppid() polling with a bounded interval. It avoids
PID-reuse mistakes by checking that the parent's start time matches the
originally recorded value. If the parent disappears or changes, the watchdog
terminates the entire process group.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable

_WATCHDOG_INTERVAL = 1.0  # seconds
_WATCHDOG_GRACE = 3.0  # seconds after parent death before kill


def _parent_start_time(ppid: int) -> float | None:
    """Best-effort parent start time. Returns None if unavailable."""
    try:
        # On Linux, read /proc/<ppid>/stat field 22 (starttime).
        with open(f"/proc/{ppid}/stat") as f:
            fields = f.read().split()
        return float(fields[21])
    except (OSError, IndexError, ValueError):
        return None


def start_parent_watchdog(
    *,
    parent_pid: int | None = None,
    on_death: Callable[[], None] | None = None,
    interval: float = _WATCHDOG_INTERVAL,
    grace: float = _WATCHDOG_GRACE,
) -> threading.Thread:
    """Start a daemon thread that monitors the parent process.

    If the parent dies (or is replaced by a different process), the watchdog
    waits for ``grace`` seconds, then calls ``on_death`` (default: kill the
    current process group with SIGKILL).
    """

    if parent_pid is None:
        parent_pid = os.getppid()

    original_start = _parent_start_time(parent_pid)

    def _default_death() -> None:
        # Kill all child processes first (workers use start_new_session=True,
        # so they are not in our process group but are still our children).
        try:
            import subprocess as _sp
            result = _sp.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True)
            for pid_str in result.stdout.split():
                try:
                    os.kill(int(pid_str), signal.SIGKILL)
                except (OSError, ValueError, ProcessLookupError):
                    pass
        except (OSError, FileNotFoundError):
            pass
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(os.getpid(), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    death_handler = on_death or _default_death

    def _watch() -> None:
        consecutive_missing = 0
        while True:
            time.sleep(interval)
            current_ppid = os.getppid()
            if current_ppid != parent_pid:
                # Parent changed (likely died and was reaped).
                consecutive_missing += 1
                if consecutive_missing >= 2:
                    time.sleep(grace)
                    death_handler()
                    return
            else:
                # Parent still alive; verify start time hasn't changed (PID reuse).
                if original_start is not None:
                    current_start = _parent_start_time(parent_pid)
                    if current_start is not None and current_start != original_start:
                        # PID was reused by a different process.
                        consecutive_missing += 1
                        if consecutive_missing >= 2:
                            time.sleep(grace)
                            death_handler()
                            return
                    else:
                        consecutive_missing = 0
                else:
                    consecutive_missing = 0

    thread = threading.Thread(target=_watch, daemon=True, name="dridh-parent-watchdog")
    thread.start()
    return thread
