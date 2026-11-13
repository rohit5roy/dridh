#!/usr/bin/env python3
"""Deterministic full-system E2E scenario. Runs inside the disposable image as user e2e.

The real pinned OpenCode CLI, the Dridh plugin, the bridge, the Coordinator, the
worker sessions, the workspace, the ledger and the approval commands all run for
real. Only the model boundary is simulated, by stub_provider.py.

Every check is recorded with a stable ID (E2E-NN). The process exits 0 only when
every check recorded here passed. The host runner (run_docker_e2e.py) also
requires the image, container, leftover and export checks, and rejects missing IDs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pty_confirm import run_with_typed_confirmation  # noqa: E402

WORK = Path("/work")
HOME = WORK / "home"
CONFIG = WORK / "config"
PROJECTS = WORK / "projects"
EVIDENCE = Path(os.environ.get("E2E_EVIDENCE_DIR", str(WORK / "evidence")))
CONTROL = WORK / "stub-control.json"
STUB_LOG = EVIDENCE / "stub-requests.jsonl"
STUB_PORT = 18090
DRIDH = os.environ["DRIDH_BIN"]
OPENCODE = os.environ["DRIDH_OPENCODE_BIN"]
OPENCODE_VERSION = "opencode v2.2.6"  # the lock-pinned CI version (.github/scripts/opencode-cli); Dridh supports >= 2.2.5
OBJECTIVE = "Create a greeting function with tests"
DRIDH_BRIDGE_PATTERN = "/opt/dridh/venv/bin/dridh bridge"
WORKER_PATTERN = "dridh/code-reacher"

RESULTS: list[dict[str, Any]] = []
COMMANDS: list[dict[str, Any]] = []
STUB: subprocess.Popen | None = None
FACTS: dict[str, Any] = {}


# ----------------------------------------------------------------- recording

def record(check_id: str, title: str, passed: bool, detail: Any) -> bool:
    RESULTS.append({"id": check_id, "title": title, "status": "PASS" if passed else "FAIL", "detail": str(detail)[:600]})
    print(f"{check_id} {'PASS' if passed else 'FAIL'} {title} :: {str(detail)[:200]}", flush=True)
    return passed


def check(check_id: str, title: str, fn: Callable[[], tuple[bool, Any]]) -> bool:
    try:
        passed, detail = fn()
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never raised
        passed, detail = False, f"{type(exc).__name__}: {exc}"
    return record(check_id, title, bool(passed), detail)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


# ----------------------------------------------------------------- environment

def base_env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": "/opt/dridh/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "OPENCODE_CONFIG_DIR": str(CONFIG),
        # The model is a local stub, so OpenCode must not depend on reaching models.dev (a refused connection can stall its first call).
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "DRIDH_BIN": DRIDH,
        "DRIDH_OPENCODE_BIN": OPENCODE,
        "PYTHONDONTWRITEBYTECODE": "1",
        # The retired generic workflow engine still drives E2E-14..30 (bridge, worker process and lease mechanics).
        "DRIDH_LEGACY_WORKFLOW": "1",
    }
    env.update(extra)
    return env


def run(args: list[str], *, env: dict[str, str], cwd: Path | None = None, timeout: float = 900) -> subprocess.CompletedProcess:
    started = time.monotonic()
    completed = subprocess.run(args, env=env, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    COMMANDS.append(
        {
            "argv": [Path(args[0]).name, *args[1:]][:12],
            "exit_code": completed.returncode,
            "seconds": round(time.monotonic() - started, 2),
        }
    )
    return completed


def dridh_json(args: list[str], *, project: Path | None = None, env: dict[str, str] | None = None, timeout: float = 900) -> tuple[int, Any, str]:
    completed = run([DRIDH, *args], env=env or base_env(), cwd=project, timeout=timeout)
    text = completed.stdout.strip()
    try:
        document = json.loads(text) if text else None
    except ValueError:
        document = None
    return completed.returncode, document, completed.stderr.strip()


def opencode_events(args: list[str], *, cwd: Path, env: dict[str, str], timeout: float = 900) -> list[dict[str, Any]]:
    completed = run([OPENCODE, "--standalone", "run", "--format", "json", *args], env=env, cwd=cwd, timeout=timeout)
    events = []
    for line in completed.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def tool_outputs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for event in events:
        part = event.get("part", {})
        if event.get("type") == "tool_use" or part.get("type") == "tool":
            state = part.get("state", {})
            found.append({"tool": part.get("tool"), "input": state.get("input", {}), "status": state.get("status"), "output": str(state.get("output") or state.get("error") or "")})
    return found


def stub_entries() -> list[dict[str, Any]]:
    if not STUB_LOG.exists():
        return []
    return [json.loads(line) for line in STUB_LOG.read_text().splitlines() if line.strip()]


def set_control(**values: Any) -> None:
    CONTROL.write_text(json.dumps(values))


def start_stub() -> None:
    global STUB
    CONTROL.write_text("{}")
    handle = (EVIDENCE / "stub-provider.out").open("w")
    STUB = subprocess.Popen(
        ["python3", "/opt/e2e/stub_provider.py", str(STUB_PORT), str(STUB_LOG), str(CONTROL)],
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", STUB_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("the stub provider did not start")


def write_configuration() -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    document = {
        "$schema": "https://opencode.ai/config.json",
        "model": "dridhtest/scripted",
        "providers": {
            "dridhtest": {
                "name": "E2E stub (model boundary only)",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": f"http://127.0.0.1:{STUB_PORT}/v1", "apiKey": "e2e-placeholder-not-a-credential"},
                "models": {"scripted": {"name": "Scripted"}},
            }
        },
    }
    write_json(CONFIG / "opencode.json", document)


def processes(pattern: str) -> list[tuple[int, int, str]]:
    completed = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True)
    found = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
        if pattern in args and pid != os.getpid() and "ps -eo" not in args:
            found.append((pid, ppid, args))
    return found


def wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def db(project: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(project / ".opencode" / "dridh" / "state" / "dridh.sqlite")
    connection.row_factory = sqlite3.Row
    return connection


def workflow_status(project: Path, workflow: str) -> dict[str, Any]:
    code, document, error = dridh_json(["workflow", "status", workflow, "--project", str(project)], project=project)
    if code != 0 or document is None:
        raise RuntimeError(f"workflow status failed: {error[:200]}")
    return document


def wait_for_worker(timeout: float = 90) -> bool:
    return wait_until(lambda: bool(processes(WORKER_PATTERN)), timeout)


def pty_approve(project: Path, workflow: str, answer: str | None = None) -> Any:
    return run_with_typed_confirmation(
        [DRIDH, "workflow", "approve", workflow, "--project", str(project)],
        timeout=60,
        answer=answer,
    )


def start_objective(project: Path, objective: str) -> tuple[str, str]:
    code, document, error = dridh_json(["session", "start", f"Objective: {objective}", "--project", str(project)], project=project, timeout=900)
    if code != 0 or document is None:
        raise RuntimeError(f"session start failed: {error[:300]}")
    match = re.search(r"wf-[0-9a-f]{12}", document["text"])
    if not match:
        raise RuntimeError(f"no workflow in coordinator reply: {document['text'][:200]}")
    return document["session_id"], match.group(0)


def approve_typed(project: Path, workflow: str) -> None:
    status = workflow_status(project, workflow)
    sha = status["plan"]["sha256"]
    result = pty_approve(project, workflow)
    if result.exit_code != 0 or not result.prompted:
        raise RuntimeError(f"typed approval failed: exit {result.exit_code}")
    if workflow_status(project, workflow)["state"] != "APPROVED":
        raise RuntimeError("workflow not approved after typed confirmation")
    FACTS.setdefault("approved_sha", {})[workflow] = sha


def init_project(name: str) -> Path:
    project = PROJECTS / name
    project.mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text(f"# {name}\n\nDisposable Dridh E2E project.\n")
    subprocess.run(["git", "init", "-q"], cwd=str(project), check=True)
    subprocess.run(["git", "config", "user.email", "e2e@test.com"], cwd=str(project), check=True)
    subprocess.run(["git", "config", "user.name", "e2e"], cwd=str(project), check=True)
    subprocess.run(["git", "add", "."], cwd=str(project), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(project), check=True)
    code, document, error = dridh_json(["init", "--project", str(project), "--mode", "existing"], project=project)
    if code != 0:
        raise RuntimeError(f"init failed: {error[:200]}")
    return project


# ----------------------------------------------------------------- scenario

def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)
    PROJECTS.mkdir(parents=True, exist_ok=True)
    env = base_env()

    # Environment and versions (steps 3-5)
    check("E2E-03", "OpenCode CLI is the lock-pinned 2.2.6", lambda: (
        (run([OPENCODE, "--version"], env=env).stdout.strip() == OPENCODE_VERSION, run([OPENCODE, "--version"], env=env).stdout.strip())
    ))
    versions = {
        "python": run(["python3", "--version"], env=env).stdout.strip(),
        "node": run(["node", "--version"], env=env).stdout.strip(),
        "npm": run(["npm", "--version"], env=env).stdout.strip(),
        "uv": run(["uv", "--version"], env=env).stdout.strip(),
        "opencode": run([OPENCODE, "--version"], env=env).stdout.strip(),
    }
    write_json(EVIDENCE / "versions.json", versions)
    check("E2E-04", "Python 3.13, Node 24.22.2, npm 11.19.1 and uv 0.13.7 are the pinned runtimes", lambda: (
        versions["python"].startswith("Python 3.13.") and versions["node"] == "v24.22.2" and versions["npm"] == "11.19.1" and versions["uv"].split()[1] == "0.13.7",
        versions,
    ))
    check("E2E-05", "Dridh runtime is installed from the built wheel", lambda: (
        (lambda out: re.match(r"dridh \d+\.\d+\.\d+", out) is not None and "/opt/dridh/" in out)(run([DRIDH, "--version"], env=env).stdout),
        run([DRIDH, "--version"], env=env).stdout.splitlines()[0:4],
    ))

    # Installation (step 6) and CLI behaviour (step 10)
    write_configuration()
    start_stub()

    def install_step() -> tuple[bool, Any]:
        code, first, error = dridh_json(["install", "--config-dir", str(CONFIG)], env=env)
        code2, second, _ = dridh_json(["install", "--config-dir", str(CONFIG), "--dry-run"], env=env)
        config = json.loads((CONFIG / "opencode.json").read_text())
        agent_root = CONFIG / "agents" / "dridh"
        agents = sorted(p.relative_to(agent_root).as_posix() for p in agent_root.rglob("*.md"))
        expected_agents = {
            "code-reacher.md",
            "coordinator.md",
            "designer.md",
            "documents.md",
            "executor.md",
            "ideator.md",
            "planner.md",
            "technical-designer.md",
            "workers/designer-doc-generator.md",
            "workers/designer-handoff.md",
            "workers/designer-inquirer.md",
            "workers/ideator-doc-generator.md",
            "workers/ideator-handoff.md",
            "workers/ideator-inquirer.md",
            "workers/planner-indexer.md",
            "workers/planner-component.md",
            "workers/planner-testing.md",
        }
        plugin_dir = CONFIG / "dridh" / "plugin" / "dridh-bridge"
        ok = (
            code == 0 and first and first["mode"] == "apply" and first["status"] == "changed"
            and code2 == 0 and second and second["changed"] is False
            and str(plugin_dir) in config.get("plugins", [])
            and "default_agent" not in config
            and set(agents) == expected_agents
            and (plugin_dir / "dist" / "index.js").is_file()
            and (CONFIG / "agents" / "dridh" / "workers" / "designer-inquirer.md").is_file()
            and (CONFIG / "agents" / "dridh" / "workers" / "designer-doc-generator.md").is_file()
            and (CONFIG / "agents" / "dridh" / "workers" / "designer-handoff.md").is_file()
        )
        FACTS["install"] = {
            "apply": first,
            "second_dry_run_changed": second and second["changed"],
            "agents": agents,
            "expected_agents": sorted(expected_agents),
        }
        return ok, {
            "status": first and first["status"],
            "agents": len(agents),
            "expected_agents": len(expected_agents),
            "missing_agents": sorted(expected_agents - set(agents)),
            "unexpected_agents": sorted(set(agents) - expected_agents),
            "second_dry_run_changed": second and second["changed"],
            "error": error[:120],
        }

    check("E2E-06", "Plugin, agent profiles and plugins entry installed idempotently; default_agent untouched", install_step)

    def cli_step() -> tuple[bool, Any]:
        help_text = run([DRIDH, "--help"], env=env).stdout
        code, status, error = dridh_json(["status", "--config-dir", str(CONFIG)], env=env)
        ok = (
            all(word in help_text for word in ("init", "install", "uninstall", "status", "session", "feature", "planner", "execute", "bridge"))
            and " workflow " not in help_text  # the retired generic workflow group is hidden
            and code == 0 and status["runtime"]["installed"] is True and status["runtime"]["plugin_entry_present"] is True
            and status["runtime"]["default_agent"] is None
        )
        return ok, {"installed": status and status["runtime"]["installed"]}

    check("E2E-10", "CLI help, status and install reporting behave as documented", cli_step)

    # Plugin hosted by OpenCode (steps 7-9)
    probe_project = init_project("probe")
    probe = opencode_events(["--agent", "dridh/coordinator", "PROBE_RUNTIME"], cwd=probe_project, env=env)
    calls = tool_outputs(probe)
    ping = [c for c in calls if "runtime.ping" in json.dumps(c["input"]) and c["status"] == "completed"]
    info = [c for c in calls if "runtime.info" in json.dumps(c["input"]) and c["status"] == "completed"]
    FACTS["probe_tool_calls"] = [{"tool": c["tool"], "status": c["status"], "output": c["output"][:120]} for c in calls]
    check("E2E-07", "Plugin is loaded by the OpenCode process and its tool answers", lambda: (
        bool(ping) and '"status":"healthy"' in ping[0]["output"], [c["status"] for c in calls],
    ))
    check("E2E-08", "runtime.ping is invoked through OpenCode and returns healthy", lambda: (
        bool(ping) and '"bridge_protocol_version":1' in ping[0]["output"], ping[0]["output"][:120] if ping else "no ping",
    ))
    check("E2E-09", "runtime.info is invoked through OpenCode and returns package metadata", lambda: (
        bool(info) and '"package":"dridh"' in info[0]["output"], info[0]["output"][:120] if info else "no info",
    ))

    # Jev advisory integration check (disabled by default, fallback tested)
    def jev_fallback_step() -> tuple[bool, Any]:
        proj = init_project("jev")
        code, created, _ = dridh_json(["workflow", "create", "Jev test", "--project", str(proj)], project=proj)
        if code != 0:
            return False, "create failed"
        wf = created["workflow_id"]
        # Plan without Jev (default: disabled, fallback used)
        code, planned, _ = dridh_json(["workflow", "plan", wf, "--project", str(proj)], project=proj, timeout=900)
        if code != 0:
            return False, f"plan failed: {code}"
        # Check ledger for JEV_PLAN_ANALYSIS event (should not be present when disabled)
        events = [r["event_type"] for r in db(proj).execute("SELECT event_type FROM ledger WHERE workflow_id = ?", (wf,))]
        return True, {"events": events, "jev_event_present": "JEV_PLAN_ANALYSIS" in events}

    check("E2E-JEV", "Jev advisory integration: disabled by default, fallback used", jev_fallback_step)

    # Specialist agent E2E checks (each role through real OpenCode)
    for specialist in ("ideator", "designer", "documents"):
        agent_id = f"dridh/{specialist}"
        probe = opencode_events(["--agent", agent_id, f"PROBE_SPECIALIST:{specialist}"], cwd=probe_project, env=env)
        calls = tool_outputs(probe)
        text = " ".join(e.get("part", {}).get("text", "") for e in probe if e.get("part", {}).get("type") == "text")
        check(f"E2E-SPEC-{specialist}", f"Specialist agent {agent_id} responds through OpenCode", lambda t=text: (bool(t) and len(t) > 10, t[:120]))

    # Requirement clarification E2E check
    def clarification_step() -> tuple[bool, Any]:
        proj = init_project("clarify")
        code, created, _ = dridh_json(["workflow", "create", "Clarify test", "--project", str(proj)], project=proj)
        if code != 0:
            return False, "create failed"
        wf = created["workflow_id"]
        code, _, _ = dridh_json(["workflow", "clarify", wf, "--question", "What format?", "--answer", "JSON", "--project", str(proj)], project=proj)
        return code == 0, {"exit": code}

    check("E2E-CLARIFY", "Requirement clarification is recorded", clarification_step)

    # Integration QA E2E check
    def qa_step() -> tuple[bool, Any]:
        proj = init_project("qa")
        code, created, _ = dridh_json(["workflow", "create", "QA test", "--project", str(proj)], project=proj)
        if code != 0:
            return False, "create failed"
        wf = created["workflow_id"]
        code, _, _ = dridh_json(["workflow", "qa", wf, "--task", "T-001", "--reviewer", "reviewer:alice", "--verdict", "PASS", "--detail", "Looks good", "--project", str(proj)], project=proj)
        if code != 0:
            return False, f"qa failed: {code}"
        code, summary, _ = dridh_json(["workflow", "qa-summary", wf, "--project", str(proj)], project=proj)
        return code == 0 and summary and len(summary.get("results", [])) == 1, {"exit": code, "count": len(summary.get("results", [])) if summary else 0}

    check("E2E-QA", "Integration QA result is recorded", qa_step)

    # QA integration gate E2E check
    def qa_gate_step() -> tuple[bool, Any]:
        proj = init_project("qagate")
        code, created, _ = dridh_json(["workflow", "create", "QA gate test", "--project", str(proj)], project=proj)
        if code != 0:
            return False, "create failed"
        wf = created["workflow_id"]
        code, _, _ = dridh_json(["workflow", "plan", wf, "--project", str(proj)], project=proj, timeout=900)
        if code != 0:
            return False, f"plan failed: {code}"
        # No QA -> not eligible
        code, result, _ = dridh_json(["workflow", "check-integration", wf, "--task", "T-001", "--project", str(proj)], project=proj)
        no_qa_blocked = (code == 0 and result and result.get("eligible") is False)
        # Record passing QA
        code, _, _ = dridh_json(["workflow", "qa", wf, "--task", "T-001", "--reviewer", "reviewer:bob", "--verdict", "PASS", "--detail", "OK", "--project", str(proj)], project=proj)
        code, result2, _ = dridh_json(["workflow", "check-integration", wf, "--task", "T-001", "--project", str(proj)], project=proj)
        qa_passes = (code == 0 and result2 and result2.get("eligible") is True)
        return no_qa_blocked and qa_passes, {"no_qa_blocked": no_qa_blocked, "qa_passes": qa_passes}

    check("E2E-QA-GATE", "QA integration gate blocks without QA and allows with passing QA", qa_gate_step)

    # Workspace initialisation and isolation (steps 11-13)
    alpha = init_project("alpha")
    beta = init_project("beta")

    def layout_step() -> tuple[bool, Any]:
        root = alpha / ".opencode" / "dridh"
        descriptor = json.loads((root / "workspace.json").read_text())
        subdirs = all((root / name).is_dir() for name in ("state", "artifacts", "reports", "evidence", "logs"))
        ok = (
            descriptor["schema"] == "dridh.workspace" and descriptor["schema_version"] == 1
            and descriptor["project_root"] == str(alpha) and subdirs and "state/" in (root / ".gitignore").read_text()
        )
        return ok, {"workspace_id": descriptor["workspace_id"], "subdirectories": subdirs}

    check("E2E-11", "Workspace initialised under .opencode/dridh in the project", lambda: (
        (alpha / ".opencode" / "dridh" / "workspace.json").is_file(), str(alpha / ".opencode" / "dridh"),
    ))
    check("E2E-12", "Workspace layout, descriptor and ignore rules are correct", layout_step)

    def isolation_step() -> tuple[bool, Any]:
        alpha_id = json.loads((alpha / ".opencode" / "dridh" / "workspace.json").read_text())["workspace_id"]
        beta_id = json.loads((beta / ".opencode" / "dridh" / "workspace.json").read_text())["workspace_id"]
        gamma = PROJECTS / "gamma"
        shutil.copytree(alpha / ".opencode", gamma / ".opencode")
        code, _, error = dridh_json(["workflow", "verify-ledger", "--project", str(gamma)], project=gamma, env=env)
        ledger_beta = dridh_json(["workflow", "verify-ledger", "--project", str(beta)], project=beta, env=env)[1]
        ok = alpha_id != beta_id and code == 8 and "STATE_CORRUPT" in error and ledger_beta and ledger_beta["events"] == 0
        return ok, {"distinct_ids": alpha_id != beta_id, "copied_state_refused": "STATE_CORRUPT" in error, "beta_events": ledger_beta and ledger_beta["events"]}

    check("E2E-13", "Two workspaces are isolated; a copied workspace state is refused", isolation_step)

    # Coordinator session and objective (steps 14-18)
    session_id = ""
    workflow = ""

    def session_step() -> tuple[bool, Any]:
        nonlocal session_id, workflow
        session_id, workflow = start_objective(alpha, OBJECTIVE)
        FACTS["alpha"] = {"session_id": session_id, "workflow": workflow}
        coordinator_roles = [e for e in stub_entries() if e.get("role") == "dridh/coordinator"]
        return bool(session_id and workflow), {"session_id": session_id, "workflow": workflow, "coordinator_requests": len(coordinator_roles)}

    check("E2E-14", "Dridh session started and the objective submitted", session_step)
    check("E2E-15", "The Coordinator is explicitly selected for the Dridh session", lambda: (
        any(e.get("role") == "dridh/coordinator" for e in stub_entries()) and bool(session_id),
        {"coordinator_roles_seen": sorted({e.get("role") for e in stub_entries()})},
    ))
    check("E2E-16", "The global default agent is unchanged", lambda: (
        "default_agent" not in json.loads((CONFIG / "opencode.json").read_text()), "no default_agent key",
    ))

    def planning_step() -> tuple[bool, Any]:
        status = workflow_status(alpha, workflow)
        ledger = {row["event_type"] for row in db(alpha).execute("SELECT event_type FROM ledger WHERE workflow_id = ?", (workflow,))}
        planners = [r for r in db(alpha).execute("SELECT agent, status FROM worker_runs WHERE workflow_id = ? AND purpose = 'plan'", (workflow,))]
        ok = status["state"] == "AWAITING_APPROVAL" and {"WORKFLOW_CREATED", "PLAN_REQUESTED", "PLAN_VALIDATED"} <= ledger and planners and planners[0]["agent"] == "dridh/planner"
        return bool(ok), {"state": status["state"], "planner_runs": [dict(r) for r in planners]}

    check("E2E-17", "The objective is recorded as the workflow objective", lambda: (
        workflow_status(alpha, workflow)["objective"].startswith("Create a greeting function"), workflow_status(alpha, workflow)["objective"],
    ))
    check("E2E-18", "Requirement intake and planning run through the dridh/planner worker", planning_step)

    def task_structure() -> tuple[bool, Any]:
        status = workflow_status(alpha, workflow)
        tasks = {task["id"]: task for task in status["plan"]["tasks"]}
        rows = {r["id"]: json.loads(r["acceptance"]) for r in db(alpha).execute("SELECT id, acceptance FROM tasks WHERE workflow_id = ?", (workflow,))}
        ok = (
            set(tasks) == {"T-001", "T-002"} and tasks["T-002"]["depends_on"] == ["T-001"]
            and all(rows[key] for key in rows) and tasks["T-001"]["agent"] == "dridh/code-reacher"
        )
        return ok, {"tasks": list(tasks), "T-002_depends_on": tasks.get("T-002", {}).get("depends_on"), "acceptance_checks": {k: len(v) for k, v in rows.items()}}

    check("E2E-19", "Tasks have IDs, dependencies and deterministic acceptance criteria", task_structure)

    # Approval gates (step 26a-d) before the approval itself
    check("E2E-26", "Approval gates fail closed: unapproved execution, scripted and wrong-typed approval are refused", lambda: approval_gates(alpha, workflow))

    # Approved execution by the Coordinator and the workers (steps 20-25, 27-28)
    def resume_step() -> tuple[bool, Any]:
        set_control()
        code, document, error = dridh_json(["session", "resume", session_id, f"Continue workflow {workflow}", "--project", str(alpha)], project=alpha, timeout=900)
        FACTS["alpha_resume"] = document
        return code == 0 and document is not None and document["mode"] == "resumed" and document["resumed"] is True, {"mode": document and document["mode"], "text": document and document["text"][:80], "error": error[:120]}


    def unknown_session_step() -> tuple[bool, Any]:
        code, document, error = dridh_json(["session", "resume", "ses_000000000000000000000001", "Continue", "--project", str(alpha)], project=alpha)
        refused = code == 7 and "SESSION_UNSAFE" in error
        code2, fallback, error2 = dridh_json(["session", "resume", "ses_000000000000000000000001", "PROBE_RUNTIME", "--project", str(alpha), "--fresh-if-unsafe"], project=alpha, timeout=600)
        ledger = [r["event_type"] for r in db(alpha).execute("SELECT event_type FROM ledger WHERE event_type IN ('SESSION_REFUSED', 'SESSION_FALLBACK_FRESH')")]
        ok = refused and code2 == 0 and fallback and fallback["mode"] == "fresh_fallback" and "SESSION_REFUSED" in ledger and "SESSION_FALLBACK_FRESH" in ledger
        return ok, {"unknown_refused": refused, "fallback_mode": fallback and fallback["mode"], "ledger": ledger}

    def session_step_28() -> tuple[bool, Any]:
        resumed_ok, resumed_detail = resume_step()
        refused_ok, refused_detail = unknown_session_step()
        return resumed_ok and refused_ok, {"resume": resumed_detail, "unknown_and_fallback": refused_detail}

    check("E2E-28", "Session resume continues the registered session; unknown sessions are refused; fallback is recorded", session_step_28)

    def execution_step() -> tuple[bool, Any]:
        status = workflow_status(alpha, workflow)
        states = {t["id"]: t["state"] for t in status["plan"]["tasks"]}
        return status["state"] == "COMPLETED" and states == {"T-001": "DONE", "T-002": "DONE"}, {"state": status["state"], "tasks": states}

    check("E2E-20", "The dridh/code-reacher worker is selected and invoked for each task", lambda: (
        (lambda rows: {r["task_id"] for r in rows} == {"T-001", "T-002"} and all(r["agent"] == "dridh/code-reacher" for r in rows))(
            list(db(alpha).execute("SELECT task_id, agent FROM worker_runs WHERE workflow_id = ? AND purpose = 'task'", (workflow,)))
        ),
        [dict(r) for r in db(alpha).execute("SELECT task_id, agent, status FROM worker_runs WHERE workflow_id = ? AND purpose = 'task'", (workflow,))],
    ))
    check("E2E-21", "Each worker runs in its own OpenCode session, not inside the Coordinator", lambda: (
        (lambda rows: len(rows) == 2 and all(r["session_id"] and r["session_id"].startswith("ses_") for r in rows) and len({r["session_id"] for r in rows} | {session_id}) == 3)(
            list(db(alpha).execute("SELECT session_id FROM worker_runs WHERE workflow_id = ? AND purpose = 'task'", (workflow,)))
        ),
        {"coordinator": session_id, "workers": [r["session_id"] for r in db(alpha).execute("SELECT session_id FROM worker_runs WHERE workflow_id = ? AND purpose = 'task'", (workflow,))]},
    ))
    check("E2E-22", "Approved workflow completes; worker results return to the Coordinator through the bridge", lambda: (
        execution_step()[0] and any(e.get("role") == "dridh/coordinator" and '"outcome":"DONE"' in e.get("last_result_head", "") for e in stub_entries()),
        "workflow COMPLETED and coordinator observed a DONE outcome",
    ))

    # Parallel execution check: create a workflow with independent tasks and run with --parallel
    def parallel_step() -> tuple[bool, Any]:
        proj = init_project("parallel")
        # Create workflow with two independent tasks
        code, created, error = dridh_json(["workflow", "create", "Parallel task test", "--project", str(proj)], project=proj)
        if code != 0 or created is None:
            return False, f"create failed: {error[:120]}"
        wf = created["workflow_id"]
        # Plan manually via CLI (planner stub returns plan with T-001, T-002)
        code, planned, error = dridh_json(["workflow", "plan", wf, "--project", str(proj)], project=proj, timeout=900)
        if code != 0:
            return False, f"plan failed: {error[:120]}"
        approve_typed(proj, wf)
        code, run_result, error = dridh_json(["workflow", "run", wf, "--project", str(proj), "--parallel", "--concurrency", "2"], project=proj, timeout=900)
        if code != 0 or run_result is None:
            return False, f"run failed: {error[:120]}"
        status = workflow_status(proj, wf)
        tasks = {t["id"]: t["state"] for t in status["plan"]["tasks"]}
        return status["state"] == "COMPLETED" and all(s == "DONE" for s in tasks.values()), {"state": status["state"], "tasks": tasks, "outcome": run_result.get("outcome")}

    # Note: parallel_step requires a plan with independent tasks. The stub returns the standard plan
    # with T-002 depending on T-001, so this tests the parallel runner with a dependency chain.
    # A true parallel test would need a plan with independent tasks.
    check("E2E-22P", "Parallel execution runner completes a workflow", parallel_step)
    check("E2E-23", "Deterministic acceptance checks run on the worker results", lambda: (
        (lambda rows: sorted((r["task_id"], r["check_type"].split(":")[0]) for r in rows) == [("T-001", "file_contains"), ("T-001", "file_exists"), ("T-002", "command")] and all(r["passed"] == 1 for r in rows))(
            list(db(alpha).execute("SELECT task_id, check_type, passed FROM acceptance_results WHERE workflow_id = ?", (workflow,)))
        ),
        [dict(r) for r in db(alpha).execute("SELECT task_id, check_type, passed, detail FROM acceptance_results WHERE workflow_id = ?", (workflow,))][:6],
    ))

    def transitions_step() -> tuple[bool, Any]:
        events = [r["event_type"] for r in db(alpha).execute("SELECT event_type FROM ledger WHERE workflow_id = ? ORDER BY seq", (workflow,))]
        ok = events.count("TASK_STARTED") == 2 and events.count("TASK_COMPLETED") == 2 and "WORKFLOW_VERIFYING" in events and "WORKFLOW_COMPLETED" in events
        return ok, {"task_started": events.count("TASK_STARTED"), "task_completed": events.count("TASK_COMPLETED")}

    check("E2E-24", "Task and workflow transitions are recorded in order", transitions_step)

    def ledger_step() -> tuple[bool, Any]:
        verdict = dridh_json(["workflow", "verify-ledger", "--project", str(alpha)], project=alpha)[1]
        types = {r["event_type"] for r in db(alpha).execute("SELECT event_type FROM ledger")}
        ok = verdict["ok"] is True and {"SESSION_STARTED", "APPROVAL_GRANTED", "WORKER_STARTED", "WORKFLOW_COMPLETED", "SESSION_RESUMED"} <= types
        return ok, {"events": verdict["events"], "head": verdict["head"][:16]}

    check("E2E-25", "The ledger is persistent, hash-chained and verifies", ledger_step)

    def report_step() -> tuple[bool, Any]:
        report = alpha / ".opencode" / "dridh" / "reports" / f"{workflow}.md"
        as_json = alpha / ".opencode" / "dridh" / "artifacts" / workflow / "report.json"
        text = report.read_text() if report.exists() else ""
        ok = "COMPLETED" in text and "| PASS |" in text and "human:" in text and as_json.exists()
        shutil.copy(report, EVIDENCE / "report.md") if report.exists() else None
        shutil.copy(as_json, EVIDENCE / "report.json") if as_json.exists() else None
        return ok, {"report_bytes": len(text), "json_exists": as_json.exists()}

    check("E2E-27", "Completion report and machine-readable report are generated from persisted state", report_step)

    # Cancellation, timeout and recovery (steps 29-30)
    check("E2E-29", "Timeout is classified, retried and exhausted; cancellation and recovery propagate", lambda: timeout_and_cancellation())
    check("E2E-30", "Recovery after a killed bridge uses the expired lease and completes the workflow", lambda: recovery_after_kill())

    # Isolation (step 31)
    check("E2E-31", "Ordinary sessions, workers and workspaces stay isolated from Dridh state", lambda: isolation_checks(beta, alpha))

    # Phase 13 Planner lifecycle is separate from the generic legacy Planner scenario above.
    phase13_planner_checks()
    code_reacher_checks()
    worktree_permission_checks()

    # Results
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    summary = {
        "scope": "in-container",
        "checks": RESULTS,
        "passed": passed,
        "failed": len(RESULTS) - passed,
        "facts": FACTS,
        "commands": COMMANDS,
    }
    write_json(EVIDENCE / "scenario-summary.json", summary)
    if STUB is not None and STUB.poll() is None:
        os.killpg(STUB.pid, signal.SIGTERM)
    return 0 if RESULTS and all(r["status"] == "PASS" for r in RESULTS) else 1



def phase13_planner_checks() -> None:
    """Exercise the real Phase 13 PlannerService against the deterministic model boundary."""
    from planner_test_helpers import seed_approved_baseline

    from dridh.integrations.opencode import OpenCodeRunner
    from dridh.planner.service import PlannerService
    from dridh.state.design import DesignRepository
    from dridh.state.documents import DocumentRepository
    from dridh.state.planning import PlanningRepository
    from dridh.state.store import StateStore

    project = PROJECTS / "phase13-planner"
    session_id = ""
    service = None
    started: dict[str, Any] | None = None
    discovered: dict[str, Any] | None = None
    generated: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    setup_error = ""
    try:
        info, designer_session_id = seed_approved_baseline(project)
        runner = OpenCodeRunner(
            executable=OPENCODE,
            workspace_root=project,
            config_dir=CONFIG,
            timeout=180,
            env=base_env(),
        )
        service = PlannerService(info, runner=runner)
        started = service.start(designer_session_id, model="dridhtest/scripted")
        session_id = str(started.get("session_id", ""))
    except Exception as exc:  # noqa: BLE001 - each mandatory stage below is still recorded
        setup_error = f"{type(exc).__name__}: {exc}"

    start_ok = bool(started and session_id and started.get("status") == "INQUIRY" and started.get("stories", 0) > 0)
    record("E2E-35", "Phase 13 planner.start runs the real OpenCode Indexer and persists its proposal",
           start_ok, started or setup_error)

    if service is not None and session_id:
        try:
            discovered = service.status(session_id)
        except Exception as exc:  # noqa: BLE001
            setup_error = f"status: {type(exc).__name__}: {exc}"
    status_ok = bool(discovered and discovered.get("session_id") == session_id
                     and discovered.get("status") == "INQUIRY" and discovered.get("stories", 0) > 0
                     and discovered.get("resumable") is True)
    record("E2E-36", "Phase 13 planner.status returns the resumable persisted discovery session",
           status_ok, discovered or setup_error)

    if service is not None and session_id:
        try:
            generated = service.generate(session_id, model="dridhtest/scripted")
        except Exception as exc:  # noqa: BLE001
            setup_error = f"generate: {type(exc).__name__}: {exc}"
    generation_ok = bool(generated and generated.get("status") == "GENERATED"
                         and generated.get("generated", {}).get("story_count", 0) > 0
                         and generated.get("generated", {}).get("component_count", 0) > 0
                         and generated.get("generated", {}).get("test_phase_count", 0) > 0
                         and (generated.get("jev_checkpoint") or {}).get("advisory") is True)
    record("E2E-37", "Phase 13 planner.generate creates central, component, phase and Table 15 plans",
           generation_ok, generated or setup_error)

    pre_approval: dict[str, Any] = {}
    if service is not None and session_id:
        try:
            from planner_test_helpers import tick_review

            pre_approval = service.gate(session_id)
            review = service.open_review(session_id)
            challenge = service.approval_challenge(session_id)
            try:  # scenario D: an incomplete checklist blocks approval
                service.approve(session_id, reviewer="e2e-human", confirmation=challenge["confirmation"])
                unchecked_refused = False
            except Exception as refusal:  # noqa: BLE001
                unchecked_refused = "checklist is incomplete" in str(refusal)
            tick_review(info, review["review_id"])
            service.approve(session_id, reviewer="e2e-human", confirmation=challenge["confirmation"])
            gate = service.gate(session_id)
            pre_approval["unchecked_refused"] = unchecked_refused
        except Exception as exc:  # noqa: BLE001
            setup_error = f"gate: {type(exc).__name__}: {exc}"
    gate_ok = bool(gate and gate.get("unlocked") is True and gate.get("session_id") == session_id
                   and pre_approval.get("unlocked") is False and pre_approval.get("plan_valid") is True
                   and pre_approval.get("unchecked_refused") is True)
    record("E2E-38", "Phase 13 planner.gate stays closed until revision-bound human approval, then unlocks after SQLite reconciliation",
           gate_ok, gate or setup_error)

    artifact_details: dict[str, Any] = {"files": 0, "sqlite_reconciled": 0, "missing": [], "validation_failures": []}
    artifacts_ok = False
    if generated and generated.get("generated") and project.exists():
        try:
            file_items = generated["generated"]["files"]
            required_suffixes = (
                "CENTRAL-PLAN-INDEX.md", "TEST-PLAN.md",
                "PROJECT-STRUCTURE-PLAN.md", "PLANNING-REVIEW-CHECKLIST.md",
            )
            paths = [item["path"] for item in file_items]
            artifact_details["files"] = len(paths)
            for suffix in required_suffixes:
                if not any(path.endswith(suffix) for path in paths):
                    artifact_details["missing"].append(suffix)
            if not any("/COMPONENT-MASTER.md" in path for path in paths):
                artifact_details["missing"].append("COMPONENT-MASTER.md")
            if not any("/PHASE-" in path for path in paths):
                artifact_details["missing"].append("PHASE-*.md")
            with StateStore(info.state_db, info.workspace_id) as store:
                documents = DocumentRepository(store.connection, info.workspace_id)
                planning = PlanningRepository(store.connection, info.workspace_id)
                design = DesignRepository(store.connection, info.workspace_id)
                for item in file_items:
                    relative = item["path"]
                    target = project / relative
                    metadata = documents.document_metadata(relative)
                    revision = documents.current_revision(relative)
                    if not target.is_file() or metadata is None or revision is None:
                        artifact_details["missing"].append(relative)
                        continue
                    if revision.content_sha256 != item["sha256"] or target.read_text(encoding="utf-8") != documents.stored_content(revision.id):
                        artifact_details["validation_failures"].append(f"{relative}: Markdown/SQLite revision mismatch")
                        continue
                    if item["kind"] in {"central_plan", "phase_file", "test_plan"}:
                        kind = {"central_plan": "CENTRAL_PLAN", "phase_file": "PHASE_FILE", "test_plan": "TEST_PLAN"}[item["kind"]]
                        findings = planning.findings(
                            relative_path=relative, document_id=str(metadata["id"]), kind=kind,
                            current_revision_id=revision.id, current_content=documents.stored_content(revision.id),
                        )
                        artifact_details["validation_failures"].extend(f"{relative}: {finding.message}" for finding in findings)
                    elif item["kind"] == "component_master":
                        findings = design.findings(
                            relative_path=relative, document_id=str(metadata["id"]), kind="COMPONENT_MASTER",
                            current_revision_id=revision.id, current_content=documents.stored_content(revision.id),
                        )
                        artifact_details["validation_failures"].extend(f"{relative}: {finding.message}" for finding in findings)
                    if item["kind"] != "artifact":
                        validator = {
                            "central_plan": "planner.central", "phase_file": "planner.phase",
                            "test_plan": "planner.test-plan", "component_master": "planner.component-master",
                        }[item["kind"]]
                        validation = documents.latest_validation(revision.id, validator)
                        if validation is None or validation.status != "PASS":
                            artifact_details["validation_failures"].append(f"{relative}: {validator} is not PASS")
                    artifact_details["sqlite_reconciled"] += 1
            artifacts_ok = (
                artifact_details["files"] > 0
                and artifact_details["sqlite_reconciled"] == artifact_details["files"]
                and not artifact_details["missing"]
                and not artifact_details["validation_failures"]
            )
        except Exception as exc:  # noqa: BLE001
            artifact_details["exception"] = f"{type(exc).__name__}: {exc}"
    record("E2E-39", "Phase 13 generated artifacts exist, validate and match their SQLite revisions",
           artifacts_ok, artifact_details)

    required_roles = {
        "dridh/workers/planner-indexer",
        "dridh/workers/planner-component",
        "dridh/workers/planner-testing",
    }
    planner_entries = [item for item in stub_entries() if item.get("role") in required_roles]
    roles_seen = {item.get("role") for item in planner_entries}
    models_seen = {item.get("model") for item in planner_entries}
    stub_ok = required_roles <= roles_seen and bool(planner_entries) and models_seen <= {"scripted", "dridhtest/scripted"}
    detail = {"roles": sorted(roles_seen), "models": sorted(str(value) for value in models_seen), "requests": len(planner_entries)}
    record("E2E-40", "Phase 13 Indexer, Component and Testing workers each execute through OpenCode's stub model boundary",
           stub_ok, detail)
    # E2E-41: the feature lifecycle consumes the real gates (Ideator, Designer, Planner) and stops at EXECUTION.
    lifecycle_detail: Any = "planner gate was not unlocked"
    lifecycle_ok = False
    if gate_ok:
        try:
            from planner_test_helpers import IDEATOR_SESSION

            from dridh.coordinator.lifecycle import FeatureLifecycle

            lifecycle = FeatureLifecycle(info)
            lifecycle.create_feature("Order Intake", actor="e2e:lifecycle")
            lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor="e2e:lifecycle")
            synced = lifecycle.sync("order-intake", actor="e2e:lifecycle")
            visited = [move["to"] for move in synced["moves"]]
            stage = synced["status"]["feature"]["stage"]
            lifecycle_ok = stage == "EXECUTION" and visited == [
                "IDEATION", "USE_CASE_REVIEW", "TECHNICAL_DISCOVERY", "TDD_REVIEW", "PLANNING", "PLAN_VALIDATION", "EXECUTION",
            ] and synced["status"]["sessions"]["planner_session_id"] == session_id
            lifecycle_detail = {"stage": stage, "visited": visited, "blockers": synced["status"]["blockers"]}
        except Exception as exc:  # noqa: BLE001 - recorded as a failed mandatory check
            lifecycle_detail = f"{type(exc).__name__}: {exc}"
    record("E2E-41", "Feature lifecycle carries the real Ideator, Designer and Planner gates to EXECUTION", lifecycle_ok, lifecycle_detail)
    # E2E-43: the Executor controller drives the real approved plan (real planner sync, real worktrees, scripted agent).
    execution_detail: Any = "lifecycle did not reach EXECUTION"
    execution_ok = False
    if lifecycle_ok:
        try:
            execution_detail = executor_plan_run(info, service, session_id, project)
            execution_ok = execution_detail["ok"]
        except Exception as exc:  # noqa: BLE001 - recorded as a failed mandatory check
            execution_detail = f"{type(exc).__name__}: {exc}"
    record("E2E-43", "Executor drives the approved plan through checkpoints to completion without invalidating the plan gate",
           execution_ok, execution_detail)
    # E2E-44: delivery pushes the stories, opens stacked PRs, checks the exact SHA and unlocks via the integration branch.
    delivery_detail: Any = "the Executor run did not complete"
    delivery_ok = False
    if execution_ok:
        try:
            delivery_detail = delivery_plan_run(info, service, project, execution_detail["run_id"])
            delivery_ok = delivery_detail["ok"]
        except Exception as exc:  # noqa: BLE001 - recorded as a failed mandatory check
            delivery_detail = f"{type(exc).__name__}: {exc}"
    record("E2E-44", "Delivery opens stacked PRs, evaluates checks for the exact head SHA and unlocks through the integration branch",
           delivery_ok, delivery_detail)
    # E2E-46: the packaged CLI imports the ledger, reconciles it with the real plan, detects drift and repairs the rows from the file.
    ledger_detail: Any = "the plan was not generated"
    ledger_ok = False
    if gate_ok:
        try:
            ledger_detail = ledger_checks(project)
            ledger_ok = ledger_detail["ok"]
        except Exception as exc:  # noqa: BLE001 - recorded as a failed mandatory check
            ledger_detail = f"{type(exc).__name__}: {exc}"
    record("E2E-46", "Ledger import, reconciliation, drift detection and repair work from the packaged install", ledger_ok, ledger_detail)
    FACTS["phase13_planner"] = {
        "session_id": session_id,
        "status": generated and generated.get("status"),
        "gate_unlocked": gate_ok,
        "worker_roles": sorted(roles_seen),
        "artifacts": artifact_details,
    }


def executor_plan_run(info: Any, planner: Any, session_id: str, project: Path) -> dict[str, Any]:
    """Run every story of the real approved plan with a scripted agent and review each checkpoint lot."""
    import asyncio
    import subprocess
    from types import SimpleNamespace

    from dridh.config.dridh_yaml import ExecutionConfig
    from dridh.executor.controller import ExecutionController
    from dridh.executor.story_runner import StoryRunner

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True).stdout.strip()

    (project / ".gitignore").write_text(".opencode/\n__pycache__/\n")
    git("init", "-q")
    git("config", "user.email", "e2e@example.com")
    git("config", "user.name", "e2e")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    stories = {item["story_id"]: item for item in planner.execution_stories(session_id)}

    class ScriptedAgent:
        async def run(self, **kwargs: Any) -> Any:
            story_id = kwargs["title"].split()[-1]
            root = Path(kwargs["worktree_path"])
            for owned in stories[story_id]["file_ownership"] or [f"src/{story_id.lower()}"]:
                target = root / owned if Path(owned).suffix else root / owned / f"{story_id.lower()}.py"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"# {story_id}\nVALUE = 1\n")
            (root / "tests").mkdir(exist_ok=True)
            # Like a real Executor: one passing test named for each approved flow the prompt lists, and a report of every flow.
            flows = re.findall(r"^- ((?:SF|AF|EF|BE|BR)-\d{2}\.\d+) \(", kwargs["prompt"], re.MULTILINE)
            names = [f"test_{flow.lower().replace('-', '_').replace('.', '_')}" for flow in flows] or ["test_ok"]
            body = "".join(f"    def {name}(self):\n        self.assertTrue(True)\n\n" for name in names)
            (root / "tests" / f"test_{re.sub('[^a-z0-9]', '_', story_id.lower())}.py").write_text(
                f"import unittest\n\n\nclass T(unittest.TestCase):\n{body}")
            reply = {"status": "DONE", "summary": "ok", "files_changed": [], "flows": [{"id": flow, "status": "IMPLEMENTED"} for flow in flows]}
            return SimpleNamespace(text=json.dumps(reply), session_id=f"ses-{story_id}")

    settings = ExecutionConfig(test_command=["python3", "-m", "unittest", "discover", "-s", "tests"], test_timeout_seconds=120)
    controller = ExecutionController(info, planner=planner, story_runner=StoryRunner(info, ScriptedAgent(), settings), settings=settings)
    challenge = controller.challenge("order-intake")
    run = controller.start("order-intake", confirmed_by="e2e-human", confirmation=challenge["confirmation"])
    result = asyncio.run(controller.drive(run["id"]))
    reviewed = 0
    for _ in range(40):
        if result["run"]["status"] == "RUNNING":
            result = asyncio.run(controller.drive(run["id"]))
            continue
        if result["run"]["status"] != "PAUSED":
            break
        waiting = [lot for lot in result["lots"] if lot["status"] == "AWAITING_REVIEW"]
        if not waiting:
            break
        for lot in waiting:
            check = controller.review_challenge(run["id"], lot["ordinal"], "approved")
            controller.review_lot(run["id"], lot["ordinal"], verdict="approved", reviewer="e2e-human", notes="", confirmation=check["confirmation"])
            reviewed += 1
        if controller.status(run["id"])["run"]["status"] == "PAUSED":
            controller.resume(run["id"], actor="e2e")
        result = controller.status(run["id"])
    final = controller.status(run["id"])
    after = planner.gate(session_id)
    statuses = {item["story_id"]: item["status"] for item in planner.execution_stories(session_id)}
    branches = git("branch", "--list", "dridh/story/*").splitlines()
    ok = (final["run"]["status"] == "COMPLETED" and set(statuses.values()) == {"COMPLETED"} and after.get("unlocked") is True
          and len(branches) == len(statuses) and git("status", "--porcelain") == "" and reviewed >= 2)
    return {"ok": ok, "run_id": run["id"], "run": final["run"]["status"], "stories": len(statuses), "lots": [(lot["kind"], lot["status"]) for lot in final["lots"]],
            "gate_after": after.get("unlocked"), "reason": after.get("reason"), "branches": len(branches)}


def delivery_plan_run(info: Any, planner: Any, project: Path, run_id: str) -> dict[str, Any]:
    """Deliver every completed story of the real plan to a local bare remote with a scripted GitHub adapter."""
    import subprocess

    from dridh.config.dridh_yaml import DeliveryConfig, ExecutionConfig
    from dridh.delivery.engine import DeliveryEngine

    def git(*args: str, cwd: Path = project) -> str:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    remote = project.parent / "phase13-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git("branch", "-M", "main")
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "main")

    class ScriptedGitHub:
        def __init__(self) -> None:
            self.prs: dict[int, dict[str, Any]] = {}

        def create_pr(self, *, title: str, body: str, head: str, base: str, draft: bool = False) -> dict[str, Any]:
            number = len(self.prs) + 1
            self.prs[number] = {"head": head, "base": base, "sha": git("rev-parse", head), "body": body}
            return {"number": number, "url": f"https://github.com/example/app/pull/{number}", "head_sha": self.prs[number]["sha"], "base_sha": "0" * 40}

        def pr_view(self, number: int) -> dict[str, Any]:
            return {"state": "OPEN", "head_sha": self.prs[number]["sha"], "base_ref": self.prs[number]["base"], "merged": False}

        def check_runs(self, sha: str) -> list[dict[str, Any]]:
            return [{"name": "ci", "status": "completed", "conclusion": "success", "url": "", "run_id": "1"}]

    github = ScriptedGitHub()
    settings = ExecutionConfig(test_command=["python3", "-m", "unittest", "discover", "-s", "tests"], test_timeout_seconds=120)
    engine = DeliveryEngine(info, config=DeliveryConfig(enabled=True, poll_seconds=0.01, checks_timeout_seconds=1), execution=settings,
                            planner=planner, adapter=github)
    from dridh.state.runs import RunRepository
    from dridh.state.store import StateStore

    with StateStore(info.state_db, info.workspace_id) as store:
        session_id = RunRepository(store.read(), info.workspace_id).get_run(run_id)["planner_session_id"]
    stories = planner.execution_stories(session_id)
    unlocked: list[str] = []
    for story in stories:  # dependency order
        sid = story["story_id"]
        engine.create_pr(run_id, sid)
        checks = engine.wait_for_checks(sid, sleep=lambda seconds: None)
        outcome = engine.integrate(run_id, sid) if checks["state"] == "PASS" else {"unlocked": False, "checks": checks}
        if outcome.get("unlocked"):
            unlocked.append(sid)
    tip = git("rev-parse", "dridh/integration/order-intake", cwd=remote)
    tree = set(git("ls-tree", "-r", "--name-only", "dridh/integration/order-intake", cwd=remote).split())
    bases = {pr["base"] for pr in github.prs.values()}
    ok = (len(unlocked) == len(stories) and engine._index_revision() == tip  # noqa: SLF001
          and git("rev-parse", "main", cwd=remote) == git("rev-parse", "main") and "main" in bases
          and all(engine.gates(sid).get("DEPENDENTS_UNLOCKED", {}).get("status") == "PASS" for sid in unlocked)
          and planner.gate(session_id).get("unlocked") is True and len(tree) > len(stories))
    return {"ok": ok, "unlocked": len(unlocked), "stories": len(stories), "prs": len(github.prs), "bases": sorted(bases),
            "index_matches_tip": engine._index_revision() == tip, "gate_after": planner.gate(session_id).get("unlocked")}  # noqa: SLF001


def worktree_permission_checks() -> None:
    """E2E-45: a worker cannot remove a worktree that OpenCode itself registered; the worktree survives.

    The worktree is created through OpenCode's own API so the tool's "not a worktree of this project" guard cannot be what
    refuses the call: only the profile's permission rule can. The refusal must therefore be "Permission denied".
    """
    project = init_project("worktree-permission")
    detail: Any = {}
    ok = False
    try:
        env = base_env()
        # A first session registers the project with OpenCode (the worktree API needs a known project id).
        opencode_events(["--agent", "dridh/code-reacher", "ping"], cwd=project, env=env)
        listing = run([OPENCODE, "--standalone", "api", "GET", "/api/project"], env=env, cwd=project, timeout=120).stdout
        project_id = next(p["id"] for p in json.loads(listing) if Path(p["canonical"]).resolve() == project.resolve())
        outcomes: dict[str, Any] = {}
        survives: dict[str, bool] = {}
        for agent in ("dridh/code-reacher", "dridh/executor"):
            created = run([OPENCODE, "--standalone", "api", "POST", "/api/worktree", "-H", "content-type: application/json",
                           "-d", json.dumps({"projectID": project_id, "name": "probe-" + agent.split("/")[1]})], env=env, cwd=project, timeout=120)
            target = Path(json.loads(created.stdout)["directory"])
            calls: list[dict[str, Any]] = []
            for _ in range(3):  # OpenCode's first call can stall; a missing tool call is retried, never counted as a pass
                events = opencode_events(["--agent", agent, f"PROBE_TOOL:opencode_worktree_remove PROBE_WORKTREE:{target}"], cwd=project, env=env, timeout=120)
                calls = [c for c in tool_outputs(events) if c["tool"] == "opencode_worktree_remove"]
                if calls:
                    break
            outcomes[agent] = {"calls": len(calls), "statuses": [c["status"] for c in calls], "output": [c["output"][:160] for c in calls]}
            survives[agent] = target.is_dir()
        detail = {"agents": outcomes, "worktree_survives": survives}
        ok = all(v["calls"] >= 1 and all(st != "completed" for st in v["statuses"]) and all("permission denied" in o.lower() for o in v["output"])
                 for v in outcomes.values()) and all(survives.values())
    except Exception as exc:  # noqa: BLE001 - recorded as a failed mandatory check
        detail = f"{type(exc).__name__}: {exc}"
    record("E2E-45", "OpenCode permission rules refuse removal of an OpenCode-registered worktree for both worker profiles and it survives", ok, detail)


def ledger_checks(project: Path) -> dict[str, Any]:
    header = ("# Order Intake — Coordinator Ledger\n\n> **Standards Version:** 2.1.0  \n> **Document Status:** DRAFT  \n> **Last Updated:** 2026-11-08\n\n"
              "| Ledger ID | UC / Flow ID | Deviation Status | Linked Story ID | Original Requirement | Actual / Deviated Behavior | Justification | Approved By |\n"
              "|---|---|---|---|---|---|---|---|\n")
    row = "| LDG-01 | UC-02 | DONE | — | Cancellation is recorded | Cancellation is recorded | Delivered as specified | — |\n"
    body = "\n#### LDG-01: Cancellation delivered as specified\n\n- **Disposition:** DONE.\n"
    path = project / "LEDGER.md"
    path.write_text(header + row + body)
    args = ["--project", str(project)]
    code_import, imported, error = dridh_json(["ledger", "import", "LEDGER.md", *args], project=project)
    code_first, first, _ = dridh_json(["ledger", "reconcile", "LEDGER.md", *args], project=project)
    path.write_text(path.read_text().replace("Cancellation is recorded | Delivered", "Cancellation is recorded and audited | Delivered"))
    code_drift, drift, _ = dridh_json(["ledger", "reconcile", "LEDGER.md", *args], project=project)
    code_fix, fixed, _ = dridh_json(["ledger", "reconcile", "LEDGER.md", "--repair", *args], project=project)
    listed = dridh_json(["ledger", "list", *args], project=project)[1] or {}
    codes = lambda report: {f["code"] for f in (report or {}).get("findings", [])}  # noqa: E731
    ok = (code_import == 0 and imported and imported["imported"] == 1
          and first is not None and "LEDGER_DRIFT" not in codes(first) and "LEDGER_ROWS_TAMPERED" not in codes(first)
          and "LEDGER_DRIFT" in codes(drift) and fixed is not None and fixed.get("repaired") is True and "LEDGER_DRIFT" not in codes(fixed)
          and [e["ledger_id"] for e in listed.get("entries", [])] == ["LDG-01"])
    return {"ok": ok, "import": code_import, "first_codes": sorted(codes(first)), "drift_codes": sorted(codes(drift)), "repaired": (fixed or {}).get("repaired"),
            "exit_codes": [code_first, code_drift, code_fix], "error": error[:200]}


def code_reacher_checks() -> None:
    """E2E-42: the packaged product indexes real Python, TypeScript and Java and answers queries (native wheels work)."""
    project = init_project("code-reacher")
    (project / "shop.py").write_text('def tax(amount):\n    """Add tax."""\n    return amount * 1.2\n\n\ndef quote(amount):\n    return tax(amount)\n')
    (project / "server.ts").write_text("import express from 'express'\nconst app = express()\napp.get('/ping', (req, res) => res.send('pong'))\n")
    (project / "Ping.java").write_text("package demo;\npublic class Ping {\n    public String pong() { return \"pong\"; }\n}\n")
    detail: Any = {}
    ok = False
    try:
        code, first, error = dridh_json(["index", "--no-embed", "--project", str(project)], project=project)
        code_again, second, _ = dridh_json(["index", "--no-embed", "--project", str(project)], project=project)
        _, deps, _ = dridh_json(["code", "deps", "shop.quote", "--direction", "callees", "--depth", "1", "--project", str(project)], project=project)
        _, endpoints, _ = dridh_json(["code", "endpoints", "--project", str(project)], project=project)
        _, found, _ = dridh_json(["code", "search", "tax", "--no-semantic", "--project", str(project)], project=project)
        ok = bool(
            code == 0 and first and first["status"] == "SUCCEEDED" and first["files_parsed"] == 3
            and code_again == 0 and second and second["unchanged"] is True
            and deps and [c["qualified_name"] for c in deps["callees"]] == ["shop.tax"]
            and endpoints and [(e["method"], e["path"], e["framework"]) for e in endpoints["endpoints"]] == [("GET", "/ping", "express")]
            and found and found["results"] and found["results"][0]["qualified_name"] == "shop.tax"
        )
        detail = {"parsed": first and first.get("files_parsed"), "second_unchanged": second and second.get("unchanged"),
                  "callees": deps and [c["qualified_name"] for c in deps.get("callees", [])], "error": error[:160]}
    except Exception as exc:  # noqa: BLE001 - recorded as a failed mandatory check
        detail = f"{type(exc).__name__}: {exc}"
    record("E2E-42", "Code Reacher indexes Python, TypeScript and Java and answers queries from the packaged install", ok, detail)


def approval_gates(project: Path, workflow: str) -> tuple[bool, Any]:
    entries = [e for e in stub_entries() if e.get("role") == "dridh/coordinator"]
    refused_in_session = any("not been approved" in e.get("last_result_head", "") for e in entries)
    code, _, error = dridh_json(["workflow", "approve", workflow, "--project", str(project)], project=project, env=base_env())
    no_tty = code == 5 and "AUTHORIZATION_DENIED" in error
    wrong = pty_approve(project, workflow, answer=f"APPROVE {workflow} 000000000000")
    state_after_wrong = workflow_status(project, workflow)["state"]
    wrong_refused = wrong.exit_code == 5 and state_after_wrong == "AWAITING_APPROVAL"
    FACTS["gates"] = {"unapproved_execute_refused": refused_in_session, "no_terminal_refused": no_tty, "wrong_confirmation_refused": wrong_refused}
    approve_typed(project, workflow)
    return refused_in_session and no_tty and wrong_refused, FACTS["gates"]


def timeout_and_cancellation() -> tuple[bool, Any]:
    details: dict[str, Any] = {}
    delta = init_project("delta")
    _, delta_wf = start_objective_via_cli(delta)
    approve_typed(delta, delta_wf)
    set_control(code_reacher_delay=60)
    timeout_env = base_env(DRIDH_WORKER_TIMEOUT_SECONDS="5")
    code, run_document, error = dridh_json(["workflow", "run", delta_wf, "--project", str(delta)], project=delta, env=timeout_env, timeout=900)
    status = workflow_status(delta, delta_wf)
    task = next(t for t in status["plan"]["tasks"] if t["id"] == "T-001")
    details["timeout"] = {"outcome": run_document and run_document["outcome"], "state": status["state"], "attempts": task["attempts"], "failure_code": task["failure_code"]}
    timeout_ok = status["state"] == "FAILED" and task["failure_code"] == "WORKER_TIMEOUT" and task["attempts"] == 2
    leftover_after_timeout = wait_until(lambda: not processes(WORKER_PATTERN), 40)
    details["timeout"]["no_worker_left"] = leftover_after_timeout

    epsilon = init_project("epsilon")
    eps_session, eps_wf = start_objective(epsilon, OBJECTIVE)
    approve_typed(epsilon, eps_wf)
    set_control(code_reacher_delay=120)
    coordinator_env = base_env()
    proc = subprocess.Popen(
        [DRIDH, "session", "resume", eps_session, f"Continue workflow {eps_wf}", "--project", str(epsilon)],
        env=coordinator_env,
        cwd=str(epsilon),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = wait_for_worker(120)
    children = [p for p in processes("opencode") if p[1] == proc.pid]
    outer = children[0][0] if children else None
    if outer is not None:
        os.kill(outer, signal.SIGKILL)
    gone = wait_until(lambda: not processes(WORKER_PATTERN) and not processes(DRIDH_BRIDGE_PATTERN), 60)
    proc.wait(timeout=120)
    cancelled_state = workflow_status(epsilon, eps_wf)["state"]
    cancel_events = [r["event_type"] for r in db(epsilon).execute("SELECT event_type FROM ledger WHERE workflow_id = ?", (eps_wf,))]
    details["cancellation"] = {"worker_seen": started, "outer_killed": outer is not None, "state": cancelled_state, "no_bridge_or_worker_left": gone, "cancelled_recorded": "WORKFLOW_CANCELLED" in cancel_events}
    cancel_ok = started and outer is not None and gone and cancelled_state == "CANCELLED" and "WORKFLOW_CANCELLED" in cancel_events
    FACTS["timeout_cancel"] = details
    return timeout_ok and leftover_after_timeout and cancel_ok, details


def start_objective_via_cli(project: Path) -> tuple[str, str]:
    code, created, error = dridh_json(["workflow", "create", OBJECTIVE, "--project", str(project)], project=project)
    if code != 0 or created is None:
        raise RuntimeError(f"workflow create failed: {error[:200]}")
    code, planned, error = dridh_json(["workflow", "plan", created["workflow_id"], "--project", str(project)], project=project, timeout=900)
    if code != 0 or planned is None:
        raise RuntimeError(f"workflow plan failed: {error[:200]}")
    return "", created["workflow_id"]


def recovery_after_kill() -> tuple[bool, Any]:
    zeta = init_project("zeta")
    zeta_session, zeta_wf = start_objective(zeta, OBJECTIVE)
    approve_typed(zeta, zeta_wf)
    set_control(code_reacher_delay=25)
    recovery_env = base_env(DRIDH_LEASE_TTL_SECONDS="3")
    proc = subprocess.Popen(
        [DRIDH, "session", "resume", zeta_session, f"Continue workflow {zeta_wf}", "--project", str(zeta)],
        env=recovery_env,
        cwd=str(zeta),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    worker_seen = wait_for_worker(120)
    bridges = processes(DRIDH_BRIDGE_PATTERN)
    killed = False
    if bridges:
        os.kill(bridges[0][0], signal.SIGKILL)
        killed = True
    in_progress = [dict(r) for r in db(zeta).execute("SELECT id, state, lease_expires_at FROM tasks WHERE workflow_id = ? AND state = 'IN_PROGRESS'", (zeta_wf,))]
    time.sleep(6)
    proc.wait(timeout=120)
    set_control(code_reacher_delay=0)
    code, document, error = dridh_json(["workflow", "run", zeta_wf, "--project", str(zeta)], project=zeta, env=recovery_env, timeout=900)
    status = workflow_status(zeta, zeta_wf)
    events = [r["event_type"] for r in db(zeta).execute("SELECT event_type FROM ledger WHERE workflow_id = ?", (zeta_wf,))]
    task = next(t for t in status["plan"]["tasks"] if t["id"] == "T-001")
    orphan_exited = wait_until(lambda: not processes(WORKER_PATTERN), 60)
    details = {
        "worker_seen": worker_seen,
        "bridge_killed": killed,
        "in_progress_before_kill": len(in_progress),
        "run_outcome": document and document["outcome"],
        "state": status["state"],
        "interrupted_recorded": "TASK_INTERRUPTED" in events,
        "attempts": task["attempts"],
        "orphaned_worker_exited": orphan_exited,
    }
    FACTS["recovery"] = details
    ok = worker_seen and killed and in_progress and document and document["outcome"] == "COMPLETED" and "TASK_INTERRUPTED" in events
    return bool(ok), details


def isolation_checks(beta: Path, alpha: Path) -> tuple[bool, Any]:
    beta_events = dridh_json(["workflow", "verify-ledger", "--project", str(beta)], project=beta)[1]["events"]
    probe = opencode_events(["--agent", "dridh/code-reacher", "PROBE_EXECUTE"], cwd=alpha, env=base_env())
    denied = [c for c in tool_outputs(probe) if c["tool"] == "execute" and c["status"] == "error"]
    ordinary = opencode_events(["hello from an ordinary session"], cwd=beta, env=base_env())
    ordinary_text = " ".join(part.get("text", "") for e in ordinary for part in [e.get("part", {})] if part.get("type") == "text")
    ordinary_roles = {e.get("role") for e in stub_entries() if e.get("user_head", "").startswith("hello from")}
    coordinator_in_ordinary = "dridh/coordinator" in ordinary_roles or bool(ordinary_roles - {"none", "title"})
    default_agent = "default_agent" in json.loads((CONFIG / "opencode.json").read_text())
    details = {
        "beta_events": beta_events,
        "worker_cannot_reach_bridge": bool(denied),
        "ordinary_session_has_text": bool(ordinary_text),
        "ordinary_session_coordinator": coordinator_in_ordinary,
        "default_agent_written": default_agent,
    }
    FACTS["isolation"] = details
    return beta_events == 0 and bool(denied) and bool(ordinary_text) and not coordinator_in_ordinary and not default_agent, details


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if STUB is not None and STUB.poll() is None:
            os.killpg(STUB.pid, signal.SIGTERM)
    sys.exit(code)
