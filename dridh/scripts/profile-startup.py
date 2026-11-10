#!/usr/bin/env python3
"""Measure lightweight CLI/bridge startup and prove heavy imports stay lazy.

The script runs every command in a fresh interpreter, so each measurement is a
real one-shot process. "Cold" is the first invocation of a command; "warm" is
the median of the remaining invocations. It writes one JSON report to stdout so
the result can be archived as evidence without a second formatting step.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HEAVY_MODULES = ("fastembed", "onnxruntime", "tree_sitter", "sqlite_vec")


def _run(command: list[str], *, stdin: str | None = None, env: dict[str, str] | None = None) -> dict:
    started = time.perf_counter()
    result = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return {
        "command": command[1:],
        "returncode": result.returncode,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _measure(command: list[str], *, stdin: str | None = None, env: dict[str, str] | None = None) -> dict:
    runs = [_run(command, stdin=stdin, env=env) for _ in range(3)]
    durations = [run["duration_seconds"] for run in runs]
    return {
        "command": command[1:],
        "cold_seconds": durations[0],
        "warm_median_seconds": round(statistics.median(durations[1:]), 6),
        "returncodes": [run["returncode"] for run in runs],
        "stdout_lines": [len(run["stdout"].splitlines()) for run in runs],
        "stderr_lines": [len(run["stderr"].splitlines()) for run in runs],
    }


def main() -> int:
    executable = sys.executable
    workspace = Path(tempfile.mkdtemp(prefix="dridh-profile-"))
    request = json.dumps(
        {
            "protocol_version": 1,
            "request_id": "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01",
            "workspace_root": str(workspace),
            "operation": "runtime.ping",
            "payload": {},
        }
    )
    info_request = json.loads(request)
    info_request["operation"] = "runtime.info"
    info_request["request_id"] = "a8f8f66d-6a09-4f39-b074-0f6d9d3a94b2"

    environment = dict(os.environ)
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"

    import_check = _run(
        [
            executable,
            "-c",
            (
                "import json, sys; "
                "import dridh, dridh.cli.main, dridh.common.bridge, dridh.common.protocol; "
                f"print(json.dumps(sorted(m for m in {HEAVY_MODULES!r} if m in sys.modules)))"
            ),
        ],
        env=environment,
    )
    loaded_heavy_modules = json.loads(import_check["stdout"])

    report = {
        "python_executable": executable,
        "python_version": sys.version.split()[0],
        "workspace": str(workspace),
        "offline_environment": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        "heavy_modules_probed": list(HEAVY_MODULES),
        "heavy_modules_loaded_by_lightweight_import": loaded_heavy_modules,
        "import_check_returncode": import_check["returncode"],
        "measurements": {
            "help": _measure([executable, "-m", "dridh", "--help"], env=environment),
            "version": _measure([executable, "-m", "dridh", "--version"], env=environment),
            "bridge_ping": _measure([executable, "-m", "dridh", "bridge"], stdin=request, env=environment),
            "bridge_info": _measure(
                [executable, "-m", "dridh", "bridge"],
                stdin=json.dumps(info_request),
                env=environment,
            ),
        },
        "bridge_ping_stdout": _run(
            [executable, "-m", "dridh", "bridge"], stdin=request, env=environment
        )["stdout"].strip(),
        "bridge_ping_stderr": [
            json.loads(line)
            for line in _run(
                [executable, "-m", "dridh", "bridge"], stdin=request, env=environment
            )["stderr"].splitlines()
            if line.strip()
        ],
    }

    if loaded_heavy_modules:
        report["result"] = "FAIL"
        print(json.dumps(report, indent=2))
        return 1
    if any(code != 0 for code in report["measurements"]["help"]["returncodes"]):
        report["result"] = "FAIL"
        print(json.dumps(report, indent=2))
        return 1
    report["result"] = "PASS"
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
