"""Agent roster, prompt contracts and the worker result contract."""

from __future__ import annotations

from dataclasses import dataclass

from dridh.common.errors import DridhError, ErrorCode
from dridh.orchestration.plan import TaskSpec, extract_json_object


@dataclass(frozen=True)
class AgentProfile:
    id: str
    role: str
    mode: str
    executable: bool
    purpose: str


ROSTER: tuple[AgentProfile, ...] = (
    AgentProfile("dridh/coordinator", "Coordinator", "primary", False, "Interprets the objective, drives the workflow through bridge operations and reports the result."),
    AgentProfile("dridh/planner", "Planner", "subagent", False, "Coordinates approved-design decomposition, central/component/test plans and the validated story DAG; retains the generic workflow planning contract for legacy objectives."),
    AgentProfile("dridh/code-reacher", "Code Reacher", "subagent", True, "Implements one approved task inside its allowed paths and reports changed files."),
    AgentProfile("dridh/ideator", "Ideator", "subagent", False, "Guides resumable requirements discovery and coordinates the Use Case review gate."),
    AgentProfile("dridh/workers/ideator-inquirer", "Ideator Inquirer", "subagent", False, "Summarises captured requirements and identifies unresolved discovery gaps."),
    AgentProfile("dridh/workers/ideator-doc-generator", "Ideator Document Generator", "subagent", False, "Proposes a structured Use Case model from the persisted discovery record."),
    AgentProfile("dridh/workers/ideator-handoff", "Ideator Handoff", "subagent", False, "Checks semantic coverage and prepares the human review handoff."),
    AgentProfile("dridh/technical-designer", "Technical Designer", "subagent", False, "Runs approved-baseline technical inquiry, generates TDDs and prepares human review."),
    AgentProfile("dridh/workers/designer-inquirer", "Technical Inquirer", "subagent", False, "Analyses approved Use Cases and evidence, identifies technical gaps and structures inquiry."),
    AgentProfile("dridh/workers/designer-doc-generator", "TDD Document Generator", "subagent", False, "Proposes TDD sections, components, feature decisions and full Use Case/flow traceability."),
    AgentProfile("dridh/workers/designer-handoff", "Designer Handoff", "subagent", False, "Checks TDD coverage and prepares the human review checklist."),
    AgentProfile("dridh/workers/planner-indexer", "Central Plan Indexer", "subagent", False, "Maps approved Use Cases and TDD components into a traceable story DAG."),
    AgentProfile("dridh/workers/planner-component", "Component Planner", "subagent", False, "Produces component boundaries, story ownership and component-level plan details."),
    AgentProfile("dridh/workers/planner-testing", "Testing Planner", "subagent", False, "Produces Table 15 test phases using real dependencies and justified fallback modes only."),
    AgentProfile("dridh/designer", "Designer", "subagent", False, "Legacy generic design profile retained for compatibility."),
    AgentProfile("dridh/documents", "Documents", "subagent", False, "Drafts and validates structured documents. Defined for later phases."),
    AgentProfile("dridh/executor", "Executor", "subagent", False, "Implements one story of a human-approved plan inside its own worktree. Started only by the run controller after a typed per-run confirmation; never schedulable by the generic task engine."),
)
PROFILES = {profile.id: profile for profile in ROSTER}
COORDINATOR = "dridh/coordinator"
PLANNER = "dridh/planner"
WORKER_RESULT_STATUSES = frozenset({"DONE", "BLOCKED"})


def planner_prompt(objective: str, *, feedback: tuple[str, ...] = ()) -> str:
    return (
        "You are dridh/planner. Produce one plan for the objective below.\n"
        "Reply with exactly one JSON object and nothing else, using this shape:\n"
        '{"schema": "dridh.plan", "schema_version": 1, "objective": "...", '
        '"stages": [{"id": "S-01", "title": "...", "tasks": [{"id": "T-001", "title": "...", '
        '"agent": "dridh/code-reacher", "depends_on": [], "allowed_paths": ["src/"], '
        '"instructions": "...", "max_attempts": 4, "acceptance": [{"type": "file_exists", "path": "src/x.py"}]}]}]}\n'
        "Acceptance check types: file_exists (path), file_contains (path, text), "
        "command (argv: ['python3', '-m', 'unittest' or 'pytest', ...]).\n"
        f"Objective: {objective}\n"
        + ("\nCorrection feedback from the previous attempt (fix only these issues):\n" + "\n".join(f"- {item}" for item in feedback) + "\nPreserve valid identifiers, constraints and unrelated plan sections. Do not make unrelated changes.\n" if feedback else "")
    )


def worker_prompt(task: TaskSpec, objective: str, *, feedback: tuple[str, ...] = ()) -> str:
    checks = [check.describe() for check in task.acceptance]
    return (
        f"You are {task.agent}. Complete task {task.id} for the workflow objective below.\n"
        f"Objective: {objective}\n"
        f"Task: {task.title}\n"
        f"Instructions: {task.instructions}\n"
        f"You may create or change files only under: {', '.join(task.allowed_paths)}.\n"
        f"Deterministic acceptance checks that will run after you finish: {'; '.join(checks)}.\n"
        "Reply with exactly one JSON object and nothing else:\n"
        '{"status": "DONE" or "BLOCKED", "summary": "one sentence", "files_changed": ["relative/path"]}\n'
        "Report BLOCKED with a summary if you cannot complete the task. Your report is not proof of completion; "
        "the checks decide."
        + ("\nCorrection feedback from the previous attempt (fix only these issues):\n" + "\n".join(f"- {item}" for item in feedback) + "\nFix only the listed files/sections. Preserve unrelated content and identifiers. Do not make unrelated changes.\n" if feedback else "")
    )


@dataclass(frozen=True)
class WorkerResult:
    status: str
    summary: str
    files_changed: tuple[str, ...]


def parse_worker_result(text: str) -> WorkerResult:
    document = extract_json_object(text)
    if document is None:
        raise DridhError(
            ErrorCode.WORKER_FAILED,
            "The worker reply contains no result object.",
            recovery="Retry the task. The worker must reply with the documented JSON result.",
        )
    status = document.get("status")
    if status not in WORKER_RESULT_STATUSES:
        raise DridhError(ErrorCode.WORKER_FAILED, "The worker result has an invalid status.", details=(str(status),))
    summary = document.get("summary")
    files = document.get("files_changed", [])
    if not isinstance(summary, str) or not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise DridhError(ErrorCode.WORKER_FAILED, "The worker result fields have the wrong types.")
    return WorkerResult(status=str(status), summary=summary[:500], files_changed=tuple(files))


def _relative(path: str) -> str:
    """Drop leading "./" segments only. ``lstrip("./")`` would also eat the dot of ``.github`` or ``.gitignore``."""
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def paths_within(allowed: tuple[str, ...], changed: tuple[str, ...]) -> list[str]:
    """Return the changed paths that fall outside every allowed prefix."""

    prefixes = [_relative(prefix).rstrip("/") for prefix in allowed]
    outside: list[str] = []
    for item in changed:
        normalised = _relative(item)
        if not any(normalised == prefix or normalised.startswith(prefix + "/") for prefix in prefixes):
            outside.append(item)
    return outside
