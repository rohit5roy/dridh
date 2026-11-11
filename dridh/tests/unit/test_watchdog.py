"""Parent watchdog: basic lifecycle."""

from __future__ import annotations

import os
import threading
import time

from dridh.common.watchdog import start_parent_watchdog


def test_watchdog_starts_and_is_daemon() -> None:
    thread = start_parent_watchdog(parent_pid=os.getppid(), interval=0.1, grace=0.1)
    assert thread.is_alive()
    assert thread.daemon is True
    assert thread.name == "dridh-parent-watchdog"


def test_watchdog_does_not_fire_for_live_parent() -> None:
    fired = threading.Event()

    def on_death() -> None:
        fired.set()

    thread = start_parent_watchdog(
        parent_pid=os.getppid(),
        on_death=on_death,
        interval=0.1,
        grace=0.1,
    )
    time.sleep(0.5)
    assert not fired.is_set()
    # Thread should still be alive.
    assert thread.is_alive()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead for our purposes
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        return False


def test_killing_the_parent_terminates_the_bridge_and_its_new_session_workers(tmp_path) -> None:
    """OpenCode (the parent) dying with SIGKILL must not leave workers running."""
    import signal
    import subprocess
    import sys

    pids = tmp_path / "pids.txt"
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import os, subprocess, sys, time\n"
        "from dridh.common.watchdog import start_parent_watchdog\n"
        "start_parent_watchdog(parent_pid=os.getppid(), interval=0.2, grace=0.2)\n"
        "worker = subprocess.Popen(['sleep', '300'], start_new_session=True)  # like OpenCode workers\n"
        f"with open({str(pids)!r}, 'w') as handle: handle.write(f'{{os.getpid()}} {{worker.pid}}')\n"
        "time.sleep(300)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c",
                               f"import subprocess, sys, time; subprocess.Popen([sys.executable, {str(bridge)!r}], start_new_session=True); time.sleep(300)"])
    deadline = time.monotonic() + 15
    while not pids.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    bridge_pid, worker_pid = (int(value) for value in pids.read_text().split())
    assert _alive(bridge_pid) and _alive(worker_pid)
    parent.send_signal(signal.SIGKILL)
    parent.wait()
    deadline = time.monotonic() + 15
    while (_alive(bridge_pid) or _alive(worker_pid)) and time.monotonic() < deadline:
        time.sleep(0.1)
    leftovers = [pid for pid in (bridge_pid, worker_pid) if _alive(pid)]
    for pid in leftovers:
        os.kill(pid, signal.SIGKILL)
    assert not leftovers


def test_every_bridge_request_starts_the_parent_watchdog(monkeypatch) -> None:
    import io

    from dridh.common import bridge

    started: list[dict] = []
    monkeypatch.setattr("dridh.common.watchdog.start_parent_watchdog", lambda **kwargs: started.append(kwargs))
    bridge.run_bridge(stdin=io.StringIO('{"not":"a request"}'), stdout=io.StringIO(), stderr=io.StringIO())
    assert started and started[0]["parent_pid"] == os.getppid()
