#!/usr/bin/env python3
"""Host runner for the deterministic Docker full-system E2E (Category C, required CI gate).

Steps owned by this runner: E2E-01 (image build), E2E-02 (isolated start), E2E-32
(leftover inspection before safety cleanup), E2E-33 (removal of disposable resources)
and E2E-34 (evidence export and manifest). The in-container scenario owns E2E-03 to
E2E-31.

Exit status is 0 only when all 46 mandatory IDs are present and PASS. A missing
check, a failed check, a failed build or a timeout all return non-zero. The runner
removes only containers, images and networks that carry this run's label or name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANDATORY_IDS = tuple(f"E2E-{number:02d}" for number in range(1, 47))
RUNNER_OWNED = ("E2E-01", "E2E-02", "E2E-32", "E2E-33", "E2E-34")
SCENARIO_TIMEOUT_SECONDS = 2400
BUILD_TIMEOUT_SECONDS = 1800
REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = Path(__file__).resolve().parent / "Dockerfile"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    missing: tuple[str, ...]
    failed: tuple[str, ...]


def evaluate(checks: Iterable[dict[str, Any]]) -> Verdict:
    """Decide the gate. Every mandatory ID must appear exactly once and must be PASS."""

    by_id: dict[str, list[str]] = {}
    for check in checks:
        by_id.setdefault(str(check.get("id")), []).append(str(check.get("status")))
    missing = tuple(identifier for identifier in MANDATORY_IDS if identifier not in by_id)
    failed = tuple(
        identifier
        for identifier in MANDATORY_IDS
        if identifier in by_id and (len(by_id[identifier]) != 1 or by_id[identifier][0] != "PASS")
    )
    return Verdict(passed=not missing and not failed, missing=missing, failed=failed)


def _run(argv: list[str], *, timeout: float, log: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT))
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(completed.stdout + completed.stderr)
    if check and completed.returncode != 0:
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {completed.stderr.strip()[:300]}")
    return completed


def _labelled(kind: str, label: str) -> list[str]:
    if kind == "container":
        command = ["docker", "ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}"]
    elif kind == "network":
        command = ["docker", "network", "ls", "--filter", f"label={label}", "--format", "{{.Name}}"]
    elif kind == "volume":
        command = ["docker", "volume", "ls", "--filter", f"label={label}", "--format", "{{.Name}}"]
    else:
        command = ["docker", "image", "ls", "--filter", f"label={label}", "--format", "{{.Repository}}:{{.Tag}}"]
    completed = _run(command, timeout=120)
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def run_e2e(evidence_root: Path, run_id: str) -> int:
    label = f"dridh.e2e.run={run_id}"
    image = f"dridh-e2e:{run_id}"
    container = f"dridh-e2e-{run_id}"
    evidence = evidence_root / run_id
    evidence.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def record(identifier: str, title: str, passed: bool, detail: Any) -> None:
        checks.append({"id": identifier, "title": title, "status": "PASS" if passed else "FAIL", "detail": str(detail)[:600]})
        print(f"{identifier} {'PASS' if passed else 'FAIL'} {title} :: {str(detail)[:160]}", flush=True)

    # E2E-01: build the disposable image from the repository state.
    try:
        build = _run(
            ["docker", "build", "--progress=plain", "--label", label, "-f", str(DOCKERFILE), "-t", image, "."],
            timeout=BUILD_TIMEOUT_SECONDS,
            log=evidence / "docker-build.log",
        )
        record("E2E-01", "Disposable image builds from the repository with pinned runtimes", build.returncode == 0, f"exit {build.returncode}")
    except subprocess.TimeoutExpired:
        record("E2E-01", "Disposable image builds from the repository with pinned runtimes", False, "build timed out")

    scenario_exit: int | None = None
    if checks[-1]["status"] == "PASS":
        # E2E-02: start the isolated container: no network, no mounts, unprivileged user, labelled.
        try:
            started = _run(
                [
                    "docker", "run", "--name", container, "--label", label,
                    "--network", "none", "--memory", "4g", "--pids-limit", "1024",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    "--env", f"E2E_RUN_ID={run_id}",
                    image,
                ],
                timeout=SCENARIO_TIMEOUT_SECONDS,
                log=evidence / "container-run.log",
            )
            scenario_exit = started.returncode
        except subprocess.TimeoutExpired:
            _run(["docker", "kill", container], timeout=120)
            scenario_exit = -1
        inspect = _run(["docker", "inspect", container], timeout=120)
        (evidence / "container-inspect.json").write_text(inspect.stdout)
        details = json.loads(inspect.stdout or "[{}]")[0] if inspect.returncode == 0 else {}
        host_config = details.get("HostConfig", {})
        mounts = details.get("Mounts", [])
        user = details.get("Config", {}).get("User")
        isolated = host_config.get("NetworkMode") == "none" and not mounts and user == "e2e" and label.split("=")[0] in details.get("Config", {}).get("Labels", {})
        record(
            "E2E-02",
            "Isolated environment starts with no network, no host mounts, a non-root user and run labels",
            bool(isolated) and inspect.returncode == 0,
            f"network={host_config.get('NetworkMode')} mounts={len(mounts)} user={user}",
        )

    # Collect the in-container evidence, then the scenario verdict.
    scenario_summary: dict[str, Any] = {}
    export_dir = evidence / "scenario"
    if (scenario_exit is not None):
        copied = _run(["docker", "cp", f"{container}:/work/evidence", str(export_dir)], timeout=300)
        _run(["docker", "logs", container], timeout=300, log=evidence / "container.log")
        summary_path = export_dir / "scenario-summary.json"
        if copied.returncode == 0 and summary_path.exists():
            scenario_summary = json.loads(summary_path.read_text())
    if scenario_summary:
        checks.extend(item for item in scenario_summary.get("checks", []) if item.get("id") not in RUNNER_OWNED)

    # E2E-32: inspect disposable resources before any safety cleanup.
    named_container = _run(["docker", "container", "inspect", container], timeout=60).returncode == 0
    named_image = _run(["docker", "image", "inspect", image], timeout=60).returncode == 0
    leftovers_before = {
        "containers": list(dict.fromkeys(_labelled("container", label) + ([container] if named_container else []))),
        "networks": _labelled("network", label),
        "volumes": _labelled("volume", label),
        "images": list(dict.fromkeys(_labelled("image", label) + ([image] if named_image else []))),
    }
    (evidence / "leftovers-before-cleanup.json").write_text(json.dumps(leftovers_before, indent=2) + "\n")
    record(
        "E2E-32",
        "Container, network and volume state inspected before safety cleanup",
        True,
        {key: len(value) for key, value in leftovers_before.items()},
    )

    # E2E-33: remove only this run's resources, then confirm none remain.
    for name in dict.fromkeys(leftovers_before["containers"]):
        _run(["docker", "rm", "-f", name], timeout=180)
    for name in dict.fromkeys(leftovers_before["networks"]):
        _run(["docker", "network", "rm", name], timeout=180)
    for name in dict.fromkeys(leftovers_before["volumes"]):
        _run(["docker", "volume", "rm", name], timeout=180)
    for name in dict.fromkeys(leftovers_before["images"]):
        _run(["docker", "image", "rm", "-f", name], timeout=300)
    remaining = {
        "containers": _labelled("container", label) + ([container] if _run(["docker", "container", "inspect", container], timeout=60).returncode == 0 else []),
        "networks": _labelled("network", label),
        "volumes": _labelled("volume", label),
        "images": _labelled("image", label) + ([image] if _run(["docker", "image", "inspect", image], timeout=60).returncode == 0 else []),
    }
    clean = all(not value for value in remaining.values())
    record("E2E-33", "Disposable containers, networks, volumes and images are removed", clean, remaining)

    # E2E-34: evidence is exported and checksummed.
    verdict = evaluate(checks)
    summary = {
        "schema": "dridh.e2e-summary",
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "verdict": "PASS" if verdict.passed else "FAIL",
        "mandatory_ids": list(MANDATORY_IDS),
        "missing": list(verdict.missing),
        "failed": list(verdict.failed),
        "checks": sorted(checks, key=lambda item: item["id"]),
        "image": image,
        "container": container,
        "scenario_exit_code": scenario_exit,
    }
    (evidence / "e2e-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    files = sorted(
        path for path in evidence.rglob("*") if path.is_file() and path.name not in ("manifest.json", "e2e-summary.json")
    )
    manifest = {
        "schema": "dridh.e2e-manifest",
        "schema_version": 1,
        "run_id": run_id,
        "files": [{"path": path.relative_to(evidence).as_posix(), "sha256": _sha256(path), "bytes": path.stat().st_size} for path in files],
    }
    (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    record("E2E-34", "Test evidence is exported with a SHA-256 manifest", (evidence / "manifest.json").is_file() and len(files) > 0, f"{len(files)} files")

    summary["checks"] = sorted(checks, key=lambda item: item["id"])
    final_verdict = evaluate(checks)
    summary["verdict"] = "PASS" if final_verdict.passed else "FAIL"
    summary["missing"] = list(final_verdict.missing)
    summary["failed"] = list(final_verdict.failed)
    (evidence / "e2e-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(f"verdict {summary['verdict']} missing={len(final_verdict.missing)} failed={len(final_verdict.failed)} evidence={evidence}")
    return 0 if final_verdict.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "dridh" / "e2e-evidence")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    run_id = args.run_id or f"e2e-{uuid.uuid4().hex[:10]}"
    if not run_id.replace("-", "").isalnum():
        print("run id must be alphanumeric with dashes", file=sys.stderr)
        return 2
    try:
        _run(["docker", "info"], timeout=120, check=True)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Docker is required for the E2E gate: {exc}", file=sys.stderr)
        return 2
    return run_e2e(args.evidence_dir.resolve(), run_id)


if __name__ == "__main__":
    sys.exit(main())
