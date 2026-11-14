# ruff: noqa: E701,E702,E401,F401,I001,A,B008
"""Dridh command line: installation, workspace, Coordinator sessions, workflows and the bridge.

Every command writes one JSON document to stdout. Human confirmations (approval and
rejection) write their prompt to stderr and read the typed answer from a terminal.
Structured failures are written to stderr as JSON and exit with status 1.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import platform
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

import dridh
from dridh.assets import default_agent_source, default_plugin_source
from dridh.common.errors import DridhError, ErrorCode, from_agent_error
from dridh.common.redaction import redact
from dridh.cli.render import force_json, render, render_error, to_json, wants_json
from dridh.document_contracts.findings import Finding, ValidationResult
from dridh.document_contracts.ledger_store import load_ledger_decisions, record_ledger_decision
from dridh.document_contracts.registry import ContractBundleError, load_contract_bundle
from dridh.document_contracts.markdown import parse_tables, plain
from dridh.document_contracts.validator import (
    H_LEDGER,
    LDG_ID,
    NO_APPROVER,
    LEDGER_APPROVAL_REQUIRED,
    STANDARDS_VERSION,
    LedgerDecision,
    classify_document,
    content_revision,
    ledger_entry_revision,
    sanitize_tdd,
    validate_central_plan,
    validate_component_master,
    validate_coordinator,
    validate_document,
    validate_document_collection,
    validate_ledger,
    validate_phase_file,
    validate_qa_log,
    validate_story_graph,
    validate_tdd,
    validate_test_plan,
    validate_use_cases,
)
from dridh.cli.exit_codes import exit_code_for
from dridh.designer.service import DesignerService
from dridh.ideator.service import IdeatorService
from dridh.planner.service import PlannerService
from dridh.install.installer import (
    apply_install,
    apply_uninstall,
    plan_install,
    plan_uninstall,
    resolve_layout,
)
from dridh.install.installer import (
    status as runtime_status,
)
from dridh.orchestration.engine import WorkflowEngine
from dridh.orchestration.sessions import SessionService
from dridh.state.design import DesignRepository, validated_master_model, validated_tdd_model
from dridh.state.documents import DocumentRepository
from dridh.state.planning import PlanningRepository, validated_central, validated_phase, validated_test_plan
from dridh.state.store import StateStore
from dridh.state.usecases import UseCaseRepository, validated_model
from dridh.workspace.layout import discover_workspace_root, init_workspace, open_workspace, workspace_exists

app = typer.Typer(
    name="dridh",
    help="Dridh harness: install the OpenCode runtime, manage project workspaces and take features from idea to delivery. "
    "Output is readable text in a terminal and JSON otherwise; add --json anywhere to force JSON.",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
)
workflow_app = typer.Typer(help="Inspect and finish generic workflows created before the feature lifecycle (creating new ones is retired).",
                           no_args_is_help=True)
session_app = typer.Typer(help="Start or resume Dridh Coordinator sessions through OpenCode.", no_args_is_help=True)
ideator_app = typer.Typer(help="Run resumable Ideator discovery, Use Case generation and human review.", no_args_is_help=True)
designer_app = typer.Typer(help="Run approved-baseline Technical Designer inquiry, TDD generation and review.", no_args_is_help=True)
app.add_typer(workflow_app, name="workflow", hidden=True)  # retired; kept to finish workflows created before the lifecycle
app.add_typer(session_app, name="session")
feature_app = typer.Typer(help="Track a feature through the Dridh lifecycle (ideation to completion).", no_args_is_help=True)
app.add_typer(feature_app, name="feature")
code_app = typer.Typer(help="Query the Code Reacher codebase index.", no_args_is_help=True)
app.add_typer(code_app, name="code")
execute_app = typer.Typer(help="Run the human-approved plan's stories with the Executor.", no_args_is_help=True)
app.add_typer(execute_app, name="execute")
app.add_typer(ideator_app, name="ideator")
app.add_typer(designer_app, name="designer")

EXIT_PREREQUISITE_MISSING = 3

PROJECT_OPTION = typer.Option(
    None, "--workspace", "--project",
    help="Workspace root (the project that holds .opencode/dridh/). Default: the nearest directory at or above the current one "
         "that holds a Dridh workspace, else the nearest Git root, else the current directory.",
)
CONFIG_DIR_OPTION = typer.Option(None, "--config-dir", help="OpenCode configuration directory. Defaults to OPENCODE_CONFIG_DIR or ~/.config/opencode.")
CONFIG_FILE_OPTION = typer.Option(None, "--config-file", help="opencode.json or opencode.jsonc, when both exist.")
DRY_RUN_OPTION = typer.Option(False, "--dry-run", help="Show the exact changes without writing anything.")
FORCE_OPTION = typer.Option(False, "--force", help="Replace or remove files Dridh does not manage. Originals are backed up first.")


def _emit(document: Any, view: str | None = None) -> None:
    """JSON for scripts, agents and pipes; readable text in a terminal (see dridh.cli.render)."""
    typer.echo(to_json(document) if wants_json() else render(document, view))


def _version_text() -> str:
    package_location = Path(dridh.__file__).resolve().parent
    return "\n".join(
        [
            f"dridh {dridh.__version__}",
            f"Python: {platform.python_version()}",
            f"Python executable: {sys.executable}",
            f"Bridge protocol: {dridh.BRIDGE_PROTOCOL_VERSION}",
            f"Runtime location: {package_location}",
        ]
    )


def _project(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return discover_workspace_root(Path.cwd())


def _workspace(project: str):  # type: ignore[no-untyped-def]
    return open_workspace(_project(project))


def _operator() -> str:
    return f"cli:{getpass.getuser()}"


def _confirm(expected: str, summary: str) -> None:
    """Require a human to type ``expected`` on a terminal. Scripts and agents cannot pass this."""

    if not sys.stdin.isatty():
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "Approval needs an interactive terminal. Scripts, pipes and agents cannot approve.",
            recovery="Run the command in a terminal and type the confirmation shown.",
        )
    sys.stderr.write(summary + "\n")
    sys.stderr.write(f"Type exactly: {expected}\n> ")
    sys.stderr.flush()
    typed = sys.stdin.readline().strip()
    if typed != expected:
        raise DridhError(
            ErrorCode.APPROVAL_REQUIRED,
            "The confirmation did not match. Nothing was changed.",
        )


def _plan_summary(status: dict[str, Any]) -> str:
    lines = [
        f"Workflow {status['workflow_id']} is {status['state']}.",
        f"Objective: {status['objective']}",
        f"Plan SHA-256: {status['plan']['sha256']}",
    ]
    for task in status["plan"]["tasks"]:
        depends = ", ".join(task["depends_on"]) or "none"
        lines.append(f"  {task['id']} [{task['agent']}] depends on: {depends} - {task['title']}")
        lines.append(f"      instructions: {task['instructions']}")
        lines.append(f"      allowed paths: {', '.join(task['allowed_paths'])}")
        for check in task["acceptance"]:
            lines.append(f"      acceptance: {check['type']} {check.get('path') or ' '.join(check.get('argv', []))}".rstrip())
    for check in status["plan"].get("acceptance", []):
        lines.append(f"  workflow acceptance: {check['type']} {check.get('path') or ' '.join(check.get('argv', []))}".rstrip())
    return "\n".join(lines)


@app.callback()
def bootstrap(
    context: typer.Context,
    version: bool = typer.Option(False, "--version", "-v", help="Show package, runtime and bridge protocol versions.", is_eager=True),
) -> None:
    if version:
        typer.echo(_version_text())
        raise typer.Exit()


@app.command("bridge")
def bridge_command() -> None:
    """Read one JSON request from stdin and write exactly one JSON response (used by the OpenCode plugin)."""

    from dridh.common.bridge import run_bridge

    raise typer.Exit(code=run_bridge())


MODE_OPTION = typer.Option(
    None,
    "--mode",
    help="Workspace mode: 'new' (new codebase) or 'existing' (enhancing an existing repository). "
    "Asked interactively when omitted in a terminal; required for non-interactive use when creating a workspace.",
)


def _resolve_init_mode(root: Path, mode: str | None) -> tuple[str | None, str]:
    """Return (mode, source). Creating a workspace never silently guesses the mode."""

    from dridh.workspace.layout import WORKSPACE_MODES, suggest_mode, workspace_exists

    if mode is not None and mode not in WORKSPACE_MODES:
        raise typer.BadParameter(f"must be one of: {', '.join(WORKSPACE_MODES)}", param_hint="--mode")
    if workspace_exists(root):
        return mode, "existing-workspace"
    if mode is not None:
        return mode, "flag"
    if not sys.stdin.isatty():
        raise typer.BadParameter(
            "this creates a new workspace, so state whether the project is a new codebase or an existing repository: "
            "--mode new|existing",
            param_hint="--mode",
        )
    suggestion = suggest_mode(root)
    chosen = typer.prompt(
        f"Is this a new codebase or an existing repository? [{'/'.join(WORKSPACE_MODES)}] (suggested from the folder: {suggestion})",
        default=suggestion,
        value_proc=_parse_mode_answer,
    )
    return chosen, "prompt"


def _parse_mode_answer(answer: str) -> str:
    """Accept 'new'/'existing' (any case); an invalid answer makes the prompt ask again."""

    from dridh.workspace.layout import WORKSPACE_MODES

    value = str(answer).strip().lower()
    if value not in WORKSPACE_MODES:
        raise typer.BadParameter(f"answer one of: {', '.join(WORKSPACE_MODES)}")
    return value


@app.command("init")
def init_command(project: str = PROJECT_OPTION, mode: str | None = MODE_OPTION) -> None:
    """Create the project-local workspace under .opencode/dridh/ (idempotent)."""

    root = _project(project)
    resolved, source = _resolve_init_mode(root, mode)
    info, created = init_workspace(root, mode=resolved)
    if not created and mode is not None and mode != info.mode:
        raise DridhError(
            ErrorCode.CONFIGURATION_CONFLICT,
            f"This workspace was created in '{info.mode}' mode; it was not changed to '{mode}'.",
            recovery="Keep the existing mode, or remove .opencode/dridh/ deliberately and run init again.",
        )
    with StateStore(info.state_db, info.workspace_id):
        pass
    _emit({"created": created, "mode_source": source, "workspace": info.to_dict()})


COORDINATOR_AGENT = "dridh/coordinator"


@app.command("start")
def start_command(
    project: str | None = PROJECT_OPTION,
    prompt: str | None = typer.Option(None, "--prompt", help="First message for the Coordinator (for example the feature to build)."),
    config_dir: str | None = CONFIG_DIR_OPTION,
    mode: str | None = MODE_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run", help="Run every check and show what would be launched, without launching OpenCode."),
) -> None:
    """Open OpenCode in this workspace with the Dridh Coordinator selected. Your OpenCode defaults are not changed."""

    import os
    import subprocess

    from dridh.diagnostics import FAIL, check_credentials, check_opencode, check_runtime

    root = _project(project)
    if not workspace_exists(root):
        if not sys.stdin.isatty():
            raise DridhError(ErrorCode.WORKSPACE_NOT_FOUND, f"{root} has no Dridh workspace.",
                             recovery="Run `dridh init --mode new|existing` there first, or run `dridh start` in a terminal to be asked.")
        if not typer.confirm(f"{root} has no Dridh workspace. Create one now?", default=True, err=True):
            raise typer.Exit(EXIT_USAGE)
        resolved, _ = _resolve_init_mode(root, mode)
        init_workspace(root, mode=resolved)
    info, store = _open_state(str(root))  # refuses an unmigrated or unreadable state
    store.close()

    env = dict(os.environ)
    if config_dir:
        env["OPENCODE_CONFIG_DIR"] = str(Path(config_dir).expanduser().resolve())
    checks = [check_opencode(env, _diagnostic_run), *check_runtime(env.get("OPENCODE_CONFIG_DIR"))]
    failed = [c for c in checks if c.status == FAIL]
    if failed:
        raise DridhError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Dridh cannot start: " + "; ".join(f"{c.title}: {c.detail}" for c in failed),
                         recovery=next((c.remedy for c in failed if c.remedy), "Run `dridh doctor` for details."))
    credentials = check_credentials(env)

    # Select the Coordinator for this process only: OPENCODE_CONFIG_CONTENT layers over the user's configuration and is
    # never written anywhere, so the global default_agent stays as it is.
    layered: dict[str, Any] = {}
    if env.get("OPENCODE_CONFIG_CONTENT"):
        try:
            layered = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        except ValueError:
            raise DridhError(ErrorCode.CONFIGURATION_ERROR, "OPENCODE_CONFIG_CONTENT is set but is not valid JSON.",
                             recovery="Unset it or fix it, then run `dridh start` again.") from None
    layered["default_agent"] = COORDINATOR_AGENT
    models = _workspace_models(info)
    if models.get("provider"):
        layered.setdefault("model", f"{models['provider']}/{models['default']}")
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(layered, sort_keys=True)

    executable = env.get("DRIDH_OPENCODE_BIN") or "opencode"
    env["PWD"] = str(root)  # OpenCode trusts $PWD over the real working directory
    argv = [executable, str(root)] + (["--prompt", prompt] if prompt else [])
    features = _lifecycle(str(root)).list_features()
    summary = {
        "workspace": str(root),
        "mode": info.mode,
        "features": [{k: f[k] for k in ("slug", "stage", "status")} for f in features],
        "agent": COORDINATOR_AGENT,
        "model": layered.get("model") or f"{models.get('default')} (provider not set in dridh.yaml; OpenCode chooses)",
        "credentials": credentials.detail,
        "argv": argv,
        "session_id": "not recorded: the interactive OpenCode interface does not report it; use `dridh session start` for a recorded session",
    }
    if dry_run:
        _emit({"dry_run": True, **summary})
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise DridhError(ErrorCode.VALIDATION_FAILED, "`dridh start` opens the interactive OpenCode interface and needs a terminal.",
                         recovery="Use `dridh session start \"<message>\"` for scripted runs, or `dridh start --dry-run` to check.")
    sys.stderr.write(f"Starting OpenCode in {root} with {COORDINATOR_AGENT} selected. Your default agent is not changed.\n")
    for f in features:
        sys.stderr.write(f"  feature {f['slug']}: {f['stage']} ({f['status']})\n")
    if not features:
        sys.stderr.write("  no features yet: tell the Coordinator what you want to build.\n")
    if credentials.status != "PASS":
        sys.stderr.write(f"  warning: {credentials.detail} {credentials.remedy}\n")
    raise typer.Exit(subprocess.run(argv, cwd=str(root), env=env, check=False).returncode)  # noqa: S603 - fixed argv, no shell


def _diagnostic_run(argv: list[str]):  # type: ignore[no-untyped-def]
    import subprocess

    return subprocess.run(argv, capture_output=True, text=True, timeout=8.0, check=False, shell=False)  # noqa: S603


def _workspace_models(info: Any) -> dict[str, Any]:
    from dridh.config.dridh_yaml import CONFIG_FILENAME, load_config

    try:
        return load_config(info.root / CONFIG_FILENAME).models.model_dump()
    except (OSError, ValueError):
        return {}


@app.command("migrate")
def migrate_command(project: str = PROJECT_OPTION) -> None:
    """Migrate the project workspace state to the current schema version."""

    from dridh.state.migration import migrate_to_current
    from dridh.workspace.layout import open_workspace

    workspace = open_workspace(_project(project))
    _emit(migrate_to_current(workspace.state_db))


@app.command("install")
def install_command(
    config_dir: str | None = CONFIG_DIR_OPTION,
    config_file: str | None = CONFIG_FILE_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
    strip_comments: bool = typer.Option(False, "--strip-comments", help="Allow a rewrite that drops JSONC comments. A backup is written first."),
    force: bool = FORCE_OPTION,
) -> None:
    """Install the Dridh plugin, agent profiles and plugins entry into the OpenCode configuration."""

    layout = resolve_layout(config_dir, config_file)
    plan = plan_install(
        layout,
        plugin_source=default_plugin_source(),
        agent_source=default_agent_source(),
        strip_comments=strip_comments,
    )
    if dry_run:
        _emit({"mode": "dry-run", "changed": bool(plan.changes), "plan": plan.to_dict()})
        return
    result = apply_install(plan, force=force)
    _emit({"mode": "apply", **result, "plan": plan.to_dict()})


@app.command("upgrade")
def upgrade_command(
    config_dir: str | None = CONFIG_DIR_OPTION,
    config_file: str | None = CONFIG_FILE_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
    strip_comments: bool = typer.Option(False, "--strip-comments", help="Allow a rewrite that drops JSONC comments. A backup is written first."),
    force: bool = FORCE_OPTION,
) -> None:
    """Update an existing Dridh installation to this version, keeping your OpenCode configuration.

    Idempotent. Only files recorded as Dridh-owned are updated or removed; a managed file you edited is
    reported as a conflict and replaced only with --force (the original is backed up first).
    """

    from dridh.install.installer import _load_manifest

    layout = resolve_layout(config_dir, config_file)
    previous = _load_manifest(layout)
    if previous is None:
        typer.echo(
            json.dumps({"ok": False, "error": {"code": "NOT_INSTALLED", "message": "Dridh is not installed in this OpenCode configuration.",
                                               "recovery": "Run `dridh install` first (use --dry-run to preview)."}}),
            err=True,
        )
        raise typer.Exit(EXIT_PREREQUISITE_MISSING)
    plan = plan_install(
        layout,
        plugin_source=default_plugin_source(),
        agent_source=default_agent_source(),
        strip_comments=strip_comments,
    )
    versions = {"from_version": previous.get("dridh_version"), "to_version": dridh.__version__}
    if dry_run:
        _emit({"mode": "dry-run", **versions, "changed": bool(plan.changes), "plan": plan.to_dict()})
        return
    result = apply_install(plan, force=force)
    _emit({"mode": "apply", **versions, **result, "plan": plan.to_dict()})


@app.command("uninstall")
def uninstall_command(
    config_dir: str | None = CONFIG_DIR_OPTION,
    config_file: str | None = CONFIG_FILE_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
    force: bool = FORCE_OPTION,
) -> None:
    """Remove the Dridh runtime. Restores the original configuration when it is unchanged."""

    layout = resolve_layout(config_dir, config_file)
    plan = plan_uninstall(layout)
    if dry_run:
        _emit({"mode": "dry-run", "plan": plan.to_dict()})
        return
    result = apply_uninstall(plan, force=force)
    _emit({"mode": "apply", **result})


@app.command("status")
def status_command(config_dir: str | None = CONFIG_DIR_OPTION, project: str | None = typer.Option(None, "--workspace", "--project")) -> None:
    """Report the shared runtime and, when given, the project workspace."""

    report: dict[str, Any] = {"runtime": runtime_status(resolve_layout(config_dir, None))}
    if project is not None:
        try:
            info = _workspace(project)
            report["workspace"] = {"initialised": True, **info.to_dict()}
        except DridhError as exc:
            if exc.code is not ErrorCode.WORKSPACE_NOT_FOUND:
                raise
            report["workspace"] = {"initialised": False}
    _emit(report)


@app.command("doctor")
def doctor_command(
    config_dir: str | None = CONFIG_DIR_OPTION,
    project: str | None = typer.Option(None, "--workspace", "--project", help="Also check this project's workspace."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check the environment and installation (read-only). Exits 3 when a mandatory prerequisite is missing."""

    from dridh.diagnostics import render_text, run_checks, summarize

    checks = run_checks(config_dir=config_dir, project=_project(project) if project is not None else None)
    summary = summarize(checks)
    if as_json:
        _emit({**summary, "checks": [check.to_dict() for check in checks]})
    else:
        typer.echo(render_text(checks))
    if not summary["ok"]:
        raise typer.Exit(EXIT_PREREQUISITE_MISSING)


@app.command("index")
def index_command(
    project: str = PROJECT_OPTION,
    status: bool = typer.Option(False, "--status", help="Show the current index instead of updating it."),
    no_embed: bool = typer.Option(False, "--no-embed", help="Skip semantic embeddings (full-text and call graph only)."),
    progress: bool = typer.Option(False, "--progress", help="Report progress on stderr as JSON lines (for the OpenCode TUI)."),
    if_changed: bool = typer.Option(False, "--if-changed", help="Only when files changed since the last index; updates the folder that index was built from."),
) -> None:
    """Build or incrementally update the Code Reacher index of this project (only changed files are re-parsed)."""

    from dridh.code_reacher.indexer import CodeIndexer, index_freshness
    from dridh.code_reacher.search import CodeSearch

    from dridh.code_reacher.mapping import refresh_mapping

    workspace = _workspace(project)
    if status:
        _emit(CodeSearch(workspace).status())
        return
    root = None
    if if_changed:
        freshness = index_freshness(workspace)
        if not freshness["changed"]:
            _emit({"skipped": True, "reason": "no files changed since the last index" if freshness["indexed"] else "nothing is indexed yet",
                   "files_seen": 0, "files_parsed": 0})
            return
        root = Path(freshness["folder"])
    result = CodeIndexer(workspace).index(embed=not no_embed, root=root, progress=_progress_reporter() if progress else None)
    result["story_mapping"] = refresh_mapping(workspace)
    _emit(result)


def _progress_reporter():  # type: ignore[no-untyped-def]
    """Progress as JSON lines on stderr, at most about five a second per phase; the start and end of a phase always."""
    import json
    import time

    last: dict[str, float] = {}

    def report(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if done not in (0, total) and now - last.get(phase, 0.0) < 0.2:
            return
        last[phase] = now
        sys.stderr.write(json.dumps({"progress": phase, "done": done, "total": total}) + "\n")
        sys.stderr.flush()

    return report


def _code_search(project: str):  # type: ignore[no-untyped-def]
    from dridh.code_reacher.search import CodeSearch

    return CodeSearch(_workspace(project))


@code_app.command("search")
def code_search_command(
    query: str,
    project: str = PROJECT_OPTION,
    limit: int = typer.Option(10, "--limit", min=1, max=50),
    no_semantic: bool = typer.Option(False, "--no-semantic", help="Full-text only."),
) -> None:
    """Find code by name, words in its name or meaning (full-text and, when available, semantic)."""

    _emit(_code_search(project).lookup(query, limit=limit, semantic=not no_semantic))


@code_app.command("symbol")
def code_symbol_command(name: str, project: str = PROJECT_OPTION) -> None:
    """Show a class, function or method by its (qualified) name, with parameters and endpoints."""

    _emit(_code_search(project).symbol(name))


@code_app.command("deps")
def code_deps_command(
    symbol: str,
    project: str = PROJECT_OPTION,
    direction: str = typer.Option("both", "--direction", help="callers, callees or both."),
    depth: int = typer.Option(2, "--depth", min=1, max=5),
) -> None:
    """Who calls a symbol and what it calls. Calls that cannot be matched to one definition are listed as unresolved."""

    if direction not in ("callers", "callees", "both"):
        raise typer.BadParameter("must be callers, callees or both", param_hint="--direction")
    _emit(_code_search(project).dependencies(symbol, direction=direction, depth=depth))


@code_app.command("map")
def code_map_command(project: str = PROJECT_OPTION) -> None:
    """Rebuild the story-to-code mapping from story file ownership and the files each story's passing attempt changed."""

    from dridh.code_reacher.mapping import StoryCodeMapper

    _emit(StoryCodeMapper(_workspace(project)).rebuild())


@code_app.command("for")
def code_for_command(target: str = typer.Argument(..., help="A story (ST-1.1), component (CMP-01), use case (UC-01) or flow (SF-01.1)."),
                     project: str = PROJECT_OPTION) -> None:
    """The indexed code that implements a story, component, use case or flow, with the evidence for each link."""

    from dridh.code_reacher.mapping import StoryCodeMapper

    _emit(StoryCodeMapper(_workspace(project)).code_for(target))


@code_app.command("owners")
def code_owners_command(path: str = typer.Argument(..., help="Project-relative file path."), project: str = PROJECT_OPTION) -> None:
    """Which stories, components, use cases and flows a file's code is mapped to."""

    from dridh.code_reacher.mapping import StoryCodeMapper

    _emit(StoryCodeMapper(_workspace(project)).targets_for(path))


@code_app.command("endpoints")
def code_endpoints_command(query: str | None = typer.Argument(None), project: str = PROJECT_OPTION) -> None:
    """List HTTP endpoints found in the code (FastAPI, Flask, Express, NestJS, Spring)."""

    _emit(_code_search(project).endpoints(query))


def _lifecycle(project: str):  # type: ignore[no-untyped-def]
    from dridh.coordinator.lifecycle import FeatureLifecycle

    return FeatureLifecycle(_workspace(project))


def _only_feature(lifecycle, reference: str | None) -> str:  # type: ignore[no-untyped-def]
    """A feature reference, or the workspace's only feature when none is given."""

    if reference:
        return reference
    features = lifecycle.list_features()
    if len(features) == 1:
        return features[0]["slug"]
    raise typer.BadParameter(
        "name the feature (this workspace has " + ("none yet" if not features else ", ".join(item["slug"] for item in features)) + ")",
        param_hint="FEATURE",
    )


@feature_app.command("create")
def feature_create(
    name: str,
    project: str = PROJECT_OPTION,
    mode: str | None = typer.Option(None, "--mode", help="new or existing; defaults to the workspace mode."),
) -> None:
    """Create a feature at the start of the lifecycle."""

    _emit({"feature": _lifecycle(project).create_feature(name, actor=_operator(), mode=mode)})


@feature_app.command("list")
def feature_list(project: str = PROJECT_OPTION) -> None:
    """List this workspace's features and their stages."""

    _emit({"features": [{k: item[k] for k in ("slug", "name", "mode", "stage", "status")} for item in _lifecycle(project).list_features()]}, "feature-list")


@feature_app.command("check-delivery")
def feature_check_delivery(project: str = PROJECT_OPTION) -> None:
    """Can the feature leave execution? Every approved flow proven by a passing test or explained in the approved ledger."""
    from dridh.coordinator.delivery_check import delivery_readiness

    _emit(delivery_readiness(_workspace(project)))


@feature_app.command("status")
def feature_status(feature: str | None = typer.Argument(None), project: str = PROJECT_OPTION) -> None:
    """Show the stage, what blocks the next step and the next valid actions."""

    lifecycle = _lifecycle(project)
    _emit(lifecycle.status(_only_feature(lifecycle, feature)), "feature-status")


@feature_app.command("sync")
def feature_sync(feature: str | None = typer.Argument(None), project: str = PROJECT_OPTION) -> None:
    """Reopen any review whose approval went stale, then advance as far as the evidence allows."""

    lifecycle = _lifecycle(project)
    _emit(lifecycle.sync(_only_feature(lifecycle, feature), actor=_operator()))


@feature_app.command("advance")
def feature_advance(feature: str | None = typer.Argument(None), project: str = PROJECT_OPTION) -> None:
    """Move one step forward; refused unless the current stage's exit evidence is satisfied."""

    lifecycle = _lifecycle(project)
    _emit(lifecycle.advance(_only_feature(lifecycle, feature), actor=_operator()))


@feature_app.command("reopen")
def feature_reopen(
    to: str = typer.Option(..., "--to", help="An earlier stage, for example IDEATION."),
    reason: str = typer.Option(..., "--reason"),
    feature: str | None = typer.Argument(None),
    project: str = PROJECT_OPTION,
) -> None:
    """Send a feature back to an earlier stage (recorded with the reason)."""

    lifecycle = _lifecycle(project)
    _emit({"feature": lifecycle.reopen(_only_feature(lifecycle, feature), to_stage=to.upper(), reason=reason, actor=_operator())})


@feature_app.command("block")
def feature_block(reason: str = typer.Option(..., "--reason"), feature: str | None = typer.Argument(None), project: str = PROJECT_OPTION) -> None:
    """Mark a feature blocked; it cannot advance until unblocked."""

    lifecycle = _lifecycle(project)
    _emit({"feature": lifecycle.block(_only_feature(lifecycle, feature), reason=reason, actor=_operator())})


@feature_app.command("unblock")
def feature_unblock(reason: str = typer.Option("Unblocked.", "--reason"), feature: str | None = typer.Argument(None), project: str = PROJECT_OPTION) -> None:
    """Clear a block."""

    lifecycle = _lifecycle(project)
    _emit({"feature": lifecycle.unblock(_only_feature(lifecycle, feature), reason=reason, actor=_operator())})


@feature_app.command("bind")
def feature_bind(
    kind: str = typer.Option(..., "--kind", help="ideator, designer or planner."),
    session: str = typer.Option(..., "--session", help="The stage session ID."),
    feature: str | None = typer.Argument(None),
    project: str = PROJECT_OPTION,
) -> None:
    """Bind a stage session to the feature (designer and planner sessions are normally discovered)."""

    if kind not in ("ideator", "designer", "planner"):
        raise typer.BadParameter("must be ideator, designer or planner", param_hint="--kind")
    lifecycle = _lifecycle(project)
    _emit({"feature": lifecycle.bind_session(_only_feature(lifecycle, feature), f"{kind}_session_id", session, actor=_operator())})


@feature_app.command("complete")
def feature_complete(
    reviewer: str = typer.Option(..., "--reviewer", help="Your name, recorded as attribution (not authentication)."),
    feature: str | None = typer.Argument(None),
    project: str = PROJECT_OPTION,
) -> None:
    """Complete a feature in DELIVERY_REVIEW. Human only: needs an interactive terminal and a typed confirmation."""

    lifecycle = _lifecycle(project)
    reference = _only_feature(lifecycle, feature)
    current = lifecycle.status(reference)["feature"]
    _confirm(f"COMPLETE {current['slug']}", f"Complete feature '{current['name']}' (currently {current['stage']}). This is recorded under your name: {reviewer}.")
    _emit(lifecycle.complete(reference, reviewer=reviewer, human_confirmed=True))


@session_app.command("start")
def session_start(message: str, project: str = PROJECT_OPTION) -> None:
    """Start a new Coordinator session for this project and record it."""

    service = SessionService(_workspace(project), actor=_operator())
    _emit(asyncio.run(service.start(message)))


@session_app.command("resume")
def session_resume(
    session_id: str,
    message: str,
    project: str = PROJECT_OPTION,
    fresh_if_unsafe: bool = typer.Option(False, "--fresh-if-unsafe", help="Start a recorded fresh session when the stored session cannot safely be resumed."),
) -> None:
    """Continue a registered Coordinator session. Refuses unknown or mismatched sessions."""

    service = SessionService(_workspace(project), actor=_operator())
    _emit(asyncio.run(service.resume(session_id, message, fresh_if_unsafe=fresh_if_unsafe)))


def _engine(project: str) -> WorkflowEngine:
    return WorkflowEngine(_workspace(project), actor=_operator())


@workflow_app.command("create", hidden=True)
def workflow_create(objective: str, project: str = PROJECT_OPTION) -> None:
    """Retired: use the feature lifecycle (`dridh start`, `dridh planner`, `dridh execute`)."""
    from dridh.orchestration.legacy import require_legacy

    require_legacy("`dridh workflow create`")
    _emit(_engine(project).create(objective))


@workflow_app.command("plan", hidden=True)
def workflow_plan(workflow_id: str, project: str = PROJECT_OPTION) -> None:
    """Retired: use the Planner stage and `dridh execute`."""
    from dridh.orchestration.legacy import require_legacy

    require_legacy("`dridh workflow plan`")
    _emit(asyncio.run(_engine(project).plan(workflow_id)))


@workflow_app.command("status")
def workflow_status(workflow_id: str, project: str = PROJECT_OPTION) -> None:
    _emit(_engine(project).status(workflow_id))


@workflow_app.command("approve")
def workflow_approve(workflow_id: str, project: str = PROJECT_OPTION) -> None:
    """Approve the reviewed plan. Requires a terminal and typing the confirmation shown."""

    engine = _engine(project)
    status = engine.status(workflow_id)
    if status["state"] != "AWAITING_APPROVAL":
        raise DridhError(
            ErrorCode.INVALID_TRANSITION,
            "Only a plan awaiting approval can be approved.",
            details=(f"workflow: {workflow_id}", f"state: {status['state']}"),
        )
    sha = status["plan"]["sha256"]
    expected = f"APPROVE {workflow_id} {sha[:12]}"
    _confirm(expected, _plan_summary(status))
    reviewer = f"human:{getpass.getuser()}"
    _emit(engine.approve(workflow_id, reviewer=reviewer, method="terminal-confirmation", expected_plan_sha=sha))


@workflow_app.command("reject")
def workflow_reject(workflow_id: str, reason: str = typer.Option(..., "--reason", help="Why the plan is rejected."), project: str = PROJECT_OPTION) -> None:
    """Reject the reviewed plan and return the workflow to planning."""

    engine = _engine(project)
    status = engine.status(workflow_id)
    if status["state"] != "AWAITING_APPROVAL":
        raise DridhError(
            ErrorCode.INVALID_TRANSITION,
            "Only a plan awaiting approval can be rejected.",
            details=(f"workflow: {workflow_id}", f"state: {status['state']}"),
        )
    expected = f"REJECT {workflow_id} {status['plan']['sha256'][:12]}"
    _confirm(expected, _plan_summary(status))
    reviewer = f"human:{getpass.getuser()}"
    _emit(engine.reject(workflow_id, reviewer=reviewer, method="terminal-confirmation", reason=reason))


@workflow_app.command("run")
def workflow_run(
    workflow_id: str,
    project: str = PROJECT_OPTION,
    parallel: bool = typer.Option(False, "--parallel", help="Run ready tasks in parallel up to --concurrency."),
    concurrency: int = typer.Option(2, "--concurrency", help="Maximum concurrent workers when --parallel is set."),
) -> None:
    """Execute the approved tasks until the workflow finishes."""

    engine = _engine(project)
    if parallel:
        _emit(asyncio.run(engine.run_parallel(workflow_id, concurrency=concurrency)))
    else:
        _emit(asyncio.run(engine.run_to_end(workflow_id)))


@workflow_app.command("clarify")
def workflow_clarify(
    workflow_id: str,
    question: str = typer.Option(..., "--question", help="The clarification question."),
    answer: str = typer.Option(None, "--answer", help="The user's answer (optional)."),
    source: str = typer.Option("user", "--source", help="Source of the clarification."),
    project: str = PROJECT_OPTION,
) -> None:
    """Record a requirement clarification for a workflow."""

    _emit(_engine(project).record_clarification(workflow_id, question, answer, source=source))


@workflow_app.command("qa")
def workflow_qa(
    workflow_id: str,
    task_id: str = typer.Option(..., "--task", help="Task being reviewed."),
    reviewer: str = typer.Option(..., "--reviewer", help="Reviewer identity."),
    verdict: str = typer.Option(..., "--verdict", help="PASS, FAIL, or WAIVED."),
    detail: str = typer.Option("", "--detail", help="Review detail."),
    project: str = PROJECT_OPTION,
) -> None:
    """Record an independent QA/review result for a task."""

    _emit(_engine(project).record_qa_result(workflow_id, task_id, reviewer, verdict, detail))


@workflow_app.command("qa-summary")
def workflow_qa_summary(workflow_id: str, project: str = PROJECT_OPTION) -> None:
    """Show QA results for a workflow."""

    _emit(_engine(project).qa_summary(workflow_id))

@workflow_app.command("check-integration")
def workflow_check_integration(
    workflow_id: str,
    task_id: str = typer.Option(..., "--task", help="Task to check integration eligibility."),
    project: str = PROJECT_OPTION,
) -> None:
    """Check if a task is eligible for integration based on QA results."""

    _emit(_engine(project).check_integration_eligible(workflow_id, task_id))


@workflow_app.command("report")
def workflow_report(workflow_id: str, project: str = PROJECT_OPTION) -> None:
    _emit(_engine(project).report(workflow_id))


@workflow_app.command("cancel")
def workflow_cancel(workflow_id: str, reason: str = typer.Option("cancelled by the operator", "--reason"), project: str = PROJECT_OPTION) -> None:
    _emit(_engine(project).cancel(workflow_id, reason=reason))


@workflow_app.command("verify-ledger")
def workflow_verify_ledger(project: str = PROJECT_OPTION) -> None:
    result = _engine(project).verify_ledger()
    _emit(result)
    if not result["ok"]:
        raise typer.Exit(code=1)


contracts_app = typer.Typer(help="Inspect and verify packaged, versioned document contracts.", no_args_is_help=True)
validate_app = typer.Typer(help="Run deterministic validators on canonical documents. Read-only: nothing is written.", no_args_is_help=True)
ledger_app = typer.Typer(help="Record human Ledger decisions. Requires an interactive terminal.", no_args_is_help=True)
app.add_typer(contracts_app, name="contracts")
app.add_typer(validate_app, name="validate")
app.add_typer(ledger_app, name="ledger")


@contracts_app.command("verify")
def contracts_verify(
    version: str = typer.Option("2.1.0", "--version", help="Versioned standards/template bundle to verify."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable contract metadata."),
) -> None:
    """Verify packaged standard and template hashes against their manifest."""

    try:
        bundle = load_contract_bundle(version)
    except ContractBundleError as exc:
        payload = {"ok": False, "error": str(exc), "version": version}
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(f"FAIL  contract bundle {version}: {exc}")
        raise typer.Exit(code=1) from exc
    payload = {
        "standards_version": bundle.version,
        "template_version": bundle.version,
        "standards_sha256": bundle.standards_sha256,
        "template_sha256": bundle.template_sha256,
        "files": {key: value.get("file") for key, value in bundle.manifest.items() if isinstance(value, dict)},
    }
    if json_output:
        typer.echo(json.dumps({"ok": True, "contracts": payload}, indent=2, sort_keys=True))
    else:
        typer.echo(f"VERIFIED  document contracts {bundle.version}")
        typer.echo(f"  TABLE-STANDARDS.md  sha256:{bundle.standards_sha256}")
        typer.echo(f"  Tech-Design-Template.md  sha256:{bundle.template_sha256}")


@contracts_app.command("show")
def contracts_show(
    version: str = typer.Option("2.1.0", "--version", help="Versioned standards/template bundle to display."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable contract metadata."),
) -> None:
    """Show verified versions, provenance and hashes for the packaged contract bundle."""

    try:
        bundle = load_contract_bundle(version)
    except ContractBundleError as exc:
        payload = {"ok": False, "error": str(exc), "version": version}
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(f"FAIL  contract bundle {version}: {exc}")
        raise typer.Exit(code=1) from exc
    payload = {
        "manifest": bundle.manifest,
        "standards_version": bundle.version,
        "template_version": bundle.version,
        "standards_sha256": bundle.standards_sha256,
        "template_sha256": bundle.template_sha256,
    }
    if json_output:
        typer.echo(json.dumps({"ok": True, "contracts": payload}, indent=2, sort_keys=True))
    else:
        typer.echo(f"TABLE-STANDARDS.md {bundle.version}  sha256:{bundle.standards_sha256}")
        typer.echo(f"Tech-Design-Template.md {bundle.version}  sha256:{bundle.template_sha256}")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
JSON_OPTION = typer.Option(False, "--json", help="Emit machine-readable findings as JSON.")
PATH_ARGUMENT = typer.Argument(..., help="Canonical document to validate.")
PATHS_ARGUMENT = typer.Argument(..., help="One or more canonical documents.")


def _finish_validation(
    command: str,
    paths: list[Path],
    result: ValidationResult,
    *,
    json_output: bool,
    status: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Print findings and exit: 0 PASS, 1 FAIL, 2 BLOCKED or usage error. Stable across commands."""

    resolved = status or ("PASS" if result.ok else "FAIL")
    exit_code = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL}.get(resolved, EXIT_USAGE)
    if json_output:
        document: dict[str, Any] = {
            "schema": "dridh.validation/1",
            "command": f"validate {command}",
            "paths": [str(p) for p in paths],
            "status": resolved,
            "ok": resolved == "PASS",
            "findings": [finding.to_dict() for finding in result.findings],
        }
        if details:
            document["details"] = details
        typer.echo(json.dumps(document, indent=2, sort_keys=True, default=str))
    else:
        typer.echo(f"{resolved}  dridh validate {command}  {', '.join(str(p) for p in paths)}")
        for finding in result.findings:
            typer.echo(f"  {finding.severity}  {finding.render()}")
            if finding.expected is not None:
                typer.echo(f"      expected: {finding.expected}")
            if finding.actual is not None:
                typer.echo(f"      actual:   {finding.actual}")
            if finding.repair_hint:
                typer.echo(f"      hint:     {finding.repair_hint}")
        if details and "sanitization" in details:
            typer.echo(f"  sanitisation: {len(details['sanitization'])} transformation(s) applied in memory; the file was not modified")
    raise typer.Exit(code=exit_code)


def _read_input(command: str, path: str, json_output: bool) -> str:
    target = Path(path)
    if not target.is_file():
        _finish_validation(
            command,
            [target],
            ValidationResult([Finding("FILE_NOT_FOUND", f"File not found: {target}", file=str(target))]),
            json_output=json_output,
            status="USAGE_ERROR",
        )
    return target.read_text(encoding="utf-8")


def _ledger_decisions_for(project: str) -> dict[str, LedgerDecision] | None:
    """Persisted decisions for the workspace, or None when no workspace state exists."""

    try:
        workspace = _workspace(project)
    except DridhError:
        return None
    if not workspace.state_db.exists():
        return None
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        return dict(load_ledger_decisions(store.connection))


def _single(command: str, path: str, validator: Any, json_output: bool) -> None:
    content = _read_input(command, path, json_output)
    result = validator(content, file=Path(path).name)
    _finish_validation(command, [Path(path)], result, json_output=json_output)


@validate_app.command("use-cases")
def validate_use_cases_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate 001-USE-CASES.md: Tables 1A, 1, 1B and UC sections."""

    _single("use-cases", path, validate_use_cases, json_output)


@validate_app.command("tdd")
def validate_tdd_command(
    path: str = PATH_ARGUMENT,
    sanitize: bool = typer.Option(False, "--sanitize", help="Validate the sanitised text (comments and aside callouts removed). The file is never modified."),
    json_output: bool = JSON_OPTION,
) -> None:
    """Validate a Technical Design Document against Tech-Design-Template 2.1.0."""

    content = _read_input("tdd", path, json_output)
    details: dict[str, Any] | None = None
    if sanitize:
        content, actions = sanitize_tdd(content)
        details = {"sanitization": actions}
    result = validate_tdd(content, file=Path(path).name)
    _finish_validation("tdd", [Path(path)], result, json_output=json_output, details=details)


@validate_app.command("central-plan")
def validate_central_plan_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate CENTRAL-PLAN-INDEX.md: Tables 2, 9 and 10 and the story graph."""

    _single("central-plan", path, validate_central_plan, json_output)


@validate_app.command("component-master")
def validate_component_master_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate COMPONENT-MASTER.md: Tables 4 and 5 and CDD definitions."""

    _single("component-master", path, validate_component_master, json_output)


@validate_app.command("phase-files")
def validate_phase_files_command(paths: list[str] = PATHS_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate phase files: every story heading needs a ten-row Table 3 story card."""

    findings: list[Finding] = []
    for path in paths:
        content = _read_input("phase-files", path, json_output)
        findings += validate_phase_file(content, file=Path(path).name).findings
    _finish_validation("phase-files", [Path(p) for p in paths], ValidationResult(findings), json_output=json_output)


@validate_app.command("coordinator")
def validate_coordinator_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate COORDINATOR.md: Tables 6, 7, 8 and 12."""

    _single("coordinator", path, validate_coordinator, json_output)


@validate_app.command("qa-log")
def validate_qa_log_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate a Q&A log (Table 11)."""

    _single("qa-log", path, validate_qa_log, json_output)


@validate_app.command("ledger")
def validate_ledger_command(
    path: str = PATH_ARGUMENT,
    project: str = PROJECT_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Validate LEDGER.md (Table 14). Deviations pass only with a persisted human approval for the current entry."""

    content = _read_input("ledger", path, json_output)
    result = validate_ledger(content, file=Path(path).name, decisions=_ledger_decisions_for(project))
    _finish_validation("ledger", [Path(path)], result, json_output=json_output)


@validate_app.command("test-plan")
def validate_test_plan_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate TEST-PLAN.md (Table 15)."""

    _single("test-plan", path, validate_test_plan, json_output)


@validate_app.command("story-dag")
def validate_story_dag_command(path: str = PATH_ARGUMENT, json_output: bool = JSON_OPTION) -> None:
    """Validate only the story dependency graph in Table 2: unknown edges, cycles, order and Unlocks."""

    _single("story-dag", path, validate_story_graph, json_output)


@validate_app.command("cross-refs")
def validate_cross_refs_command(
    paths: list[str] = PATHS_ARGUMENT,
    project: str = PROJECT_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Resolve references across documents. Each reference is checked against its owning document."""

    documents: dict[str, str] = {}
    for path in paths:
        name = Path(path).name
        if name in documents:
            _finish_validation(
                "cross-refs",
                [Path(p) for p in paths],
                ValidationResult([Finding("DUPLICATE_DOCUMENT", f"Two inputs share the name {name}", file=path)]),
                json_output=json_output,
                status="USAGE_ERROR",
            )
        documents[name] = _read_input("cross-refs", path, json_output)
    result = validate_document_collection(documents, ledger_decisions=_ledger_decisions_for(project))
    _finish_validation("cross-refs", [Path(p) for p in paths], result, json_output=json_output)


def _document_key(project: str, path: str) -> tuple[Path, str]:
    """Document identity: the POSIX path relative to the project root. Must not escape it."""

    root = _project(project)
    target = Path(path).expanduser().resolve()
    try:
        key = target.relative_to(root).as_posix()
    except ValueError:
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            f"{target} is outside the project root {root}",
            recovery="Place the document inside the project that holds its workspace.",
        ) from None
    return root, key


def _open_state(project: str) -> tuple[Any, StateStore]:
    """Open the workspace state. A missing workspace or an unmigrated schema is a BLOCKED prerequisite."""

    workspace = _workspace(project)
    if not workspace.state_db.exists():
        raise DridhError(ErrorCode.WORKSPACE_NOT_FOUND, "The workspace has no state database", recovery="Run `dridh init` in the project.")
    return workspace, StateStore(workspace.state_db, workspace.workspace_id)


@validate_app.command("document-sync")
def validate_document_sync_command(
    path: str = PATH_ARGUMENT,
    project: str = PROJECT_OPTION,
    require_validation: bool = typer.Option(False, "--require-validation", help="Treat a missing or stale validation as an error instead of a warning."),
    json_output: bool = JSON_OPTION,
) -> None:
    """Compare a Markdown file with its persisted revision history. Read-only: nothing is written.

    PASS: the file matches the current persisted revision and the history is intact.
    FAIL: an inconsistency was found (changed file, corrupt record, missing record, stale standards).
    BLOCKED: a prerequisite genuinely does not exist (no workspace, or a schema that needs migration).
    """

    target = Path(path)
    root, key = _document_key(project, path)
    try:
        workspace, store = _open_state(project)
        store.open()
    except DridhError as exc:
        needs_migration = exc.code is ErrorCode.SCHEMA_UNSUPPORTED and any(re.search(r"found [23],", str(detail)) for detail in exc.details)
        if exc.code is ErrorCode.WORKSPACE_NOT_FOUND or needs_migration:
            finding = Finding(
                f"PREREQUISITE_{exc.code.value}",
                exc.message,
                file=key,
                repair_hint=exc.recovery,
            )
            _finish_validation("document-sync", [target], ValidationResult([finding]), json_output=json_output, status="BLOCKED")
        if exc.code is ErrorCode.STATE_CORRUPT:
            finding = Finding("STATE_DATABASE_CORRUPT", exc.message, severity="ERROR", file=key, repair_hint=exc.recovery)
            _finish_validation("document-sync", [target], ValidationResult([finding]), json_output=json_output)
        raise
    source = target.read_text(encoding="utf-8") if target.is_file() else None
    try:
        repository = DocumentRepository(store.connection, workspace.workspace_id)
        findings = repository.synchronization_findings(
            relative_path=key,
            source_text=source,
            expected_standards_version=STANDARDS_VERSION,
            require_validation=require_validation,
        )
    finally:
        store.close()
    _finish_validation("document-sync", [target], ValidationResult(findings), json_output=json_output)


documents_app = typer.Typer(help="Persist canonical documents as immutable revisions in the workspace state.", no_args_is_help=True)
app.add_typer(documents_app, name="documents")


@documents_app.command("record")
def documents_record(
    path: str = PATH_ARGUMENT,
    project: str = PROJECT_OPTION,
    validate: bool = typer.Option(False, "--validate", help="Also persist a validation result for the recorded revision."),
) -> None:
    """Record the file as a new revision if its content changed. Identical content is a no-op."""

    target = Path(path)
    if not target.is_file():
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"File not found: {target}")
    _, key = _document_key(project, path)
    content = target.read_text(encoding="utf-8")
    kind = classify_document(target.name, content)
    if kind is None:
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            f"{target.name} is not a supported Dridh document type",
            recovery="Use a canonical filename or table schema. Unknown documents are not persisted.",
        )
    workspace, store = _open_state(project)
    standards = _standards_of(content)
    validator_version = f"dridh-{dridh.__version__}/standards-{STANDARDS_VERSION}"
    with store:
        with store.transaction() as connection:
            repository = DocumentRepository(connection, workspace.workspace_id)
            revision, created = repository.record_revision(
                relative_path=key,
                document_type=kind,
                content=content,
                standards_version=standards,
                source_path=str(target),
            )
            outcome: dict[str, Any] = {
                "ok": True,
                "document": key,
                "document_type": kind,
                "revision_number": revision.revision_number,
                "revision_id": revision.id,
                "content_sha256": revision.content_sha256,
                "created": created,
            }
            if validate:
                decisions = load_ledger_decisions(connection) if kind == "ledger" else None
                result = validate_document(target.name, content, ledger_decisions=decisions)
                validation_id = repository.record_validation(
                    revision_id=revision.id,
                    validator="validate_document",
                    validator_version=validator_version,
                    result=result,
                )
                outcome["validation"] = {
                    "id": validation_id,
                    "ok": result.ok,
                    "errors": sum(1 for f in result.findings if f.severity == "ERROR"),
                    "warnings": sum(1 for f in result.findings if f.severity == "WARNING"),
                }
    _emit(outcome)


@documents_app.command("show")
def documents_show(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION) -> None:
    """Show a document's persisted metadata, revision history and latest validations. Read-only."""

    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        repository = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = repository.document_metadata(key)
        if metadata is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"{key} is not recorded in this workspace", recovery=f"Record it: dridh documents record {path}")
        current = repository.current_revision(key)
        _emit(
            {
                "document": key,
                "metadata": metadata,
                "current_revision": None if current is None else current.revision_number,
                "revisions": [
                    {"revision_number": r.revision_number, "content_sha256": r.content_sha256, "created_at": r.created_at, "line_count": r.line_count}
                    for r in repository.revisions(key)
                ],
                "validations": [
                    {"revision_number": v.revision_number, "validator": v.validator, "status": v.status, "error_count": v.error_count, "warning_count": v.warning_count, "validated_at": v.validated_at}
                    for v in repository.document_validations(key)
                ],
            }
        )


usecases_app = typer.Typer(help="Import and reconcile actors, use cases and flows from an approved use-case document.", no_args_is_help=True)
app.add_typer(usecases_app, name="usecases")


@usecases_app.command("import")
def usecases_import(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION) -> None:
    """Replace the use-case rows with this document's content. Refused unless the document validates."""

    target = Path(path)
    if not target.is_file():
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"File not found: {target}")
    _, key = _document_key(project, path)
    content = target.read_text(encoding="utf-8")
    if classify_document(target.name, content) != "use_cases":
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{target.name} is not a use-case document", recovery="Only 001-USE-CASES.md-style documents are imported.")
    model = validated_model(content, target.name)
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as connection:
            documents = DocumentRepository(connection, workspace.workspace_id)
            revision, created = documents.record_revision(
                relative_path=key,
                document_type="use_cases",
                content=content,
                standards_version=_standards_of(content),
                source_path=str(target),
            )
            document_id = str(documents.document_metadata(key)["id"])  # type: ignore[index]
            repository = UseCaseRepository(connection, workspace.workspace_id)
            changed = not repository.projection_is_current(document_id=document_id, revision_id=revision.id, model=model)
            if changed:
                counts = repository.replace_projection(document_id=document_id, revision_id=revision.id, model=model)
            else:
                counts = {"actors": len(model.actors), "use_cases": len(model.use_cases), "flows": len(model.flows)}
    _emit(
        {
            "ok": True,
            "document": key,
            "revision_number": revision.revision_number,
            "revision_id": revision.id,
            "revision_created": created,
            "projection_changed": changed,
            "counts": counts,
        }
    )


@usecases_app.command("show")
def usecases_show(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION) -> None:
    """Show the stored actors, use cases and flows for a document. Read-only."""

    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        documents = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = documents.document_metadata(key)
        if metadata is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"{key} is not recorded in this workspace", recovery=f"Import it: dridh usecases import {path}")
        model = UseCaseRepository(store.connection, workspace.workspace_id).stored_model(str(metadata["id"]))
    _emit({"document": key, "actors": [asdict(a) for a in model.actors], "use_cases": [asdict(u) for u in model.use_cases], "flows": [asdict(f) for f in model.flows]})


@usecases_app.command("verify")
def usecases_verify(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION, json_output: bool = JSON_OPTION) -> None:
    """Database reconciliation only: compares the stored use-case rows with the stored revision text.

    This command does not read the Markdown file. To check whether the file has changed since its
    last recorded revision, use `dridh validate document-sync`. Read-only; exit 1 on mismatch.
    """

    target = Path(path)
    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        documents = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = documents.document_metadata(key)
        current = documents.current_revision(key)
        if metadata is None or current is None:
            findings = [Finding("PROJECTION_MISSING", "The document is not recorded in this workspace", file=key, repair_hint=f"Record and import it: dridh usecases import {path}")]
        else:
            current_content = documents.stored_content(current.id)
            findings = UseCaseRepository(store.connection, workspace.workspace_id).projection_findings(
                relative_path=key,
                document_id=str(metadata["id"]),
                current_revision_id=current.id,
                current_content=current_content,
            )
    _finish_validation(
        "usecases verify",
        [target],
        ValidationResult(findings),
        json_output=json_output,
        details={"compares": "stored use-case rows with the stored revision text", "reads_source_file": False},
    )


design_app = typer.Typer(help="Import and reconcile components, design decisions, traceability and API/event references.", no_args_is_help=True)
app.add_typer(design_app, name="design")


def _design_kind(target: Path, content: str) -> str:
    kind = classify_document(target.name, content)
    if kind == "tdd":
        return "TDD"
    if kind == "component_master":
        return "COMPONENT_MASTER"
    raise DridhError(ErrorCode.VALIDATION_FAILED, f"{target.name} is not a TDD or component master", recovery="Import a feature TDD or a component master.")


@design_app.command("import")
def design_import(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION) -> None:
    """Replace the design rows of a TDD or component master. Refused unless the document validates.

    A TDD needs its use-case document imported first (Table 13 traceability). A component master needs
    the TDD that defines its component. An unchanged revision and identical rows write nothing.
    """

    target = Path(path)
    if not target.is_file():
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"File not found: {target}")
    _, key = _document_key(project, path)
    content = target.read_text(encoding="utf-8")
    kind = _design_kind(target, content)
    model = validated_tdd_model(content, target.name) if kind == "TDD" else validated_master_model(content, target.name)
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as connection:
            documents = DocumentRepository(connection, workspace.workspace_id)
            revision, created = documents.record_revision(
                relative_path=key,
                document_type="tdd" if kind == "TDD" else "component_master",
                content=content,
                standards_version=_standards_of(content),
                source_path=str(target),
            )
            document_id = str(documents.document_metadata(key)["id"])  # type: ignore[index]
            repository = DesignRepository(connection, workspace.workspace_id)
            changed = not repository.is_current(document_id=document_id, revision_id=revision.id, kind=kind, model=model)
            if changed:
                counts = repository.replace_tdd(document_id=document_id, revision_id=revision.id, model=model) if kind == "TDD" else repository.replace_component_master(document_id=document_id, revision_id=revision.id, model=model)  # type: ignore[arg-type]
            else:
                counts = {"unchanged": True}
    _emit({"ok": True, "document": key, "document_kind": kind, "revision_number": revision.revision_number, "revision_created": created, "projection_changed": changed, "counts": counts})


@design_app.command("show")
def design_show(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION) -> None:
    """Show the stored components, decisions, mappings and API/event references. Read-only."""

    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        documents = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = documents.document_metadata(key)
        if metadata is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"{key} is not recorded in this workspace", recovery=f"Import it: dridh design import {path}")
        repository = DesignRepository(store.connection, workspace.workspace_id)
        marker = repository.import_marker(str(metadata["id"]))
        if marker is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"{key} has not been imported into the design domain", recovery=f"Import it: dridh design import {path}")
        if marker["document_kind"] == "TDD":
            model = repository.stored_tdd(str(metadata["id"]))
            body: dict[str, object] = {"components": [asdict(c) for c in model.components], "decisions": [asdict(d) for d in model.decisions], "mappings": [asdict(m) for m in model.mappings], "api_events": [asdict(a) for a in model.api_events]}
        else:
            master = repository.stored_master(str(metadata["id"]))
            body = {"component_id": master.component_id, "decisions": [asdict(d) for d in master.decisions]}
    _emit({"document": key, "document_kind": marker["document_kind"], **body})


@design_app.command("verify")
def design_verify(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION, json_output: bool = JSON_OPTION) -> None:
    """Database reconciliation only: compares stored design rows with the stored revision text. Read-only."""

    target = Path(path)
    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        documents = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = documents.document_metadata(key)
        current = documents.current_revision(key)
        if metadata is None or current is None:
            findings = [Finding("DESIGN_NEVER_IMPORTED", "The document is not recorded in this workspace", file=key, repair_hint=f"Import it: dridh design import {path}")]
        else:
            marker = DesignRepository(store.connection, workspace.workspace_id).import_marker(str(metadata["id"]))
            kind = marker["document_kind"] if marker is not None else ("TDD" if metadata["document_type"] == "tdd" else "COMPONENT_MASTER")
            findings = DesignRepository(store.connection, workspace.workspace_id).findings(
                relative_path=key,
                document_id=str(metadata["id"]),
                kind=kind,
                current_revision_id=current.id,
                current_content=documents.stored_content(current.id),
            )
    _finish_validation(
        "design verify",
        [target],
        ValidationResult(findings),
        json_output=json_output,
        details={"compares": "stored design rows with the stored revision text", "reads_source_file": False},
    )


planning_app = typer.Typer(help="Import and reconcile stories, dependencies, phases, milestones, risks, test phases and evidence.", no_args_is_help=True)
app.add_typer(planning_app, name="planning")
_KIND_OF_PLANNING = {"central_plan": "CENTRAL_PLAN", "phase_file": "PHASE_FILE", "test_plan": "TEST_PLAN"}


@planning_app.command("import")
def planning_import(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION) -> None:
    """Import a central plan, phase file or test plan. Refused unless it validates; unchanged revisions write nothing."""

    target = Path(path)
    if not target.is_file():
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"File not found: {target}")
    _, key = _document_key(project, path)
    content = target.read_text(encoding="utf-8")
    kind = classify_document(target.name, content)
    if kind not in _KIND_OF_PLANNING:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{target.name} is not a central plan, phase file or test plan", recovery="Import a canonical planning document.")
    planning_kind = _KIND_OF_PLANNING[kind]
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as connection:
            documents = DocumentRepository(connection, workspace.workspace_id)
            revision, created = documents.record_revision(relative_path=key, document_type=kind, content=content, standards_version=_standards_of(content), source_path=str(target))
            document_id = str(documents.document_metadata(key)["id"])  # type: ignore[index]
            repository = PlanningRepository(connection, workspace.workspace_id)
            marker = repository.import_marker(document_id)
            changed = marker is None or marker["source_revision_id"] != revision.id
            if not changed:
                counts: dict[str, object] = {"unchanged": True}
            elif planning_kind == "CENTRAL_PLAN":
                counts = repository.import_central(document_id=document_id, revision_id=revision.id, model=validated_central(content, target.name))
            elif planning_kind == "PHASE_FILE":
                counts = repository.import_phase(document_id=document_id, revision_id=revision.id, model=validated_phase(content, target.name))
            else:
                counts = repository.import_test_plan(document_id=document_id, revision_id=revision.id, model=validated_test_plan(content, target.name))
    _emit({"ok": True, "document": key, "document_kind": planning_kind, "revision_number": revision.revision_number, "revision_created": created, "projection_changed": changed, "counts": counts})


@planning_app.command("verify")
def planning_verify(path: str = PATH_ARGUMENT, project: str = PROJECT_OPTION, json_output: bool = JSON_OPTION) -> None:
    """Database reconciliation only: compares stored planning rows with the stored revision text. Read-only."""

    target = Path(path)
    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        documents = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = documents.document_metadata(key)
        current = documents.current_revision(key)
        if metadata is None or current is None:
            findings = [Finding("PLANNING_NEVER_IMPORTED", "The document is not recorded in this workspace", file=key, repair_hint=f"Import it: dridh planning import {path}")]
        else:
            repository = PlanningRepository(store.connection, workspace.workspace_id)
            marker = repository.import_marker(str(metadata["id"]))
            kind = marker["document_kind"] if marker is not None else _KIND_OF_PLANNING.get(str(metadata["document_type"]), "CENTRAL_PLAN")
            findings = repository.findings(relative_path=key, document_id=str(metadata["id"]), kind=kind, current_revision_id=current.id, current_content=documents.stored_content(current.id))
    _finish_validation("planning verify", [target], ValidationResult(findings), json_output=json_output, details={"compares": "stored planning rows with the stored revision text", "reads_source_file": False})


@planning_app.command("stories")
def planning_stories(story_id: str | None = typer.Option(None, "--story", help="Show one story."), project: str = PROJECT_OPTION) -> None:
    """Stories in deterministic execution order, with derived unlocks, parallel partners and ready/blocked state. Read-only."""

    workspace, store = _open_state(project)
    with store:
        repository = PlanningRepository(store.connection, workspace.workspace_id)
        views = repository.graph()
        order = repository.topological_order()
        selected = [story_id] if story_id else order
        if story_id and story_id not in views:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"{story_id} is not a story in this workspace")
        _emit({"order": order, "stories": [asdict(views[sid]) for sid in selected]})


@planning_app.command("graph")
def planning_graph(project: str = PROJECT_OPTION) -> None:
    """Verify the dependency graph: acyclic, deterministic order, ready and blocked stories. Read-only."""

    workspace, store = _open_state(project)
    with store:
        repository = PlanningRepository(store.connection, workspace.workspace_id)
        order = repository.topological_order()
        views = repository.graph()
    _emit({"acyclic": True, "order": order, "ready": [s for s in order if views[s].state == "READY"], "blocked": [s for s in order if views[s].state == "BLOCKED"]})


@planning_app.command("milestones")
def planning_milestones(project: str = PROJECT_OPTION) -> None:
    """Milestones with their status and linked stories. Read-only."""

    workspace, store = _open_state(project)
    with store:
        rows = store.connection.execute(
            "SELECT milestone_id, title, status, target_date FROM milestone WHERE workspace_id = ? ORDER BY position", (workspace.workspace_id,)
        ).fetchall()
        items = []
        for row in rows:
            linked = [r[0] for r in store.connection.execute(
                "SELECT story_id FROM milestone_story WHERE workspace_id = ? AND milestone_id = ? ORDER BY story_id", (workspace.workspace_id, row["milestone_id"])
            )]
            items.append({"milestone_id": row["milestone_id"], "title": row["title"], "status": row["status"], "target_date": row["target_date"], "stories": linked})
    _emit({"milestones": items})


@planning_app.command("milestone-link")
def planning_milestone_link(milestone_id: str, story_id: str, project: str = PROJECT_OPTION) -> None:
    """Associate a story with a milestone."""

    workspace, store = _open_state(project)
    with store:
        with store.transaction() as connection:
            PlanningRepository(connection, workspace.workspace_id).link_milestone(milestone_id, story_id)
    _emit({"ok": True, "milestone_id": milestone_id, "story_id": story_id})


@planning_app.command("group-create")
def planning_group_create(name: str, stories: str = typer.Option(..., "--stories", help="Comma-separated story IDs, mutually independent."), project: str = PROJECT_OPTION) -> None:
    """Create a parallel group. Stories must be mutually independent in the dependency graph."""

    members = [s.strip() for s in stories.split(",") if s.strip()]
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as connection:
            group_id = PlanningRepository(connection, workspace.workspace_id).create_parallel_group(name, members)
    _emit({"ok": True, "group": name, "group_id": group_id, "stories": members})


@planning_app.command("evidence-record")
def planning_evidence_record(
    story_id: str,
    test_phase_id: str,
    status: str = typer.Option(..., "--status", help="NOT_STARTED, IN_PROGRESS, PASSED, FAILED, BLOCKED or NOT_APPLICABLE."),
    reference: str = typer.Option(..., "--ref", help="Evidence reference: a report path, CI run URL or log location."),
    project: str = PROJECT_OPTION,
) -> None:
    """Append a test evidence record for a story and test phase. Evidence is append-only."""

    workspace, store = _open_state(project)
    with store:
        with store.transaction() as connection:
            evidence_id = PlanningRepository(connection, workspace.workspace_id).record_evidence(
                story_id=story_id, test_phase_id=test_phase_id, status=status, reference=reference, recorded_by=_operator()
            )
    _emit({"ok": True, "evidence_id": evidence_id, "story_id": story_id, "test_phase_id": test_phase_id, "status": status})


@planning_app.command("evidence-list")
def planning_evidence_list(story_id: str | None = typer.Option(None, "--story"), project: str = PROJECT_OPTION) -> None:
    """List recorded test evidence, optionally for one story. Read-only."""

    workspace, store = _open_state(project)
    with store:
        sql = "SELECT story_id, test_phase_id, evidence_status, evidence_ref, recorded_by, recorded_at FROM story_test_evidence WHERE workspace_id = ?"
        params: tuple = (workspace.workspace_id,)
        if story_id:
            sql += " AND story_id = ?"
            params += (story_id,)
        rows = [dict(r) for r in store.connection.execute(sql + " ORDER BY recorded_at, story_id", params).fetchall()]
    _emit({"evidence": rows})


def _standards_of(content: str) -> str:
    match = re.search(r"\*\*Standards Version:\*\*\s*(\S+)", content)
    return match.group(1) if match else STANDARDS_VERSION


def _ledger_decide(ledger_id: str, decision: str, reviewer: str, reason: str, ledger_file: str, project: str) -> None:
    if not LDG_ID.fullmatch(ledger_id):
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{ledger_id} is not a ledger ID", recovery="Use the form LDG-NN.")
    if not reviewer.strip():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "--reviewer is required", recovery="Name the human reviewer.")
    if decision == "reject" and not reason.strip():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "--reason is required to reject", recovery="Explain the rejection.")

    from dridh.ledger.drafts import ledger_file as default_ledger_file

    target = Path(ledger_file) if ledger_file else default_ledger_file(_workspace(project))
    if not target.is_file():
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"Ledger file not found: {target}")
    content = target.read_text(encoding="utf-8")
    row = next((r for t in parse_tables(content) if t.header == H_LEDGER for r in t.rows if plain(r.cells[0]) == ledger_id), None)
    if row is not None and decision == "approve" and plain(row.cells[7]) in NO_APPROVER:
        # A draft Dridh wrote has no approver yet: the approving human's name goes into the entry before it is hashed.
        lines = content.split("\n")
        index = next(i for i, line in enumerate(lines) if line.startswith("|") and plain(line.strip("|").split("|")[0]) == ledger_id)
        cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
        cells[7] = reviewer.strip().replace("|", "/")
        proposed = "\n".join([*lines[:index], "| " + " | ".join(cells) + " |", *lines[index + 1:]])
    else:
        proposed = None
    if row is None:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{ledger_id} is not in the Table 14 ledger", recovery="Check the ledger ID.")
    status = plain(row.cells[2])
    if status not in LEDGER_APPROVAL_REQUIRED:
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            f"{ledger_id} is {status}; only DEVIATED, NOT_DONE and WONT_DO entries need a human decision",
        )
    if proposed is not None:
        content = proposed
    entry_sha = ledger_entry_revision(content, ledger_id)
    document_sha = content_revision(content)

    _confirm(
        f"{decision.upper()} {ledger_id}",
        f"{decision.capitalize()} ledger entry {ledger_id} ({status}) for reviewer {reviewer}.\n"
        f"Entry SHA-256: {entry_sha}\nDocument SHA-256: {document_sha}",
    )
    workspace = _workspace(project)
    if proposed is not None:
        target.write_text(content, encoding="utf-8")
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        with store.transaction() as connection:
            seq = record_ledger_decision(
                connection,
                ledger_id=ledger_id,
                decision=decision,
                reviewer=reviewer.strip(),
                reason=reason.strip(),
                entry_sha256=entry_sha or "",
                document_sha256=document_sha,
                actor=_operator(),
            )
    _emit(
        {
            "ok": True,
            "ledger_id": ledger_id,
            "decision": decision,
            "reviewer": reviewer.strip(),
            "entry_sha256": entry_sha,
            "document_sha256": document_sha,
            "audit_seq": seq,
        }
    )
    try:  # keep the queryable rows current; an import problem is reported by `dridh ledger reconcile`, never hidden
        from dridh.ledger.service import LedgerService

        LedgerService(workspace).import_ledger(str(target))
    except DridhError:
        pass


_REVIEWER_HELP = "Human reviewer name. This is attribution metadata, not authentication."
_LEDGER_FILE_HELP = "Path to LEDGER.md (default: the workspace's .opencode/dridh/documents/LEDGER.md)."


@ledger_app.command("approve")
def ledger_approve(
    ledger_id: str,
    reviewer: str = typer.Option(..., "--reviewer", help=_REVIEWER_HELP),
    ledger_file: str = typer.Option("", "--ledger-file", help=_LEDGER_FILE_HELP),
    project: str = PROJECT_OPTION,
) -> None:
    """Approve a deviation. Requires an interactive terminal and a typed confirmation."""

    _ledger_decide(ledger_id, "approve", reviewer, "", ledger_file, project)


@ledger_app.command("reject")
def ledger_reject(
    ledger_id: str,
    reviewer: str = typer.Option(..., "--reviewer", help=_REVIEWER_HELP),
    reason: str = typer.Option(..., "--reason", help="Why the deviation is rejected."),
    ledger_file: str = typer.Option("", "--ledger-file", help=_LEDGER_FILE_HELP),
    project: str = PROJECT_OPTION,
) -> None:
    """Reject a deviation. Requires an interactive terminal and a typed confirmation."""

    _ledger_decide(ledger_id, "reject", reviewer, reason, ledger_file, project)


@ledger_app.command("draft")
def ledger_draft(
    story: str = typer.Option(..., "--story", help="A BLOCKED story whose flows will not be built."),
    reason: str = typer.Option(..., "--reason", help="Why its flows are not being built."),
    project: str = PROJECT_OPTION,
) -> None:
    """Record every flow of a BLOCKED story as NOT_DONE (drafts awaiting approval). Writes LEDGER.md; approves nothing."""
    from dridh.ledger.drafts import add_drafts
    from dridh.ledger.flows import story_flows

    workspace = _workspace(project)
    if not reason.strip():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "--reason is required", recovery="Say why the story's flows are not being built.")
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        connection = store.read()
        row = connection.execute("SELECT status FROM story WHERE workspace_id=? AND story_id=?", (workspace.workspace_id, story)).fetchone()
        flows = story_flows(connection, workspace.workspace_id).get(story, [])
    if row is None:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{story} is not a planned story.")
    if row["status"] != "BLOCKED":
        raise DridhError(ErrorCode.INVALID_TRANSITION, f"{story} is {row['status']}; only a BLOCKED story's flows can be recorded as not done.",
                         recovery="A story that completes reports its own flows; rework or block it first.")
    _emit(add_drafts(workspace, [{"flow": flow, "status": "NOT_DONE", "story": story, "actual": "Not built: the story is blocked.",
                                  "reason": reason.strip(), "source": "a human (`dridh ledger draft`)"} for flow in flows]))


@ledger_app.command("import")
def ledger_import(
    path: str = PATH_ARGUMENT,
    project: str = PROJECT_OPTION,
) -> None:
    """Import LEDGER.md as queryable rows. Missing human approvals are reported, not hidden; nothing is approved."""
    from dridh.ledger.service import LedgerService

    _emit(LedgerService(_workspace(project)).import_ledger(path))


@ledger_app.command("reconcile")
def ledger_reconcile(
    path: str = PATH_ARGUMENT,
    repair: bool = typer.Option(False, "--repair", help="Re-import the rows from the file when they drifted. Never edits Markdown or records approvals."),
    project: str = PROJECT_OPTION,
) -> None:
    """Compare the ledger file, its rows and the plan: drift, tampering, unknown references, uncovered scope, missing approvals."""
    from dridh.ledger.service import LedgerService

    report = LedgerService(_workspace(project)).reconcile(path, repair=repair)
    _emit(report)
    if not report["ok"]:
        raise typer.Exit(code=4)  # validation failure


@ledger_app.command("list")
def ledger_list(project: str = PROJECT_OPTION) -> None:
    """List the imported ledger entries."""
    from dridh.state.ledger_entries import LedgerEntryRepository

    workspace = _workspace(project)
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        _emit({"entries": LedgerEntryRepository(store.read(), workspace.workspace_id).entries()})


reviews_app = typer.Typer(help="Revision-bound reviews and human approvals.", no_args_is_help=True)
app.add_typer(reviews_app, name="reviews")


@reviews_app.command("create")
def reviews_create(
    path: str = PATH_ARGUMENT,
    reviewer: str = typer.Option(..., "--reviewer"),
    project: str = PROJECT_OPTION,
) -> None:
    """Create a review against the current persisted document revision."""
    _, key = _document_key(project, path)
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as conn:
            documents = DocumentRepository(conn, workspace.workspace_id)
            metadata = documents.document_metadata(key)
            revision = documents.current_revision(key)
            if metadata is None or revision is None:
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Record the document first: dridh documents record <path>")
            from dridh.state.reviews import ReviewRepository
            review_id = ReviewRepository(conn, workspace.workspace_id).create_review(
                document_id=str(metadata["id"]), revision_id=revision.id, reviewer=reviewer, actor=_operator()
            )
    _emit({"ok": True, "review_id": review_id, "document": key, "revision_id": revision.id, "revision_number": revision.revision_number})


@reviews_app.command("show")
def reviews_show(review_id: str, project: str = PROJECT_OPTION) -> None:
    workspace, store = _open_state(project)
    with store:
        from dridh.state.reviews import ReviewRepository
        _emit(ReviewRepository(store.connection, workspace.workspace_id).get_review(review_id))


@reviews_app.command("checklist")
def reviews_checklist(
    review_id: str,
    item: str = typer.Argument("", help="The checklist item text (omit with --all)."),
    checked: bool = typer.Option(True, "--checked/--unchecked"),
    all_items: bool = typer.Option(False, "--all", help="Tick or untick every item after reading the list."),
    project: str = PROJECT_OPTION,
) -> None:
    """Record a human's checklist decision. Approval is refused while any item is unchecked."""
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as conn:
            from dridh.state.reviews import ReviewRepository
            repository = ReviewRepository(conn, workspace.workspace_id)
            if all_items:
                items = [row["item"] for row in repository.get_review(review_id)["checklist"]]
            elif item.strip():
                items = [item]
            else:
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Name a checklist item or pass --all.")
            for text in items:
                repository.set_checklist(review_id, text, checked, actor=_operator())
    _emit({"ok": True, "review_id": review_id, "items": items, "checked": checked})


@reviews_app.command("comment")
def reviews_comment(review_id: str, comment: str, project: str = PROJECT_OPTION) -> None:
    workspace, store = _open_state(project)
    with store:
        with store.transaction() as conn:
            from dridh.state.reviews import ReviewRepository
            comment_id = ReviewRepository(conn, workspace.workspace_id).add_comment(
                review_id, actor=_operator(), comment=comment
            )
    _emit({"ok": True, "review_id": review_id, "comment_id": comment_id})


def _review_decide(review_id: str, decision: str, reviewer: str, reason: str, project: str) -> None:
    workspace, store = _open_state(project)
    with store:
        row = store.connection.execute(
            "SELECT revision_id FROM review WHERE id=? AND workspace_id=?", (review_id, workspace.workspace_id)
        ).fetchone()
        if row is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown review {review_id}")
        revision_id = row["revision_id"]
        _confirm(
            f"{decision} {review_id} {revision_id[:12]}",
            f"{decision} review {review_id} for immutable revision {revision_id}; reviewer={reviewer}",
        )
        with store.transaction() as conn:
            from dridh.state.reviews import ReviewRepository
            approval_id = ReviewRepository(conn, workspace.workspace_id).decide(
                review_id, decision=decision, reviewer=reviewer, reason=reason,
                actor=_operator(), revision_id=revision_id,
            )
    _emit({"ok": True, "review_id": review_id, "decision": decision, "approval_id": approval_id, "revision_id": revision_id})


@reviews_app.command("approve")
def reviews_approve(
    review_id: str, reviewer: str = typer.Option(..., "--reviewer"), project: str = PROJECT_OPTION
) -> None:
    """Human-only terminal approval bound to the reviewed revision."""
    _review_decide(review_id, "APPROVED", reviewer, "", project)


@reviews_app.command("reject")
def reviews_reject(
    review_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    reason: str = typer.Option(..., "--reason"),
    project: str = PROJECT_OPTION,
) -> None:
    """Human-only terminal rejection bound to the reviewed revision."""
    _review_decide(review_id, "REJECTED", reviewer, reason, project)


def _ideator(project: str) -> IdeatorService:
    return IdeatorService(open_workspace(_project(project)))


@ideator_app.command("start")
def ideator_start(
    objective: str,
    project: str = PROJECT_OPTION,
    existing_context: str = typer.Option("", "--existing-context", help="Relevant workspace facts already known."),
) -> None:
    """Start a resumable requirements discovery session."""
    _emit(_ideator(project).start(objective, existing_context=existing_context))


@ideator_app.command("answer")
def ideator_answer(
    session_id: str,
    topic: str = typer.Option(..., "--topic"),
    question: str = typer.Option(..., "--question"),
    answer: str = typer.Option("", "--answer"),
    source: str = typer.Option("user", "--source"),
    unresolved: bool = typer.Option(False, "--unresolved"),
    project: str = PROJECT_OPTION,
) -> None:
    """Record one answer (or explicitly unresolved question) in a discovery session."""
    _emit(_ideator(project).record_answer(
        session_id, topic=topic, question=question, answer=answer, source=source, unresolved=unresolved
    ))


@ideator_app.command("status")
def ideator_status(session_id: str, project: str = PROJECT_OPTION) -> None:
    """Show discovery progress, Markdown/JSON parity and the Technical Designer gate."""
    _emit(_ideator(project).status(session_id))


@ideator_app.command("parity")
def ideator_parity(session_id: str, project: str = PROJECT_OPTION) -> None:
    """Verify that INQUIRY-NOTES.md is the exact projection of inquiry.json."""
    result = _ideator(project).parity(session_id)
    _emit(result)
    if not result["ok"]:
        raise typer.Exit(code=1)


@ideator_app.command("generate")
def ideator_generate(
    session_id: str,
    project: str = PROJECT_OPTION,
    model: str | None = typer.Option(None, "--model", help="Optional OpenCode model override for the Ideator workers."),
) -> None:
    """Run discovery workers, generate and validate the canonical Use Case document."""
    _emit(_ideator(project).generate(session_id, model=model))


@ideator_app.command("approve")
def ideator_approve(
    session_id: str,
    reviewer: str = typer.Option(..., "--reviewer", help="Human reviewer name."),
    project: str = PROJECT_OPTION,
) -> None:
    """Approve the exact Use Case revision after typing its terminal confirmation."""
    service = _ideator(project)
    challenge = service.approval_challenge(session_id)
    _confirm(
        challenge["confirmation"],
        f"Approve {challenge['document']} with SHA-256 {challenge['sha256']} for review {challenge['review_id']}?",
    )
    _emit(service.approve(session_id, reviewer=reviewer, confirmation=challenge["confirmation"]))


@ideator_app.command("gate")
def ideator_gate(
    project: str = PROJECT_OPTION,
    session_id: str | None = typer.Option(None, "--session", help="Inquiry session whose approval is being checked."),
) -> None:
    """Check whether the approved Use Case revision unlocks Technical Designer."""
    _emit(_ideator(project).gate(session_id))


def _designer(project: str) -> DesignerService:
    return DesignerService(open_workspace(_project(project)))


@designer_app.command("start")
def designer_start(
    ideator_session_id: str,
    feature: str = typer.Option(..., "--feature", help="Feature slug/name used for <feature>_tdd.md."),
    project: str = PROJECT_OPTION,
    existing_context: str = typer.Option("", "--existing-context"),
    code_reacher_evidence: str = typer.Option("", "--code-reacher-evidence", help="Verified Code Reacher findings; do not substitute assumptions."),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Start Technical Designer only from the exact approved Use Case revision."""
    _emit(_designer(project).start(
        ideator_session_id,
        feature=feature,
        existing_context=existing_context,
        code_reacher_evidence=code_reacher_evidence,
        model=model,
    ))


@designer_app.command("answer")
def designer_answer(
    session_id: str,
    topic: str = typer.Option(..., "--topic"),
    answer: str = typer.Option("", "--answer"),
    source: str = typer.Option("user", "--source"),
    impact: str | None = typer.Option(None, "--impact"),
    unresolved: bool = typer.Option(False, "--unresolved"),
    project: str = PROJECT_OPTION,
) -> None:
    """Record a technical answer or explicitly mark it unresolved."""
    _emit(_designer(project).answer(
        session_id,
        topic=topic,
        answer=answer,
        source=source,
        impact=impact,
        unresolved=unresolved,
    ))


@designer_app.command("status")
def designer_status(session_id: str, project: str = PROJECT_OPTION) -> None:
    _emit(_designer(project).status(session_id))


@designer_app.command("parity")
def designer_parity(session_id: str, project: str = PROJECT_OPTION) -> None:
    result = _designer(project).parity(session_id)
    _emit(result)
    if not result["ok"]:
        raise typer.Exit(code=1)


@designer_app.command("generate")
def designer_generate(
    session_id: str,
    project: str = PROJECT_OPTION,
    model: str | None = typer.Option(None, "--model"),
) -> None:
    _emit(_designer(project).generate(session_id, model=model))


@designer_app.command("gate")
def designer_gate(session_id: str, project: str = PROJECT_OPTION) -> None:
    result = _designer(project).gate(session_id)
    _emit(result)
    if not result["unlocked"]:
        raise typer.Exit(code=1)


@designer_app.command("approve")
def designer_approve(
    session_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    project: str = PROJECT_OPTION,
) -> None:
    """Human-only terminal approval of the exact TDD revision."""
    service = _designer(project)
    challenge = service.approval_challenge(session_id)
    _confirm(
        challenge["confirmation"],
        f"Approve {challenge['document']} with SHA-256 {challenge['sha256']} for review {challenge['review_id']}?",
    )
    _emit(service.approve(session_id, reviewer=reviewer, confirmation=challenge["confirmation"]))


planner_app = typer.Typer(help="Run the approved-design Planner stage and reconcile central, component and testing plans.", no_args_is_help=True)
app.add_typer(planner_app, name="planner")


def _planner(project: str) -> PlannerService:
    return PlannerService(_workspace(project))


@planner_app.command("start")
def planner_start(designer_session_id: str, project: str = PROJECT_OPTION, model: str | None = typer.Option(None, "--model")) -> None:
    """Start or resume Planner discovery from an approved Technical Designer session."""
    _emit(_planner(project).start(designer_session_id, model=model))


@planner_app.command("clarify")
def planner_clarify(
    session_id: str,
    question: str = typer.Option(..., "--question"),
    answer: str = typer.Option(..., "--answer"),
    source: str = typer.Option("user", "--source"),
    project: str = PROJECT_OPTION,
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Record a human clarification and resume the same Planner discovery session."""
    _emit(_planner(project).clarify(session_id, question=question, answer=answer, source=source, model=model))


@planner_app.command("status")
def planner_status(session_id: str, project: str = PROJECT_OPTION) -> None:
    """Show persisted Planner discovery and generation progress."""
    _emit(_planner(project).status(session_id))


@planner_app.command("generate")
def planner_generate(session_id: str, project: str = PROJECT_OPTION, model: str | None = typer.Option(None, "--model")) -> None:
    """Generate and reconcile central, component and test plans with a review checklist."""
    _emit(_planner(project).generate(session_id, model=model))


@planner_app.command("sync")
def planner_sync(session_id: str, project: str = PROJECT_OPTION) -> None:
    """Refresh central, component, phase and test status projections from SQLite."""
    _emit(_planner(project).sync(session_id))


@planner_app.command("review")
def planner_review(session_id: str, project: str = PROJECT_OPTION) -> None:
    """Open the plan review. A human then ticks each checklist item (`dridh reviews checklist <id> --all`) and approves."""
    _emit(_planner(project).open_review(session_id))


@planner_app.command("approve")
def planner_approve(
    session_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    project: str = PROJECT_OPTION,
) -> None:
    """Human-only terminal approval of the generated plan; this is what enables execution."""
    service = _planner(project)
    challenge = service.approval_challenge(session_id)
    _confirm(
        challenge["confirmation"],
        f"Approve {challenge['story_count']} planned stories (plan digest {challenge['plan_digest'][:12]}) in {challenge['central_plan']}?",
    )
    _emit(service.approve(session_id, reviewer=reviewer, confirmation=challenge["confirmation"]))


@planner_app.command("gate")
def planner_gate(session_id: str, project: str = PROJECT_OPTION) -> None:
    """Verify that the generated planning baseline is current and reconciled."""
    result = _planner(project).gate(session_id)
    _emit(result)
    if not result["unlocked"]:
        raise typer.Exit(code=1)


trace_app = typer.Typer(help="Workspace traceability projections.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")


@trace_app.command("use-cases")
def trace_use_cases(project: str = PROJECT_OPTION) -> None:
    workspace, store = _open_state(project)
    with store:
        from dridh.state.traceability import TraceabilityRepository
        _emit({"ok": True, "items": TraceabilityRepository(store.connection, workspace.workspace_id).use_case_story_map()})


@trace_app.command("story")
def trace_story(story_id: str, project: str = PROJECT_OPTION) -> None:
    workspace, store = _open_state(project)
    with store:
        from dridh.state.traceability import TraceabilityRepository
        _emit({"ok": True, **TraceabilityRepository(store.connection, workspace.workspace_id).story_trace(story_id)})


@trace_app.command("risks")
def trace_risks(project: str = PROJECT_OPTION) -> None:
    workspace, store = _open_state(project)
    with store:
        from dridh.state.traceability import TraceabilityRepository
        _emit({"ok": True, "items": TraceabilityRepository(store.connection, workspace.workspace_id).risks()})


@trace_app.command("evidence")
def trace_evidence(story_id: str | None = typer.Option(None, "--story"), project: str = PROJECT_OPTION) -> None:
    workspace, store = _open_state(project)
    with store:
        from dridh.state.traceability import TraceabilityRepository
        _emit({"ok": True, "items": TraceabilityRepository(store.connection, workspace.workspace_id).test_evidence(story_id=story_id)})


@app.command("backup")
def backup_command(project: str = PROJECT_OPTION, output: Path = typer.Option(..., "--output", help="Backup ZIP path outside the project workspace.")) -> None:  # noqa: B008
    """Create a consistent, integrity-checked workspace backup."""
    from dridh.state.backup import backup_workspace
    info = _workspace(project)
    _emit(backup_workspace(workspace_root=info.project_root, db_path=info.state_db, destination=output, dridh_version=dridh.__version__))


@app.command("restore")
def restore_command(backup: Path = typer.Option(..., "--backup", help="Dridh backup ZIP."), destination: Path = typer.Option(..., "--destination", help="Empty project directory to restore into.")) -> None:  # noqa: B008
    """Restore a backup into a new or empty project directory."""
    from dridh.state.backup import restore_workspace
    _emit(restore_workspace(backup_path=backup, destination=destination))


@app.command("integrity")
def integrity_command(project: str = PROJECT_OPTION) -> None:
    """Check SQLite and foreign-key integrity for a workspace."""
    from dridh.state.backup import integrity_check
    info = _workspace(project)
    verdict = integrity_check(info.state_db)
    _emit(verdict)
    if not verdict["ok"]:
        raise typer.Exit(code=1)


def main() -> None:
    """Console-script wrapper that renders stable CLI errors on stderr."""

    sys.argv[1:] = _take_global_json(sys.argv[1:])
    try:
        try:
            app()
        except Exception as raw:  # a model-runner failure is reported like any Dridh error, not as a traceback
            typed = from_agent_error(raw)
            if typed is None:
                raise
            raise typed from None
    except DridhError as exc:
        error = redact(exc.to_dict())
        if sys.stderr.isatty() and not wants_json():
            sys.stderr.write(render_error(error) + "\n")
        else:
            sys.stderr.write(json.dumps({"ok": False, "error": error}, separators=(",", ":")) + "\n")
        raise SystemExit(exit_code_for(exc.code)) from None


def _take_global_json(args: list[str]) -> list[str]:
    """`--json` anywhere forces JSON output. It is left in place for commands that define their own --json option."""

    if "--json" not in args:
        return args
    force_json()
    command: Any = typer.main.get_command(app)
    for arg in args:
        if arg.startswith("-") or not hasattr(command, "commands"):
            break
        sub = command.commands.get(arg)
        if sub is None:
            break
        command = sub
    accepts = any("--json" in getattr(param, "opts", ()) for param in getattr(command, "params", ()))
    return args if accepts else [a for a in args if a != "--json"]


@app.command("observe")
def observe(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable report."),
    project: str = PROJECT_OPTION,
) -> None:
    """How the workspace is doing, as tables: features, execution, model usage and cost, reviews, deviations, system."""
    from dridh.observability.metrics import collect
    from dridh.observability.tables import render

    report = collect(_workspace(project))
    typer.echo(json.dumps(report, indent=2, sort_keys=True, default=str) if json_output else render(report))


def _controller(project: str, model: str | None = None):  # type: ignore[no-untyped-def]
    from dridh.executor.controller import ExecutionController

    return ExecutionController(_workspace(project), model=model)


@execute_app.command("start")
def execute_start(
    feature: str,
    confirmed_by: str = typer.Option(..., "--confirmed-by", help="Who is starting this run."),
    model: str = typer.Option("", "--model", help="Provider/model for the Executor agent."),
    project: str = PROJECT_OPTION,
) -> None:
    """Start an execution run (typed confirmation) and drive it until it completes or pauses for review."""
    controller = _controller(project, model or None)
    challenge = controller.challenge(feature)
    _confirm(
        challenge["confirmation"],
        f"Execute {challenge['pending']} of {challenge['stories']} stories for '{challenge['feature']}' "
        f"(up to {challenge['max_parallel']} in parallel, {challenge['max_repairs']} repairs per story). "
        "Agents edit only story worktrees; nothing is pushed.",
    )
    run = controller.start(feature, confirmed_by=confirmed_by, confirmation=challenge["confirmation"])
    _emit(asyncio.run(controller.drive(run["id"])))


@execute_app.command("resume")
def execute_resume(
    run_id: str,
    model: str = typer.Option("", "--model"),
    project: str = PROJECT_OPTION,
) -> None:
    """Resume a paused run (for example after a review), or recover a run whose process died, and drive it again."""
    controller = _controller(project, model or None)
    controller.resume(run_id, actor=_operator())
    _emit(asyncio.run(controller.drive(run_id)))


@execute_app.command("status")
def execute_status(run_id: str, project: str = PROJECT_OPTION) -> None:
    """Show a run, its story counts, lots and attempts."""
    _emit(_controller(project).status(run_id), "execute-status")


@execute_app.command("stop")
def execute_stop(run_id: str, project: str = PROJECT_OPTION) -> None:
    """Stop a run. Completed work and story branches are kept."""
    _emit(_controller(project).stop(run_id, actor=_operator()))


@execute_app.command("review")
def execute_review(
    run_id: str,
    lot: int = typer.Argument(..., help="The lot number shown by `dridh execute status`."),
    verdict: str = typer.Option(..., "--verdict", help="approved or rework"),
    reviewer: str = typer.Option(..., "--reviewer"),
    notes: str = typer.Option("", "--notes", help="What must change (required for rework)."),
    project: str = PROJECT_OPTION,
) -> None:
    """Human-only whole-lot verdict at a checkpoint. Approval releases every story in the lot."""
    controller = _controller(project)
    challenge = controller.review_challenge(run_id, lot, verdict)
    shown = challenge["lot"]
    _confirm(
        challenge["confirmation"],
        f"Lot {shown['ordinal']} ({shown['kind'].lower()}): {', '.join(shown['story_ids'])}. Verdict: {verdict}. "
        "The verdict applies to the whole lot.",
    )
    _emit(controller.review_lot(run_id, lot, verdict=verdict, reviewer=reviewer, notes=notes, confirmation=challenge["confirmation"]))


@execute_app.command("discard")
def execute_discard(
    run_id: str,
    story_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    project: str = PROJECT_OPTION,
) -> None:
    """Throw away one story's worktree and branch so it can start again (run must be paused or stopped)."""
    controller = _controller(project)
    _confirm(f"DISCARD {story_id}", f"Delete the worktree and local branch of {story_id}; its attempts stay on record.")
    _emit(controller.discard_story(run_id, story_id, reviewer=reviewer))


deliver_app = typer.Typer(help="Push story branches, open PRs, evaluate checks and integrate (GitHub delivery).", no_args_is_help=True)
app.add_typer(deliver_app, name="deliver")


def _delivery(project: str):  # type: ignore[no-untyped-def]
    from dridh.config.dridh_yaml import load_config
    from dridh.delivery.engine import DeliveryEngine

    workspace = _workspace(project)
    config = load_config(workspace.root / "dridh.yaml")
    if not config.delivery.enabled:
        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Delivery is disabled.", recovery="Set delivery.enabled: true in .opencode/dridh/dridh.yaml.")
    return DeliveryEngine(workspace, config=config.delivery, execution=config.execution)


def _delivery_order(engine, run_id: str):  # type: ignore[no-untyped-def]
    from dridh.state.runs import RunRepository

    with engine._store() as store:  # noqa: SLF001
        run = RunRepository(store.read(), engine.workspace.workspace_id).get_run(run_id)
    return run, engine.planner.execution_stories(run["planner_session_id"])


@deliver_app.command("run")
def deliver_run(
    run_id: str,
    model: str = typer.Option("", "--model", help="Provider/model for CI-failure repairs by the Executor agent."),
    project: str = PROJECT_OPTION,
) -> None:
    """Push, open PRs, wait for checks (repairing failures a bounded number of times) and integrate every approved story."""
    engine = _delivery(project)
    run, stories = _delivery_order(engine, run_id)
    todo = [s for s in stories if s["status"] == "COMPLETED" and engine.gates(s["story_id"]).get("DEPENDENTS_UNLOCKED", {}).get("status") != "PASS"]
    if not todo:
        raise DridhError(ErrorCode.INVALID_TRANSITION, "No approved story is waiting for delivery.")
    remote, target = engine.config.remote, engine.config.target_branch
    _confirm(f"DELIVER {run_id[:8]} {len(todo)}",
             f"Push {len(todo)} story branch(es) and the integration branch to '{remote}', open PRs against '{target}' or stacked bases. "
             f"A failing check may trigger up to {engine.config.max_ci_repairs} Executor repair round(s) per story, pushed as ordinary commits. "
             "Nothing is force-pushed and nothing is merged.")
    runner = _controller(project, model or None)._story_runner_instance() if engine.config.max_ci_repairs > 0 else None  # noqa: SLF001
    results: list[dict[str, Any]] = []
    for story in todo:
        sid = story["story_id"]
        if any(engine.gates(dep).get("DEPENDENTS_UNLOCKED", {}).get("status") != "PASS" for dep in story["dependencies"]):
            results.append({"story_id": sid, "skipped": "a dependency is not integrated yet"})
            continue
        outcome = engine.deliver_story(run_id, sid, runner=runner)
        results.append(outcome)
        if not (outcome["integration"] or {}).get("unlocked"):
            break
    _emit({"run_id": run["id"], "results": results})


@deliver_app.command("checks")
def deliver_checks(story_id: str, project: str = PROJECT_OPTION) -> None:
    """Re-evaluate the required checks for a story's PR head commit once."""
    _emit(_delivery(project).refresh_checks(story_id))


@deliver_app.command("status")
def deliver_status(run_id: str, project: str = PROJECT_OPTION) -> None:
    """Show each story's delivery gates."""
    _emit({"run_id": run_id, "stories": _delivery(project).status(run_id)}, "deliver-status")


ui_app = typer.Typer(help="Read-only views for interfaces such as the OpenCode TUI plugin (JSON).", no_args_is_help=True)
app.add_typer(ui_app, name="ui", hidden=True)


@ui_app.command("state")
def ui_state(feature: str | None = typer.Option(None, "--feature"), project: str = PROJECT_OPTION) -> None:
    """Feature, stages, documents, reviews, runs and the next human action."""
    from dridh.ui.state import dump, workspace_state

    typer.echo(dump(workspace_state(_workspace(project), feature)))


@ui_app.command("document")
def ui_document(stage: str = typer.Argument(..., help="use_cases, design or plan"), feature: str | None = typer.Option(None, "--feature"),
                project: str = PROJECT_OPTION) -> None:
    """One reviewable document with its outline, validation, concerns, Jev and checklist."""
    from dridh.ui.state import document_view, dump

    typer.echo(dump(document_view(_workspace(project), stage, feature)))


@ui_app.command("metrics")
def ui_metrics(project: str = PROJECT_OPTION) -> None:
    """The observability tables and a one-line summary, for the panel's Metrics tab."""
    from dridh.observability.metrics import collect
    from dridh.observability.tables import build
    from dridh.ui.state import dump

    typer.echo(dump(build(collect(_workspace(project)))))


@ui_app.command("lot")
def ui_lot(run_id: str, lot: int, project: str = PROJECT_OPTION) -> None:
    """A checkpoint lot: each story's latest attempt, evidence and diff."""
    from dridh.ui.state import dump, lot_view

    typer.echo(dump(lot_view(_workspace(project), run_id, lot)))


recovery_app = typer.Typer(help="See and restore work Dridh saved before removing a worktree or branch.", no_args_is_help=True)
app.add_typer(recovery_app, name="recovery")


@recovery_app.command("list")
def recovery_list(project: str = PROJECT_OPTION) -> None:
    """List the recovery refs (snapshots of uncommitted work and pinned branch tips)."""
    from dridh.workspace.worktree import WorktreeManager

    _emit({"snapshots": WorktreeManager(_workspace(project).project_root).recovery_snapshots()})


@recovery_app.command("restore")
def recovery_restore(
    ref: str,
    directory: str = typer.Argument(..., help="A new directory to check the snapshot out into."),
    project: str = PROJECT_OPTION,
) -> None:
    """Check a recovery ref out into a new detached worktree. The snapshot itself is kept."""
    from dridh.workspace.worktree import WorktreeManager

    path = WorktreeManager(_workspace(project).project_root).restore_snapshot(ref, Path(directory).resolve())
    _emit({"restored": ref, "directory": str(path)})
