"""``dridh doctor``: read-only environment and installation checks (plan sections 2.1 and 4).

Every check reports PASS, WARN or FAIL with the evidence and, when not PASS, a remedy.

* FAIL  - a mandatory prerequisite is missing or the installation violates a plan invariant
          (OpenCode V2 absent, runtime not installed, the Coordinator set as the global default agent,
          an unreadable or unsupported workspace database). ``dridh doctor`` exits 3.
* WARN  - an optional or later-phase capability is unavailable (Docker, sqlite-vec, Tree-sitter, provider
          credentials). The harness works in a degraded mode for the affected feature.

The module never writes, never migrates and never creates directories. It reports credential *names*
only, never values. Subprocesses use argument arrays with short timeouts. Heavy packages (FastEmbed,
ONNX Runtime, Tree-sitter, sqlite-vec) are located with ``importlib.util.find_spec`` and never imported,
so the command stays cheap (Decision D-P0-09).
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dridh.install.installer import resolve_layout
from dridh.install.installer import status as runtime_status
from dridh.state.store import STATE_SCHEMA_VERSION

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# Environment variable *names* that may hold provider credentials (values are never read into output).
CREDENTIAL_NAMES = ("OPENROUTER_API_KEY", "OPENCODE_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL", "ANTHROPIC_API_KEY")

# Optional native/heavy capabilities (GraphQLite is not listed: Code Reacher v1 does not use it, D-P14-05): (importable module, label, what happens without it, how to get it).
OPTIONAL_PACKAGES = (
    ("tree_sitter", "Tree-sitter parser (Code Reacher)", "Code Reacher cannot parse source files", "Reinstall Dridh; tree-sitter is a runtime dependency."),
    ("sqlite_vec", "sqlite-vec vector search (Code Reacher)", "an exact search over stored vectors is used instead", "Reinstall Dridh; sqlite-vec is a runtime dependency."),
    ("fastembed", "FastEmbed local embeddings (Code Reacher)", "a deterministic hashing embedder is used and semantic results report degraded",
     "Install the optional extra: uv tool install --with fastembed==0.9.1 dridh  (or pip install 'dridh[embeddings]'); the model (about 65 MB) downloads from huggingface.co on first use into ~/.cache/dridh/fastembed."),
)

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    status: str
    detail: str
    remedy: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail, "remedy": self.remedy}


def _run(argv: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, shell=False)  # noqa: S603


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


OPENCODE_MINIMUM = (2, 2, 5)  # oldest supported OpenCode V2 release; CI tests the lock-pinned version (.github/scripts/opencode-cli)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def check_opencode(env: Mapping[str, str], run: Runner) -> Check:
    executable = env.get("DRIDH_OPENCODE_BIN") or shutil.which("opencode")
    remedy = "Install OpenCode V2 (https://opencode.ai) or set DRIDH_OPENCODE_BIN to its executable."
    if not executable:
        return Check("opencode", "OpenCode V2 CLI", FAIL, "opencode was not found on PATH and DRIDH_OPENCODE_BIN is not set.", remedy)
    try:
        completed = run([executable, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("opencode", "OpenCode V2 CLI", FAIL, f"{executable} could not be run: {type(exc).__name__}.", remedy)
    version = _first_line(completed.stdout or completed.stderr)
    found = _version_tuple(version)
    if completed.returncode != 0 or found is None or found[0] != 2:
        return Check("opencode", "OpenCode V2 CLI", FAIL, f"{executable} reports '{version or 'no version'}'; OpenCode V2 is required.", remedy)
    minimum = ".".join(map(str, OPENCODE_MINIMUM))
    if found < OPENCODE_MINIMUM:
        return Check("opencode", "OpenCode V2 CLI", FAIL, f"{executable} is {version}; Dridh needs OpenCode {minimum} or later.",
                     f"Upgrade OpenCode: npm install -g @opencode/cli@latest (any V2 release from {minimum} on).")
    return Check("opencode", "OpenCode V2 CLI", PASS, f"{executable} ({version})")


def check_runtime(config_dir: str | None) -> list[Check]:
    layout = resolve_layout(config_dir, None)
    report = runtime_status(layout)
    checks = [Check("config-dir", "OpenCode configuration directory", PASS if layout.config_dir.is_dir() else WARN,
                    str(layout.config_dir) + ("" if layout.config_dir.is_dir() else " (does not exist yet)"),
                    "" if layout.config_dir.is_dir() else "It is created by `dridh install`.")]
    if not report["installed"]:
        checks.append(Check("runtime", "Dridh runtime, agents and plugin", FAIL,
                            "No install manifest was found.", "Run `dridh install` (use --dry-run first to preview)."))
    else:
        bad = {name: state for name, state in report["files"].items() if state != "ok"}
        if bad:
            sample = ", ".join(f"{name} ({state})" for name, state in sorted(bad.items())[:4])
            checks.append(Check("runtime", "Dridh runtime, agents and plugin", FAIL,
                                f"{len(bad)} of {len(report['files'])} managed files differ from the manifest: {sample}.",
                                "Run `dridh upgrade --force` to restore them (the originals are backed up first; your own files are never touched)."))
        else:
            coordinator = "agents/dridh/coordinator.md" in report["files"]
            checks.append(Check("runtime", "Dridh runtime, agents and plugin", PASS if coordinator else FAIL,
                                f"{len(report['files'])} managed files match the manifest."
                                + ("" if coordinator else " dridh/coordinator is not among them."),
                                "" if coordinator else "Run `dridh upgrade`."))
        checks.append(Check("plugin-entry", "Plugin registered in the OpenCode config",
                            PASS if report["plugin_entry_present"] else WARN,
                            "present" if report["plugin_entry_present"] else "no Dridh plugin entry in the config file",
                            "" if report["plugin_entry_present"] else "Run `dridh install` or `dridh upgrade`."))
    if report["default_agent_is_coordinator"]:
        checks.append(Check("default-agent", "Global default agent untouched", FAIL,
                            "default_agent is set to dridh/coordinator, which would affect ordinary OpenCode sessions (decision D-05).",
                            "Remove default_agent from the OpenCode config; start Dridh sessions with `dridh session start`."))
    else:
        detail = "default_agent is not set" if report["default_agent"] is None else f"default_agent is '{report['default_agent']}' (not Dridh)"
        checks.append(Check("default-agent", "Global default agent untouched", PASS, detail))
    return checks


def check_workspace(project: Path | None) -> Check | None:
    if project is None:
        return None
    database = project / ".opencode" / "dridh" / "state" / "dridh.sqlite"
    if not (project / ".opencode" / "dridh" / "workspace.json").is_file():
        return Check("workspace", "Project workspace", WARN, f"{project} has no Dridh workspace.", "Run `dridh init --mode new|existing`.")
    if not database.is_file():
        return Check("workspace", "Project workspace", FAIL, "workspace.json exists but the state database is missing.",
                     "Restore it with `dridh restore` or re-run `dridh init`.")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            quick = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return Check("workspace", "Project workspace", FAIL, f"The state database could not be read: {type(exc).__name__}.",
                     "Run `dridh integrity`, then `dridh restore` from a backup if needed.")
    version = row[0] if row else None
    if quick and quick[0] != "ok":
        return Check("workspace", "Project workspace", FAIL, "SQLite quick_check reported a problem.", "Run `dridh integrity`; restore from a backup.")
    if version is None or int(version) > STATE_SCHEMA_VERSION:
        return Check("workspace", "Project workspace", FAIL, f"State schema version is {version}; this runtime supports up to {STATE_SCHEMA_VERSION}.",
                     "Upgrade Dridh (`dridh upgrade`) so it can read this workspace.")
    if int(version) < STATE_SCHEMA_VERSION:
        return Check("workspace", "Project workspace", WARN, f"State schema version is {version}; current is {STATE_SCHEMA_VERSION}.", "Run `dridh migrate`.")
    return Check("workspace", "Project workspace", PASS, f"state schema version {version}, integrity ok")


def check_sqlite() -> list[Check]:
    checks: list[Check] = []
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        checks.append(Check("sqlite-fts5", "SQLite FTS5 full-text search", PASS, f"SQLite {sqlite3.sqlite_version}"))
    except sqlite3.Error:
        checks.append(Check("sqlite-fts5", "SQLite FTS5 full-text search", FAIL, "This Python's SQLite lacks FTS5.",
                            "Use a Python build whose SQLite includes FTS5."))
    can_load = hasattr(sqlite3.Connection, "enable_load_extension")
    checks.append(Check("sqlite-extensions", "SQLite loadable extensions", PASS if can_load else WARN,
                        "supported" if can_load else "this Python was built without loadable-extension support",
                        "" if can_load else "Vector and graph search will use the relational fallback."))
    return checks


def check_optional_packages() -> list[Check]:
    checks = []
    for module, label, without, remedy in OPTIONAL_PACKAGES:
        found = importlib.util.find_spec(module) is not None
        checks.append(Check(f"pkg-{module}", label, PASS if found else WARN,
                            "installed" if found else f"not installed: {without}",
                            "" if found else remedy))
    return checks


def check_git(run: Runner) -> Check:
    if not shutil.which("git"):
        return Check("git", "Git", FAIL, "git was not found on PATH.", "Install Git; worktrees and branches depend on it.")
    version = _first_line(run(["git", "--version"]).stdout)
    identity = [run(["git", "config", "--get", key]).stdout.strip() for key in ("user.name", "user.email")]
    if not all(identity):
        return Check("git", "Git", WARN, f"{version}; user.name/user.email are not both configured.",
                     "Set them with `git config --global user.name/user.email` before Dridh creates commits.")
    return Check("git", "Git", PASS, version)


def check_glow(env: Mapping[str, str], run: Runner) -> Check:
    """glow renders documents in the OpenCode TUI reviewer; without it the reviewer shows the raw Markdown."""
    binary = env.get("DRIDH_GLOW") or shutil.which("glow")
    if not binary:
        return Check("glow", "glow (document rendering)", WARN, "glow was not found; the TUI reviewer shows raw Markdown.",
                     "Re-run install.sh, or install glow (https://github.com/charmbracelet/glow) and put it on PATH.")
    try:
        version = _first_line(run([binary, "--version"]).stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("glow", "glow (document rendering)", WARN, f"{binary} could not run: {type(exc).__name__}.", "Reinstall glow.")
    return Check("glow", "glow (document rendering)", PASS, version or binary)


def check_docker(run: Runner) -> Check:
    if not shutil.which("docker"):
        return Check("docker", "Docker", WARN, "docker was not found on PATH.", "Needed for Testcontainers/Compose tests; install Docker.")
    try:
        completed = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("docker", "Docker", WARN, f"docker could not be queried: {type(exc).__name__}.", "Start the Docker daemon.")
    if completed.returncode != 0:
        return Check("docker", "Docker", WARN, "the Docker daemon is not reachable.", "Start Docker (needed for Testcontainers/Compose tests).")
    return Check("docker", "Docker", PASS, f"daemon {_first_line(completed.stdout)}")


def check_credentials(env: Mapping[str, str]) -> Check:
    present = [name for name in CREDENTIAL_NAMES if env.get(name)]
    auth_file = Path(env.get("OPENCODE_AUTH_FILE") or Path.home() / ".local" / "share" / "opencode" / "auth.json")
    stored = auth_file.is_file()
    if present or stored:
        parts = ([f"environment: {', '.join(present)}"] if present else []) + (["OpenCode auth store present"] if stored else [])
        return Check("credentials", "Model provider credentials", PASS, "; ".join(parts) + " (values are never shown)")
    return Check("credentials", "Model provider credentials", WARN, "no provider credential variable or OpenCode auth store was found.",
                 "Run `opencode auth login`, or export your provider's key variable.")


def run_checks(
    *, config_dir: str | None = None, project: Path | None = None,
    env: Mapping[str, str] | None = None, run: Runner | None = None,
) -> list[Check]:
    env = os.environ if env is None else env
    run = run or _run
    checks = [check_opencode(env, run), *check_runtime(config_dir)]
    workspace = check_workspace(project)
    if workspace is not None:
        checks.append(workspace)
    checks += [*check_sqlite(), *check_optional_packages(), check_git(run), check_glow(env, run), check_docker(run), check_credentials(env)]
    return checks


def summarize(checks: list[Check]) -> dict[str, Any]:
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for check in checks:
        counts[check.status] += 1
    return {"ok": counts[FAIL] == 0, "counts": counts}


def render_text(checks: list[Check]) -> str:
    marks = {PASS: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = []
    for check in checks:
        lines.append(f"{marks[check.status]} {check.title}: {check.detail}")
        if check.remedy and check.status != PASS:
            lines.append(f"         -> {check.remedy}")
    counts = summarize(checks)["counts"]
    lines.append("")
    lines.append(f"{counts[PASS]} passed, {counts[WARN]} warnings, {counts[FAIL]} failed")
    return "\n".join(lines)
