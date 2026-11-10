"""Plan contract: parse and validate a Planner result before any state is changed.

Validation collects every error, so one correction round can fix them all. A
plan is accepted only when it is complete, acyclic, uses executable agents only,
keeps every path inside the workspace, and uses acceptance checks from an
allow-list. Nothing in the plan is executed at this stage.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from dridh.common.errors import DridhError, ErrorCode

PLAN_SCHEMA = "dridh.plan"
PLAN_SCHEMA_VERSION = 1
MAX_STAGES = 10
MAX_TASKS = 60
MAX_ACCEPTANCE = 10
MAX_ALLOWED_PATHS = 20
MAX_INSTRUCTIONS = 4000
MAX_ATTEMPTS = 4  # initial attempt plus up to three repairs
EXECUTABLE_AGENTS = frozenset({"dridh/ideator", "dridh/planner", "dridh/designer", "dridh/code-reacher", "dridh/documents"})
STAGE_ID = re.compile(r"^S-\d{2}$")
TASK_ID = re.compile(r"^T-\d{3}$")
CHECK_TYPES = frozenset({"file_exists", "file_contains", "command"})
PYTHON_MODULES = frozenset({"unittest", "pytest"})
FORBIDDEN_ARGUMENT_CHARACTERS = frozenset(";&|`$<>\n\r")
PROTECTED_PREFIXES = (".opencode", ".git")


@dataclass(frozen=True)
class AcceptanceCheck:
    type: str
    path: str | None = None
    text: str | None = None
    argv: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.type == "command":
            return "command: " + " ".join(self.argv)
        if self.type == "file_contains":
            return f"file_contains: {self.path}"
        return f"file_exists: {self.path}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.path is not None:
            payload["path"] = self.path
        if self.text is not None:
            payload["text"] = self.text
        if self.argv:
            payload["argv"] = list(self.argv)
        return payload


@dataclass(frozen=True)
class TaskSpec:
    id: str
    stage_id: str
    ordinal: int
    title: str
    agent: str
    instructions: str
    allowed_paths: tuple[str, ...]
    depends_on: tuple[str, ...]
    acceptance: tuple[AcceptanceCheck, ...]
    max_attempts: int
    parent_task_id: str | None = None


@dataclass(frozen=True)
class StageSpec:
    id: str
    ordinal: int
    title: str


@dataclass(frozen=True)
class PlanSpec:
    objective: str
    stages: tuple[StageSpec, ...]
    tasks: tuple[TaskSpec, ...]
    acceptance: tuple[AcceptanceCheck, ...]
    sha256: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object in a model reply, from a fenced block or inline."""

    candidates: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(match.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _safe_relative_path(value: Any, errors: list[str], where: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: path must be a non-empty string")
        return None
    normalised = value.strip().replace("\\", "/")
    path = PurePosixPath(normalised)
    if path.is_absolute() or ".." in path.parts or normalised.startswith("~"):
        errors.append(f"{where}: path must be relative and must not contain '..': {value}")
        return None
    cleaned = str(path)
    if cleaned in (".", "") or cleaned.split("/")[0] in PROTECTED_PREFIXES:
        errors.append(f"{where}: path is reserved for Dridh or version control: {value}")
        return None
    return cleaned


def _parse_check(raw: Any, errors: list[str], where: str) -> AcceptanceCheck | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}: acceptance check must be an object")
        return None
    kind = raw.get("type")
    if kind not in CHECK_TYPES:
        errors.append(f"{where}: unknown acceptance check type {kind!r}; allowed: {sorted(CHECK_TYPES)}")
        return None
    if kind in ("file_exists", "file_contains"):
        path = _safe_relative_path(raw.get("path"), errors, where)
        if path is None:
            return None
        if kind == "file_contains":
            text = raw.get("text")
            if not isinstance(text, str) or not text or len(text) > 500:
                errors.append(f"{where}: file_contains needs a non-empty text of at most 500 characters")
                return None
            return AcceptanceCheck(type=kind, path=path, text=text)
        return AcceptanceCheck(type=kind, path=path)
    argv = raw.get("argv")
    if not isinstance(argv, list) or not 3 <= len(argv) <= 12 or not all(isinstance(item, str) for item in argv):
        errors.append(f"{where}: command argv must be a list of 3 to 12 strings")
        return None
    if argv[0] != "python3" or argv[1] != "-m" or argv[2] not in PYTHON_MODULES:
        errors.append(f"{where}: only 'python3 -m unittest' or 'python3 -m pytest' commands are allowed")
        return None
    for argument in argv[3:]:
        if len(argument) > 200 or any(character in FORBIDDEN_ARGUMENT_CHARACTERS for character in argument):
            errors.append(f"{where}: argument is too long or contains a forbidden character: {argument[:40]!r}")
            return None
        if argument.startswith("/") or ".." in argument.split("/"):
            errors.append(f"{where}: command arguments must be relative paths: {argument!r}")
            return None
    return AcceptanceCheck(type=kind, argv=tuple(argv))


def _parse_checks(raw: Any, errors: list[str], where: str, *, required: bool) -> tuple[AcceptanceCheck, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        errors.append(f"{where}: acceptance must be a list")
        return ()
    if required and not raw:
        errors.append(f"{where}: at least one acceptance check is required")
    if len(raw) > MAX_ACCEPTANCE:
        errors.append(f"{where}: at most {MAX_ACCEPTANCE} acceptance checks are allowed")
    checks = []
    for index, item in enumerate(raw[:MAX_ACCEPTANCE]):
        check = _parse_check(item, errors, f"{where}[{index}]")
        if check is not None:
            checks.append(check)
    return tuple(checks)


def parse_plan(document: dict[str, Any], raw_text: str) -> PlanSpec:
    """Validate a parsed plan document. Raises ``PLAN_INVALID`` with every error found."""

    errors: list[str] = []
    if document.get("schema") != PLAN_SCHEMA or document.get("schema_version") != PLAN_SCHEMA_VERSION:
        errors.append(f"schema must be {PLAN_SCHEMA!r} version {PLAN_SCHEMA_VERSION}")
    objective = document.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 500:
        errors.append("objective must be a non-empty string of at most 500 characters")
        objective = ""
    raw_stages = document.get("stages")
    if not isinstance(raw_stages, list) or not 1 <= len(raw_stages) <= MAX_STAGES:
        errors.append(f"stages must be a list of 1 to {MAX_STAGES} stages")
        raw_stages = []

    stages: list[StageSpec] = []
    tasks: list[TaskSpec] = []
    seen_stages: set[str] = set()
    seen_tasks: set[str] = set()
    ordinal = 0
    for stage_index, raw_stage in enumerate(raw_stages):
        where = f"stages[{stage_index}]"
        if not isinstance(raw_stage, dict):
            errors.append(f"{where}: stage must be an object")
            continue
        stage_id = raw_stage.get("id")
        if not isinstance(stage_id, str) or not STAGE_ID.match(stage_id):
            errors.append(f"{where}: stage id must match S-NN")
            continue
        if stage_id in seen_stages:
            errors.append(f"{where}: duplicate stage id {stage_id}")
            continue
        seen_stages.add(stage_id)
        title = raw_stage.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{where}: stage title is required")
            title = ""
        stages.append(StageSpec(id=stage_id, ordinal=stage_index + 1, title=title))
        raw_tasks = raw_stage.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            errors.append(f"{where}: a stage needs at least one task")
            continue
        for task_index, raw_task in enumerate(raw_tasks):
            task_where = f"{where}.tasks[{task_index}]"
            if not isinstance(raw_task, dict):
                errors.append(f"{task_where}: task must be an object")
                continue
            task_id = raw_task.get("id")
            if not isinstance(task_id, str) or not TASK_ID.match(task_id):
                errors.append(f"{task_where}: task id must match T-NNN")
                continue
            if task_id in seen_tasks:
                errors.append(f"{task_where}: duplicate task id {task_id}")
                continue
            seen_tasks.add(task_id)
            ordinal += 1
            agent = raw_task.get("agent")
            if agent not in EXECUTABLE_AGENTS:
                errors.append(f"{task_where}: agent {agent!r} is not executable; allowed: {sorted(EXECUTABLE_AGENTS)}")
            instructions = raw_task.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > MAX_INSTRUCTIONS:
                errors.append(f"{task_where}: instructions must be a non-empty string of at most {MAX_INSTRUCTIONS} characters")
                instructions = ""
            raw_paths = raw_task.get("allowed_paths")
            if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= MAX_ALLOWED_PATHS:
                errors.append(f"{task_where}: allowed_paths must list 1 to {MAX_ALLOWED_PATHS} paths")
                raw_paths = []
            paths = []
            for path_index, value in enumerate(raw_paths):
                cleaned = _safe_relative_path(value, errors, f"{task_where}.allowed_paths[{path_index}]")
                if cleaned is not None:
                    paths.append(cleaned.rstrip("/"))
            raw_depends = raw_task.get("depends_on", [])
            if not isinstance(raw_depends, list) or not all(isinstance(item, str) for item in raw_depends):
                errors.append(f"{task_where}: depends_on must be a list of task ids")
                raw_depends = []
            parent_task_id = raw_task.get("parent_task_id")
            if parent_task_id is not None:
                if not isinstance(parent_task_id, str) or parent_task_id not in seen_tasks:
                    errors.append(f"{task_where}: parent_task_id must reference an earlier task")
                    parent_task_id = None
            raw_attempts = raw_task.get("max_attempts", MAX_ATTEMPTS)
            if not isinstance(raw_attempts, int) or not 1 <= raw_attempts <= MAX_ATTEMPTS:
                errors.append(f"{task_where}: max_attempts must be an integer from 1 to {MAX_ATTEMPTS}")
                raw_attempts = MAX_ATTEMPTS
            title = raw_task.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"{task_where}: task title is required")
                title = ""
            checks = _parse_checks(raw_task.get("acceptance"), errors, f"{task_where}.acceptance", required=True)
            tasks.append(
                TaskSpec(
                    id=task_id,
                    stage_id=stage_id,
                    ordinal=ordinal,
                    title=title,
                    agent=str(agent),
                    instructions=instructions,
                    allowed_paths=tuple(paths),
                    depends_on=tuple(raw_depends),
                    acceptance=checks,
                    max_attempts=raw_attempts,
                    parent_task_id=parent_task_id,
                )
            )

    if len(tasks) > MAX_TASKS:
        errors.append(f"at most {MAX_TASKS} tasks are allowed")
    order = {task.id: task.ordinal for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            if dependency == task.id:
                errors.append(f"{task.id}: a task cannot depend on itself")
            elif dependency not in order:
                errors.append(f"{task.id}: depends_on refers to an unknown task {dependency}")
            elif order[dependency] >= task.ordinal:
                errors.append(f"{task.id}: depends_on {dependency} must appear earlier in the plan")

    plan_checks = _parse_checks(document.get("acceptance"), errors, "acceptance", required=False)

    if errors:
        raise DridhError(
            ErrorCode.PLAN_INVALID,
            "The plan failed validation.",
            details=tuple(errors[:60]),
            recovery="Ask the Planner for a corrected plan that fixes every listed error, then submit it again.",
        )
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return PlanSpec(
        objective=objective,
        stages=tuple(stages),
        tasks=tuple(tasks),
        acceptance=plan_checks,
        sha256=digest,
    )


def parse_plan_text(text: str) -> PlanSpec:
    document = extract_json_object(text)
    if document is None:
        raise DridhError(
            ErrorCode.PLAN_INVALID,
            "The Planner reply contains no JSON plan object.",
            recovery="Ask the Planner to reply with exactly one JSON plan object.",
        )
    return parse_plan(document, json.dumps(document, sort_keys=True, separators=(",", ":")))
