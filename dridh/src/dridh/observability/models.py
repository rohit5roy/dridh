"""Which model each Dridh agent uses, and why: read from configuration, no model involved.

OpenCode picks the model for an agent run in this order, and Dridh reads the same places:

1. the model Dridh passes for the run (``--model``), when the caller chose one;
2. the agent profile's own ``model:`` (the installed ``agents/dridh/**.md`` front matter);
3. the ``model`` of the OpenCode configuration: ``OPENCODE_CONFIG_CONTENT``, then the project's ``opencode.json(c)``,
   then the user's (``$OPENCODE_CONFIG_DIR`` or ``~/.config/opencode``);
4. otherwise OpenCode's own default, which Dridh cannot see: shown as "OpenCode default".

``dridh.yaml`` ``models.provider``/``models.default`` reach OpenCode through ``dridh start`` (step 3), so they are
reported there when nothing above overrides them. OpenCode's JSON events do not name the model a step ran on, so the
usage log records what this resolution said at the time of the run.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

OPENCODE_DEFAULT = "OpenCode default"
SOURCES = {
    "run": "chosen for the run",
    "label": "DRIDH_USAGE_MODEL_LABEL",
    "agent": "agent profile",
    "env": "OPENCODE_CONFIG_CONTENT",
    "project": "project opencode.json",
    "user": "user opencode.json",
    "dridh": "dridh.yaml",
    "default": "OpenCode chooses",
}
_FRONT_MODEL = re.compile(r"(?m)^model:\s*['\"]?([^'\"\n#]+?)['\"]?\s*$")


def _config_dir() -> Path:
    configured = os.environ.get("OPENCODE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".config" / "opencode"


def _jsonc_model(path: Path) -> str | None:
    """The top-level ``model`` of an ``opencode.json`` or ``.jsonc`` (comments and trailing commas allowed)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        value = json.loads(text).get("model")
    except (ValueError, AttributeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _config_model(directory: Path) -> str | None:
    for name in ("opencode.json", "opencode.jsonc"):
        if (directory / name).is_file() and (model := _jsonc_model(directory / name)):
            return model
    return None


def _profile(agent: str) -> Path | None:
    """The installed profile of ``dridh/workers/planner-indexer``, else the packaged one."""
    relative = agent.split("/", 1)[1] if agent.startswith("dridh/") else agent
    for root in (_config_dir() / "agents" / "dridh", _packaged_agents()):
        if root is not None and (root / f"{relative}.md").is_file():
            return root / f"{relative}.md"
    return None


def _packaged_agents() -> Path | None:
    try:
        from dridh.assets import default_agent_source

        return default_agent_source()
    except Exception:  # noqa: BLE001 - a source checkout without built assets has no packaged profiles
        return None


def _profile_model(agent: str) -> str | None:
    path = _profile(agent)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    front = text.split("---", 2)[1] if text.count("---") >= 2 else ""
    match = _FRONT_MODEL.search(front)
    return match.group(1).strip() if match else None


def _dridh_model(project_root: Path) -> str | None:
    try:
        from dridh.config.dridh_yaml import CONFIG_FILENAME, load_config

        models = load_config(project_root / ".opencode" / "dridh" / CONFIG_FILENAME).models
    except Exception:  # noqa: BLE001 - no or unreadable dridh.yaml: nothing to report from it
        return None
    return f"{models.provider}/{models.default}" if models.provider else None


def shared_model(project_root: str | Path) -> tuple[str, str]:
    """(model, source) every agent without its own ``model:`` runs on."""
    root = Path(project_root)
    content = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if content:
        try:
            value = json.loads(content).get("model")
            if isinstance(value, str) and value.strip():
                return value.strip(), "env"
        except (ValueError, AttributeError):
            pass
    for directory, source in ((root, "project"), (root / ".opencode", "project"), (_config_dir(), "user")):
        if model := _config_model(directory):
            return model, source
    if model := _dridh_model(root):
        return model, "dridh"
    return OPENCODE_DEFAULT, "default"


def resolve(project_root: str | Path, agent: str, passed: str | None = None) -> tuple[str, str]:
    """(model, source key) for one run of ``agent``."""
    if passed:
        return passed, "run"
    if label := os.environ.get("DRIDH_USAGE_MODEL_LABEL"):
        return label, "label"
    if model := _profile_model(agent):
        return model, "agent"
    return shared_model(project_root)


def agents() -> list[str]:
    """Every Dridh agent role, ``dridh/coordinator`` first, from the packaged profiles."""
    root = _packaged_agents()
    if root is None or not root.is_dir():
        return []
    names = sorted("dridh/" + str(p.relative_to(root).with_suffix("")).replace(os.sep, "/") for p in root.rglob("*.md"))
    return sorted(names, key=lambda n: (n != "dridh/coordinator", "/workers/" in n, n))


def agent_models(project_root: str | Path, entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per agent: the model it will use now and why, plus the model and time of its last recorded run."""
    from dridh.observability.usage import stage_of

    last: dict[str, dict[str, Any]] = {}
    runs: dict[str, int] = {}
    for entry in entries or []:
        name = str(entry.get("agent") or "")
        if name:
            last[name] = entry
            runs[name] = runs.get(name, 0) + 1
    rows = []
    for name in dict.fromkeys([*agents(), *last]):
        model, source = resolve(project_root, name)
        previous = last.get(name) or {}
        rows.append({"agent": name, "stage": stage_of(name), "model": model, "source": SOURCES[source],
                     "runs": runs.get(name, 0), "last_model": previous.get("model"), "last_at": previous.get("at")})
    return rows


def headline(rows: list[dict[str, Any]]) -> str:
    """One line for the panel: ``Models: deepseek-v4.1-flash-free (all 14 agents)`` or the split by model."""
    if not rows:
        return ""
    by_model: dict[str, list[str]] = {}
    for row in rows:
        by_model.setdefault(row["model"], []).append(row["agent"])
    if len(by_model) == 1:
        model = next(iter(by_model))
        return f"Models: {model} (all {len(rows)} agents)"
    parts = sorted(by_model.items(), key=lambda item: -len(item[1]))
    return "Models: " + " · ".join(f"{model} ({len(names)} agent{'s' if len(names) != 1 else ''})" for model, names in parts)
