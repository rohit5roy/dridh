"""Story tests in containers (recovery scenarios 10 and 11).

Each story's test command runs in a fresh container that sees only the story's worktree (mounted at ``/work``), on a
network of its own, with labels that identify the workspace, run and story, CPU and memory limits, and the caller's
user ID so files it writes stay owned by the person. When the project has a test Compose file, its services start as a
Compose project named for the story, and the test container joins that project's network: two stories never share a
project, network, container or volume, so parallel runs cannot collide.

Everything is removed afterwards, whatever happened (success, failure, timeout, cancellation), and anything left by a
crashed run is removed by label the next time the run is driven. Each environment's lifecycle is recorded in the
``docker_test_environment`` table. When Docker is not available, ``auto`` mode runs the tests on the host as before and
says so in the evidence; ``docker`` mode refuses instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dridh.common.redaction import redact
from dridh.executor.policy import per_test_results

LABEL = "io.dridh"
SETUP_FAILED_EXIT = 97  # the container's setup lines failed: the tests never ran
MAX_OUTPUT_CHARS = 20_000
COMPOSE_FILES = (".opencode/dridh/docker/compose.test.yml", "compose.test.yml", "compose.test.yaml",
                 "docker-compose.test.yml", "docker-compose.test.yaml")
# Image and setup by project type when execution.containers.image / setup are not set. Python and Node install the
# project's test dependencies into the container (a virtualenv under /tmp, node_modules in the worktree as usual).
DEFAULT_IMAGES = {"python": "python:3.13-slim", "node": "node:24-slim", "maven": "maven:3-eclipse-temurin-21",
                  "gradle": "gradle:8-jdk21", "go": "golang:1.24"}


def project_kind(command: list[str]) -> str | None:
    head = Path(command[0]).name if command else ""
    if head.startswith("python") or head in ("pytest", "uv"):
        return "python"
    if head in ("npm", "npx", "node", "pnpm", "yarn"):
        return "node"
    if head == "mvn":
        return "maven"
    if head in ("gradle", "gradlew", "./gradlew"):
        return "gradle"
    if head == "go":
        return "go"
    return None


def default_setup(kind: str | None, root: Path, command: list[str]) -> list[str]:
    """Shell lines run in the container before the tests (dependencies the image does not have)."""
    if kind == "python":
        lines = ["python3 -m venv /tmp/venv", ". /tmp/venv/bin/activate"]
        if (root / "requirements-test.txt").is_file():
            lines.append("pip install -q -r requirements-test.txt")
        elif (root / "requirements.txt").is_file():
            lines.append("pip install -q -r requirements.txt")
        if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
            lines.append("pip install -q -e .")
        if "pytest" in command:
            lines.append("pip install -q pytest")
        return lines
    if kind == "node":
        return ["npm ci --no-audit --no-fund" if (root / "package-lock.json").is_file() else "npm install --no-audit --no-fund"]
    return []


def find_compose_file(root: Path, configured: str | None) -> Path | None:
    candidates = (configured,) if configured else COMPOSE_FILES
    for name in candidates:
        if name and (root / name).is_file():
            path = (root / name).resolve()
            if root.resolve() in path.parents:
                return path
    return None


def name_prefix(workspace_id: str) -> str:
    """Every resource of one workspace starts with this, so Compose leftovers (which carry no Dridh labels) are findable."""
    return "dridh-" + re.sub(r"[^a-z0-9]", "", workspace_id.lower())[:8]


def run_tag(run_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", run_id.lower())[:8] or "run"


def environment_names(workspace_id: str, run_id: str, story_id: str) -> dict[str, str]:
    """Deterministic, sanitised, collision-resistant names for one story's test environment."""
    slug = re.sub(r"[^a-z0-9]+", "-", story_id.lower()).strip("-")[:24] or "story"
    digest = hashlib.sha256(f"{workspace_id}:{run_id}:{story_id}".encode()).hexdigest()[:8]
    base = f"{name_prefix(workspace_id)}-{slug}-{run_tag(run_id)}-{digest}"
    return {"id": base, "project": base.replace("-", "_"), "network": f"{base}-net", "container": f"{base}-tests"}


def labels(workspace_id: str, run_id: str, story_id: str) -> dict[str, str]:
    return {LABEL: "1", f"{LABEL}.workspace": workspace_id, f"{LABEL}.run": run_id, f"{LABEL}.story": story_id}


@dataclass
class ContainerPlan:
    image: str
    setup: list[str]
    command: list[str]
    compose_file: Path | None
    memory: str = "2g"
    cpus: float = 2.0
    docker_socket: bool = False
    names: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def script(self) -> str:
        """The shell script the test container runs: setup lines, then exactly the validated test command.

        A failing setup line (dependencies that cannot be installed) exits with SETUP_FAILED_EXIT before the tests
        start, so it is reported as an environment failure, not as failing tests the Executor would try to repair."""
        guarded = [f"{line} || {{ echo {shlex.quote('dridh: setup step failed: ' + line)} >&2; exit {SETUP_FAILED_EXIT}; }}"
                   for line in self.setup]
        return "\n".join(["set -e", *guarded, "exec " + shlex.join(self.command)])


async def _docker(*args: str, timeout: float = 120.0, stdin: str | None = None) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, "DOCKER_CLI_HINTS": "false"})
    try:
        out, _ = await asyncio.wait_for(process.communicate(stdin.encode() if stdin is not None else None), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"docker {args[0]} timed out after {timeout:.0f}s"
    return process.returncode or 0, out.decode("utf-8", "replace")


async def docker_available() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker is not installed"
    code, out = await _docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    return (code == 0, out.strip()[:200] if code == 0 else "the Docker daemon is not reachable")


async def remove_labelled(workspace_id: str, *, run_id: str | None = None, story_id: str | None = None) -> dict[str, int]:
    """Remove containers, networks and volumes carrying this workspace's labels (optionally narrowed to a run/story)."""
    filters = ["--filter", f"label={LABEL}.workspace={workspace_id}"]
    if run_id:
        filters += ["--filter", f"label={LABEL}.run={run_id}"]
    if story_id:
        filters += ["--filter", f"label={LABEL}.story={story_id}"]
    removed = {"containers": 0, "networks": 0, "volumes": 0}
    for kind, list_args, rm_args in (("containers", ["ps", "-aq"], ["rm", "-f", "-v"]), ("networks", ["network", "ls", "-q"], ["network", "rm"]),
                                     ("volumes", ["volume", "ls", "-q"], ["volume", "rm", "-f"])):
        code, out = await _docker(*list_args, *filters, timeout=30)
        ids = [line for line in out.split() if line] if code == 0 else []
        if ids:
            await _docker(*rm_args, *ids, timeout=120)
            removed[kind] = len(ids)
    return removed


async def cleanup_stale(workspace_id: str, *, run_id: str | None = None) -> dict[str, int]:
    """Remove what a crashed or interrupted run left behind: labelled resources and this workspace's Compose projects."""
    removed = await remove_labelled(workspace_id, run_id=run_id)
    prefix = name_prefix(workspace_id).replace("-", "_")
    code, out = await _docker("ps", "-a", "--filter", "label=com.docker.compose.project", "--format",
                              '{{.Label "com.docker.compose.project"}}', timeout=30)
    projects = sorted({p for p in out.split() if p.startswith(prefix) and (run_id is None or f"_{run_tag(run_id)}_" in p)}) if code == 0 else []
    for project in projects:
        await _docker("compose", "-p", project, "down", "-v", "--remove-orphans", "--timeout", "10", timeout=180)
    removed["compose_projects"] = len(projects)
    return removed


async def run_in_container(plan: ContainerPlan, worktree: Path, *, timeout: float) -> dict[str, Any]:
    """Start the environment, run the tests, clean up. Returns test evidence plus a ``containers`` record."""
    names, started, wall_started = plan.names, time.monotonic(), time.time()
    record: dict[str, Any] = {"mode": "docker", "image": plan.image, "network": names["network"], "container": names["container"],
                              "compose_project": names["project"] if plan.compose_file else None,
                              "compose_file": plan.compose_file.name if plan.compose_file else None, "events": []}
    label_args = [x for key, value in plan.labels.items() for x in ("--label", f"{key}={value}")]
    network = names["network"]
    try:
        if plan.compose_file is not None:
            env_args = ["-p", names["project"], "-f", str(plan.compose_file)]
            code, out = await _docker("compose", *env_args, "up", "-d", "--wait", "--quiet-pull", timeout=600)
            record["events"].append({"step": "compose up", "exit_code": code})
            if code != 0:
                record["status"] = "FAILED"
                return _infrastructure_failure(record, plan, f"The Compose services did not start ({plan.compose_file.name}): {out[-600:]}")
            network = f"{names['project']}_default"
            record["network"] = network
        else:
            code, out = await _docker("network", "create", *label_args, network, timeout=60)
            record["events"].append({"step": "network create", "exit_code": code})
            if code != 0:
                record["status"] = "FAILED"
                return _infrastructure_failure(record, plan, f"The story network could not be created: {out[-400:]}")
        code, digest = await _docker("image", "inspect", "--format", "{{index .RepoDigests 0}}", plan.image, timeout=30)
        if code != 0:
            code, out = await _docker("pull", "-q", plan.image, timeout=600)
            record["events"].append({"step": "pull", "exit_code": code})
            if code != 0:
                record["status"] = "FAILED"
                return _infrastructure_failure(record, plan, f"The test image {plan.image} could not be pulled: {out[-400:]}")
            code, digest = await _docker("image", "inspect", "--format", "{{index .RepoDigests 0}}", plan.image, timeout=30)
        record["image_digest"] = digest.strip() if code == 0 else None
        run_args = ["run", "--rm", "-i", "--name", names["container"], *label_args, "--network", network,
                    "--memory", plan.memory, "--cpus", str(plan.cpus), "--pids-limit", "1024",
                    "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "CI=true", "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-v", f"{worktree.resolve()}:/work", "-w", "/work"]
        if plan.docker_socket:  # opt-in: lets Testcontainers in the tests start sibling containers
            run_args += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
        code, out = await _docker(*run_args, plan.image, "sh", "-s", stdin=plan.script(), timeout=timeout)
        timed_out = code == 124 and out.startswith("docker run timed out")
        if timed_out:
            await _docker("rm", "-f", names["container"], timeout=60)
        if code == 125:  # the container itself could not be created or started (scenario 10)
            record["status"] = "FAILED"
            return _infrastructure_failure(record, plan, f"The test container did not start: {out[-600:]}")
        if code == SETUP_FAILED_EXIT and "dridh: setup step failed:" in out:
            record["status"] = "FAILED"
            return _infrastructure_failure(record, plan, "The test environment could not be prepared (a setup step failed, so the tests "
                                           "did not run; check the container's network access or execution.containers.setup): " + out[-600:])
        record["status"] = "PASSED" if code == 0 else "FAILED"
        return {"command": plan.command, "passed": code == 0, "exit_code": None if timed_out else code, "timed_out": timed_out,
                "output": str(redact(out.strip()))[-MAX_OUTPUT_CHARS:], "containers": record,
                "test_results": per_test_results(out, worktree, wall_started)}
    finally:
        if plan.compose_file is not None:
            code, _ = await _docker("compose", "-p", names["project"], "-f", str(plan.compose_file), "down", "-v", "--remove-orphans", "--timeout", "10", timeout=180)
            record["events"].append({"step": "compose down", "exit_code": code})
        removed = await remove_labelled(plan.labels[f"{LABEL}.workspace"], run_id=plan.labels[f"{LABEL}.run"], story_id=plan.labels[f"{LABEL}.story"])
        record["cleanup"] = removed
        record["duration_seconds"] = round(time.monotonic() - started, 1)


def _infrastructure_failure(record: dict[str, Any], plan: ContainerPlan, message: str) -> dict[str, Any]:
    record["infrastructure_failure"] = True
    return {"command": plan.command, "passed": False, "exit_code": None, "timed_out": False,
            "output": str(redact(message))[-MAX_OUTPUT_CHARS:], "containers": record, "infrastructure_failure": True}
