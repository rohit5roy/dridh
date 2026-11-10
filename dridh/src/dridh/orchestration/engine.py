"""Workflow service: sequential, dependency-aware, recoverable orchestration.

The engine is the only writer of workflow state. It enforces:

* transitions through ``model`` (every other change is refused);
* one task in progress at a time, always the first non-terminal task in plan order;
* dependencies that are complete before a task starts;
* approval of the plan before any worker runs (fail closed);
* deterministic acceptance checks, whatever the worker reports;
* leases with heartbeats, so an interrupted run is recovered on the next call;
* cancellation requested from another process, and termination of the worker
  when the process that started it has gone (the orphan guard).

Workers run as one-shot OpenCode sessions through ``OpenCodeRunner``. Each run
is recorded with its session ID, exit code, duration and output digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.integrations.opencode import AgentErrorCode, AgentRunError, AgentRunResult, OpenCodeRunner
from dridh.jev.evaluation import JevEvaluationService, default_evaluation_questions
from dridh.orchestration.acceptance import run_checks
from dridh.orchestration.model import (
    TASK_TERMINAL,
    WORKFLOW_TERMINAL,
    TaskState,
    WorkflowState,
    require_task_transition,
    require_workflow_transition,
)
from dridh.orchestration.plan import PlanSpec, TaskSpec, parse_plan_text
from dridh.orchestration.roster import (
    PLANNER,
    parse_worker_result,
    paths_within,
    planner_prompt,
    worker_prompt,
)
from dridh.orchestration.scheduler import (
    CycleError,
    MissingDependencyError,
    ScheduleState,
    TaskNode,
    blocked_tasks,
    build_graph,
    can_start,
    is_finished,
    mark_completed,
    mark_failed,
    mark_started,
    ready_tasks,
)
from dridh.state.store import StateStore, append_ledger, canonical_json, utc_now, verify_ledger
from dridh.workspace.layout import WorkspaceInfo
from dridh.workspace.worktree import WorktreeManager

MAX_OBJECTIVE = 500
SNAPSHOT_SKIP = frozenset({".opencode", ".git", "__pycache__", ".venv", "node_modules"})
MAX_SNAPSHOT_BYTES = 5_000_000


def _snapshot(root: Path) -> dict[str, str]:
    """Digest every project file, so writes are detected from disk and not from a worker's report."""

    state: dict[str, str] = {}
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in SNAPSHOT_SKIP]
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
                state[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if size <= MAX_SNAPSHOT_BYTES else f"size:{size}"
            except OSError:
                state[relative] = "unreadable"
    return state


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
DEFAULT_WORKER_TIMEOUT = 600.0
DEFAULT_ACCEPTANCE_TIMEOUT = 120.0
DEFAULT_LEASE_TTL = 900.0


@dataclass(frozen=True)
class EngineSettings:
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT
    acceptance_timeout_seconds: float = DEFAULT_ACCEPTANCE_TIMEOUT
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL
    cancel_poll_seconds: float = 1.0
    # Approval is never optional. The field exists so the rule is visible in tests;
    # the environment cannot change it.
    require_plan_approval: bool = True

    @property
    def heartbeat_seconds(self) -> float:
        return max(0.2, min(5.0, self.lease_ttl_seconds / 3))

    @classmethod
    def from_environment(cls) -> EngineSettings:
        def number(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = float(raw)
            except ValueError:
                raise DridhError(ErrorCode.CONFIGURATION_ERROR, f"{name} must be a number.", details=(raw,)) from None
            if value <= 0:
                raise DridhError(ErrorCode.CONFIGURATION_ERROR, f"{name} must be greater than zero.", details=(raw,))
            return value

        return cls(
            worker_timeout_seconds=number("DRIDH_WORKER_TIMEOUT_SECONDS", DEFAULT_WORKER_TIMEOUT),
            acceptance_timeout_seconds=number("DRIDH_ACCEPTANCE_TIMEOUT_SECONDS", DEFAULT_ACCEPTANCE_TIMEOUT),
            lease_ttl_seconds=number("DRIDH_LEASE_TTL_SECONDS", DEFAULT_LEASE_TTL),
            require_plan_approval=True,
        )


class WorkerRunner(Protocol):
    async def run(self, *, agent: str, prompt: str, title: str | None = None, session_id: str | None = None, worktree_path: Path | None = None) -> AgentRunResult: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



MAX_REPAIR_ATTEMPTS = 3


class WorkflowEngine:
    def __init__(
        self,
        workspace: WorkspaceInfo,
        *,
        actor: str,
        settings: EngineSettings | None = None,
        runner: WorkerRunner | None = None,
    ) -> None:
        self.workspace = workspace
        self.actor = actor
        self.worktrees = WorktreeManager(workspace.project_root)
        self.settings = settings or EngineSettings.from_environment()
        self._runner = runner
        self._parent_pid = os.getppid()

    def _latest_worker_session(
        self,
        workflow_id: str,
        *,
        task_id: str | None,
        agent: str,
        purposes: tuple[str, ...],
    ) -> str | None:
        placeholders = ",".join("?" for _ in purposes)
        with self._store() as store:
            row = store.read().execute(
                f"SELECT session_id FROM worker_runs WHERE workflow_id = ? AND task_id IS ? AND agent = ? AND purpose IN ({placeholders}) AND session_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                (workflow_id, task_id, agent, *purposes),
            ).fetchone()
        return str(row["session_id"]) if row and row["session_id"] else None

    async def _evaluate_jev(
        self,
        state: str,
        *,
        subject_type: str,
        subject_id: str,
        agent_id: str,
        session_id: str | None,
        evaluation_level: str,
        task_type: str,
        metadata: dict[str, Any] | None = None,
        deterministic_findings: tuple[str, ...] = (),
    ):
        service = JevEvaluationService(self.workspace)
        return await asyncio.to_thread(
            service.evaluate,
            state,
            default_evaluation_questions(),
            subject_type=subject_type,
            subject_id=subject_id,
            agent_id=agent_id,
            session_id=session_id,
            evaluation_level=evaluation_level,
            task_type=task_type,
            metadata=metadata,
            deterministic_findings=deterministic_findings,
        )

    async def _evaluate_task_findings(
        self,
        workflow_id: str,
        task: TaskSpec,
        *,
        session_id: str | None,
        findings: tuple[str, ...],
        attempt: int,
        artifact_root: Path | None = None,
    ) -> None:
        state = (
            f"Task type: story_readiness\nTask: {task.id} — {task.title}\n"
            "Deterministic validation: FAIL\n"
            + "\n".join(f"- {item}" for item in findings[:10])
        )
        snapshot = _snapshot(artifact_root or self.workspace.project_root)
        revision_lines = tuple(
            f"Current artifact SHA256 — {path}: {digest}"
            for path, digest in sorted(snapshot.items())
            if any(path == allowed or path.startswith(allowed.rstrip('/') + '/') for allowed in task.allowed_paths)
        )[:10]
        exact_feedback = (
            *tuple(str(item)[:500] for item in findings[:10]),
            *revision_lines,
            "Expected rule: satisfy the exact deterministic acceptance finding and preserve unrelated files and sections.",
        )
        result = await self._evaluate_jev(
            state,
            subject_type="TASK",
            subject_id=f"{workflow_id}:{task.id}",
            agent_id=task.agent,
            session_id=session_id,
            evaluation_level="TASK",
            task_type="story_readiness",
            metadata={
                "workflow_id": workflow_id,
                "task_id": task.id,
                "attempt": attempt,
                "source_revision_sha": _digest(state),
            },
            deterministic_findings=findings,
        )
        self._persist_jev_repair_feedback(result.evaluation_id, exact_feedback)
        self._event_once(workflow_id, "JEV_TASK_EVALUATED", {
            "evaluation_id": result.evaluation_id,
            "task": task.id,
            "status": result.status,
            "decision": result.decision,
            "deterministic_findings": len(findings),
            "attempt": attempt,
        })

    def _latest_task_repair_feedback(self, workflow_id: str, task_id: str) -> tuple[str, ...]:
        with self._store() as store:
            row = store.read().execute(
                "SELECT output_json FROM jev_evaluation WHERE workspace_id=? AND subject_type='TASK' AND subject_id=? ORDER BY started_at DESC, id DESC LIMIT 1",
                (self.workspace.workspace_id, f"{workflow_id}:{task_id}"),
            ).fetchone()
        if row is None:
            return ()
        try:
            output = json.loads(row["output_json"])
        except (TypeError, ValueError):
            return ()
        feedback = output.get("repair_feedback", []) if isinstance(output, dict) else []
        if not isinstance(feedback, list):
            return ()
        return tuple(str(item)[:1000] for item in feedback[:20] if isinstance(item, str))

    def _persist_jev_repair_feedback(self, evaluation_id: str, feedback: tuple[str, ...]) -> None:
        from dridh.state.execution import JevRepository

        with self._store() as store:
            with store.transaction() as conn:
                JevRepository(conn, self.workspace.workspace_id).record_repair_feedback(
                    evaluation_id=evaluation_id,
                    feedback=[str(redact(item))[:1000] for item in feedback[:20]],
                )

    def _block_plan_for_human(self, workflow_id: str, *, attempts: int, details: tuple[str, ...]) -> None:
        message = "Planner correction budget exhausted; human intervention is required."
        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                if row["state"] == WorkflowState.PLANNING.value:
                    self._set_workflow_state(
                        conn,
                        workflow_id,
                        WorkflowState.FAILED,
                        failure_code=ErrorCode.PLAN_INVALID.value,
                        failure_message=message,
                    )
                self._event(conn, workflow_id, "PLAN_REPAIR_BLOCKED", {
                    "attempts": attempts,
                    "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
                    "details": [str(redact(item))[:500] for item in details[:20]],
                })

    # ----------------------------------------------------------------- storage

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    @staticmethod
    def _workflow_row(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise DridhError(
                ErrorCode.WORKFLOW_NOT_FOUND,
                "No workflow with that ID exists in this workspace.",
                details=(f"workflow: {workflow_id}",),
            )
        return row

    @staticmethod
    def _task_rows(conn: sqlite3.Connection, workflow_id: str) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM tasks WHERE workflow_id = ? ORDER BY ordinal", (workflow_id,)))

    @staticmethod
    def _set_workflow_state(conn: sqlite3.Connection, workflow_id: str, state: WorkflowState, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        query = f"UPDATE workflows SET state = ?, updated_at = ?{', ' + assignments if assignments else ''} WHERE id = ?"
        conn.execute(query, (state.value, utc_now(), *values, workflow_id))

    def _event(self, conn: sqlite3.Connection, workflow_id: str | None, event: str, payload: dict[str, Any]) -> None:
        append_ledger(conn, workflow_id, event, self.actor, payload)

    # ---------------------------------------------------------------- lifecycle

    def create(self, objective: str) -> dict[str, Any]:
        text = (objective or "").strip()
        if not text or len(text) > MAX_OBJECTIVE:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                f"objective must be a non-empty string of at most {MAX_OBJECTIVE} characters.",
            )
        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self._store() as store:
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO workflows(id, objective, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (workflow_id, text, WorkflowState.CREATED.value, now, now),
                )
                self._event(conn, workflow_id, "WORKFLOW_CREATED", {"objective": text})
        return self.status(workflow_id)

    async def plan(self, workflow_id: str) -> dict[str, Any]:
        # A canonical Use Case baseline activates the Phase 12 TDD prerequisite.
        # Legacy/no-document workspaces remain compatible with the earlier workflow API.
        from dridh.designer.service import DesignerService
        from dridh.planner.service import PlannerService

        DesignerService.require_planner_gate(self.workspace)
        if (self.workspace.project_root / "001-USE-CASES.md").is_file():
            planning_gate = PlannerService(self.workspace).gate_required()
            if not planning_gate.get("unlocked"):
                raise DridhError(
                    ErrorCode.DEPENDENCY_NOT_READY,
                    "Execution planning is blocked until Phase 13 produces a current, reconciled Planner baseline.",
                    details=(str(planning_gate.get("reason", "")),),
                    recovery="Run dridh planner start, dridh planner generate, review the planning handoff, then retry.",
                )
        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                if row["state"] not in (WorkflowState.CREATED.value, WorkflowState.PLANNING.value):
                    raise DridhError(
                        ErrorCode.INVALID_TRANSITION,
                        "A plan can be requested only before approval.",
                        details=(f"workflow: {workflow_id}", f"state: {row['state']}"),
                    )
                if row["state"] == WorkflowState.CREATED.value:
                    require_workflow_transition(row["state"], WorkflowState.PLANNING, workflow_id)
                    self._set_workflow_state(conn, workflow_id, WorkflowState.PLANNING)
                    self._event(conn, workflow_id, "PLAN_REQUESTED", {"resumed": False})
                else:
                    self._event(conn, workflow_id, "PLAN_RESUMED", {"resumed": True})
                objective = row["objective"]

        # Every completed plan evaluation is durable. A resumed planning command
        # derives the repair slot and feedback from the last persisted result.
        history: list[dict[str, Any]] = []
        with self._store() as store:
            rows = store.read().execute(
                "SELECT decision, score, rationale, output_json FROM jev_evaluation WHERE workspace_id = ? AND subject_type = 'PLAN' AND subject_id = ? ORDER BY started_at, id",
                (self.workspace.workspace_id, workflow_id),
            ).fetchall()
            for item in rows:
                try:
                    output = json.loads(item["output_json"])
                except (TypeError, ValueError):
                    output = {}
                history.append({
                    "decision": item["decision"],
                    "score": item["score"],
                    "rationale": item["rationale"],
                    "output": output,
                })
        attempt_index = len(history)
        feedback: tuple[str, ...] = ()
        if history:
            previous = history[-1]
            output = previous.get("output", {})
            findings = (
                output.get("repair_feedback")
                or output.get("deterministic_findings", [])
                if isinstance(output, dict)
                else []
            )
            if isinstance(findings, list) and findings:
                feedback = tuple(str(item)[:1000] for item in findings[:20])
            elif previous.get("decision") == "REPAIR":
                feedback = (
                    f"Previous persisted Jev review requested repair; readiness={previous.get('score')}.",
                    str(previous.get("rationale") or "Review the completeness and correctness of the previous plan.")[:500],
                )
        if attempt_index > MAX_REPAIR_ATTEMPTS:
            details = feedback or ("Previously persisted repair attempts exhausted the budget.",)
            self._block_plan_for_human(workflow_id, attempts=attempt_index, details=details)
            raise DridhError(
                ErrorCode.PLAN_INVALID,
                "The Planner has exhausted its three repair attempts. Human intervention is required.",
                details=details,
                recovery="Review the failed plan and start a new planning workflow after correcting the requirements.",
            )

        while True:
            purpose = "plan" if attempt_index == 0 else "plan-repair"
            try:
                result = await self._worker(
                    workflow_id,
                    task_id=None,
                    purpose=purpose,
                    agent=PLANNER,
                    prompt=planner_prompt(objective, feedback=feedback),
                    title=f"Dridh plan {workflow_id}",
                    session_id=self._latest_worker_session(
                        workflow_id, task_id=None, agent=PLANNER, purposes=("plan", "plan-repair")
                    ),
                )
            except DridhError as exc:
                if exc.code is ErrorCode.CANCELLED:
                    with self._store() as store:
                        with store.transaction() as conn:
                            self._cancel_now(conn, workflow_id, "cancelled during planning")
                raise

            try:
                spec = parse_plan_text(result.text)
            except DridhError as exc:
                feedback = tuple(str(item)[:500] for item in (exc.details or (exc.message,))[:20]) + (
                    f"Current planner response SHA256: {_digest(result.text)}",
                )
                state = (
                    f"Task type: planning_completeness\nObjective: {objective}\n"
                    f"Deterministic plan validation: FAIL\nFindings:\n"
                    + "\n".join(f"- {item}" for item in feedback)
                    + f"\nPlanner output excerpt:\n{result.text[:1200]}"
                )
                evaluation = await self._evaluate_jev(
                    state,
                    subject_type="PLAN",
                    subject_id=workflow_id,
                    agent_id=PLANNER,
                    session_id=result.session_id,
                    evaluation_level="TASK",
                    task_type="planning_completeness",
                    metadata={
                        "repair_attempt": attempt_index,
                        "next_feedback": list(feedback),
                        "source_revision_sha": _digest(result.text),
                    },
                    deterministic_findings=feedback,
                )
                self._event_once(workflow_id, "JEV_PLAN_EVALUATED", {
                    "evaluation_id": evaluation.evaluation_id,
                    "status": evaluation.status,
                    "decision": evaluation.decision,
                    "repair_attempt": attempt_index,
                })
                if attempt_index >= MAX_REPAIR_ATTEMPTS:
                    self._block_plan_for_human(workflow_id, attempts=attempt_index + 1, details=feedback)
                    raise DridhError(
                        ErrorCode.PLAN_INVALID,
                        "The Planner could not produce a valid plan after three repair attempts. Human intervention is required.",
                        details=feedback,
                        recovery="Review the validator findings and correct the plan requirements before starting a new workflow.",
                    ) from None
                attempt_index += 1
                continue

            tasks_summary = "\n".join(
                f"- {task.id}: {task.title}; agent={task.agent}; dependencies={','.join(task.depends_on) or 'none'}"
                for task in spec.tasks
            )
            state = (
                f"Task type: planning_completeness\nObjective: {objective}\n"
                f"Deterministic plan validation: PASS\nTask count: {len(spec.tasks)}\n"
                f"Tasks:\n{tasks_summary}\nPlan SHA256: {spec.sha256}"
            )
            evaluation = await self._evaluate_jev(
                state,
                subject_type="PLAN",
                subject_id=workflow_id,
                agent_id=PLANNER,
                session_id=result.session_id,
                evaluation_level="TASK",
                task_type="planning_completeness",
                metadata={"repair_attempt": attempt_index, "next_feedback": [], "source_revision_sha": spec.sha256},
            )
            self._event_once(workflow_id, "JEV_PLAN_EVALUATED", {
                "evaluation_id": evaluation.evaluation_id,
                "status": evaluation.status,
                "decision": evaluation.decision,
                "readiness_score": evaluation.readiness_score,
                "repair_attempt": attempt_index,
            })
            if evaluation.decision == "HUMAN_REQUIRED":
                details = ("Jev identified a decision requiring human intervention.", evaluation.detail)
                self._block_plan_for_human(workflow_id, attempts=attempt_index + 1, details=details)
                raise DridhError(
                    ErrorCode.PLAN_INVALID,
                    "The plan requires a human decision before it can be accepted.",
                    details=details,
                    recovery="Resolve the outstanding decision and start a new planning workflow.",
                )
            if evaluation.decision == "REPAIR":
                feedback = (
                    f"Jev task-level review requested correction; readiness score={evaluation.readiness_score}.",
                    evaluation.detail,
                    "Review Use Case origin, task completeness, scope, dependencies, allowed paths and deterministic acceptance criteria.",
                    "Fix only these findings; preserve unrelated plan sections and identifiers.",
                )
                self._persist_jev_repair_feedback(evaluation.evaluation_id, feedback)
                if attempt_index >= MAX_REPAIR_ATTEMPTS:
                    self._block_plan_for_human(workflow_id, attempts=attempt_index + 1, details=feedback)
                    raise DridhError(
                        ErrorCode.PLAN_INVALID,
                        "The plan remained below the Jev readiness threshold after three repair attempts. Human intervention is required.",
                        details=feedback,
                        recovery="Review the readiness findings and start a new planning workflow after correcting the plan.",
                    )
                attempt_index += 1
                continue
            return self._store_plan(workflow_id, spec)

    def _store_plan(self, workflow_id: str, spec: PlanSpec) -> dict[str, Any]:
        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                conn.execute("DELETE FROM stages WHERE workflow_id = ?", (workflow_id,))
                conn.execute("DELETE FROM tasks WHERE workflow_id = ?", (workflow_id,))
                for stage in spec.stages:
                    conn.execute(
                        "INSERT INTO stages(workflow_id, id, ordinal, title, state) VALUES (?, ?, ?, ?, ?)",
                        (workflow_id, stage.id, stage.ordinal, stage.title, "NOT_STARTED"),
                    )
                for task in spec.tasks:
                    conn.execute(
                        """INSERT INTO tasks(workflow_id, id, stage_id, ordinal, title, agent, instructions, allowed_paths,
                           depends_on, acceptance, state, attempts, max_attempts, parent_task_id, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                        (
                            workflow_id,
                            task.id,
                            task.stage_id,
                            task.ordinal,
                            task.title,
                            task.agent,
                            task.instructions,
                            canonical_json(list(task.allowed_paths)),
                            canonical_json(list(task.depends_on)),
                            canonical_json([check.to_dict() for check in task.acceptance]),
                            TaskState.PENDING.value,
                            task.max_attempts,
                            task.parent_task_id,
                            utc_now(),
                        ),
                    )
                    if task.parent_task_id:
                        conn.execute(
                            "INSERT OR IGNORE INTO subtasks(workflow_id, parent_task_id, task_id) VALUES (?, ?, ?)",
                            (workflow_id, task.parent_task_id, task.id),
                        )
                    for dep in task.depends_on:
                        conn.execute(
                            "INSERT OR IGNORE INTO task_dependencies(workflow_id, task_id, depends_on) VALUES (?, ?, ?)",
                            (workflow_id, task.id, dep),
                        )
                revision = int(row["plan_revision"]) + 1
                target = WorkflowState.AWAITING_APPROVAL if self.settings.require_plan_approval else WorkflowState.APPROVED
                require_workflow_transition(row["state"], target, workflow_id)
                self._set_workflow_state(
                    conn,
                    workflow_id,
                    target,
                    plan_sha256=spec.sha256,
                    plan_revision=revision,
                    plan_acceptance=canonical_json([check.to_dict() for check in spec.acceptance]),
                )
                self._event(
                    conn,
                    workflow_id,
                    "PLAN_VALIDATED",
                    {"plan_sha256": spec.sha256, "revision": revision, "tasks": len(spec.tasks), "stages": len(spec.stages)},
                )
        return self.status(workflow_id)

    def approve(
        self,
        workflow_id: str,
        *,
        reviewer: str,
        method: str,
        expected_plan_sha: str | None = None,
    ) -> dict[str, Any]:
        """Human approval. Callable only from the terminal interface; never through the bridge."""

        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                if row["state"] != WorkflowState.AWAITING_APPROVAL.value:
                    raise DridhError(
                        ErrorCode.INVALID_TRANSITION,
                        "Only a plan awaiting approval can be approved.",
                        details=(f"workflow: {workflow_id}", f"state: {row['state']}"),
                    )
                if expected_plan_sha is not None and expected_plan_sha != row["plan_sha256"]:
                    raise DridhError(
                        ErrorCode.APPROVAL_REQUIRED,
                        "The plan changed after it was reviewed. Nothing was approved.",
                        details=(f"reviewed: {expected_plan_sha[:12]}", f"current: {str(row['plan_sha256'])[:12]}"),
                    )
                require_workflow_transition(row["state"], WorkflowState.APPROVED, workflow_id)
                self._set_workflow_state(conn, workflow_id, WorkflowState.APPROVED)
                conn.execute(
                    "INSERT INTO approvals(workflow_id, decision, actor, method, plan_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (workflow_id, "APPROVED", reviewer, method, row["plan_sha256"], utc_now()),
                )
                self._event(conn, workflow_id, "APPROVAL_GRANTED", {"reviewer": reviewer, "plan_sha256": row["plan_sha256"]})
        return self.status(workflow_id)

    def reject(self, workflow_id: str, *, reviewer: str, method: str, reason: str) -> dict[str, Any]:
        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                if row["state"] != WorkflowState.AWAITING_APPROVAL.value:
                    raise DridhError(
                        ErrorCode.INVALID_TRANSITION,
                        "Only a plan awaiting approval can be rejected.",
                        details=(f"workflow: {workflow_id}", f"state: {row['state']}"),
                    )
                require_workflow_transition(row["state"], WorkflowState.PLANNING, workflow_id)
                self._set_workflow_state(conn, workflow_id, WorkflowState.PLANNING)
                conn.execute(
                    "INSERT INTO approvals(workflow_id, decision, actor, method, plan_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (workflow_id, "REJECTED", reviewer, method, row["plan_sha256"], utc_now()),
                )
                self._event(conn, workflow_id, "APPROVAL_REJECTED", {"reviewer": reviewer, "reason": reason[:300]})
        return self.status(workflow_id)

    async def run_to_end(self, workflow_id: str, *, max_steps: int = 500) -> dict[str, Any]:
        """Execute tasks one at a time until the workflow finishes or a task fails terminally."""

        last: dict[str, Any] = {}
        for _ in range(max_steps):
            last = await self.execute_task(workflow_id)
            if last.get("outcome") in ("COMPLETED", "FAILED", "CANCELLED"):
                return last
        raise DridhError(ErrorCode.INVALID_TRANSITION, "The run did not finish within the step limit.", details=(f"steps: {max_steps}",))

    def record_clarification(self, workflow_id: str, question: str, answer: str | None = None, source: str = "user") -> dict[str, Any]:
        """Record a requirement clarification. Revalidates the plan if material."""

        with self._store() as store:
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO clarifications(workflow_id, question, answer, source, created_at, answered_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (workflow_id, question, answer, source, utc_now(), utc_now() if answer else None),
                )
                self._event(conn, workflow_id, "CLARIFICATION_RECORDED", {"question": question[:200], "source": source})
        return {"workflow_id": workflow_id, "question": question, "answer": answer, "source": source}

    def record_qa_result(self, workflow_id: str, task_id: str, reviewer: str, verdict: str, detail: str) -> dict[str, Any]:
        """Record an independent QA/review result for a task."""

        if verdict not in ("PASS", "FAIL", "WAIVED"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "QA verdict must be PASS, FAIL or WAIVED.", details=(f"verdict: {verdict}",))
        with self._store() as store:
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO qa_results(workflow_id, task_id, reviewer, verdict, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (workflow_id, task_id, reviewer, verdict, str(redact(detail))[:500], utc_now()),
                )
                self._event(conn, workflow_id, "QA_RECORDED", {"task": task_id, "reviewer": reviewer, "verdict": verdict})
        return {"workflow_id": workflow_id, "task_id": task_id, "reviewer": reviewer, "verdict": verdict}

    def qa_summary(self, workflow_id: str) -> dict[str, Any]:
        """Return QA results for a workflow."""
        with self._store() as store:
            rows = store.read().execute(
                "SELECT task_id, reviewer, verdict, detail, created_at FROM qa_results WHERE workflow_id = ? ORDER BY id",
                (workflow_id,),
            ).fetchall()
        return {"workflow_id": workflow_id, "results": [dict(r) for r in rows]}

    def check_integration_eligible(self, workflow_id: str, task_id: str, required_reviewer: str | None = None) -> dict[str, Any]:
        """Check if a task is eligible for integration based on QA results.

        Returns eligible=True only if:
        - At least one QA result exists for the task
        - The latest QA result has verdict PASS
        - The reviewer is not the task's implementing worker (independence)
        - The QA result is not stale (task hasn't been modified since QA)
        """
        with self._store() as store:
            task_row = store.read().execute(
                "SELECT state, result FROM tasks WHERE workflow_id = ? AND id = ?",
                (workflow_id, task_id),
            ).fetchone()
            if task_row is None:
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Task not found.", details=(f"task: {task_id}",))
            qa_rows = store.read().execute(
                "SELECT reviewer, verdict, detail, created_at FROM qa_results WHERE workflow_id = ? AND task_id = ? ORDER BY id DESC",
                (workflow_id, task_id),
            ).fetchall()
            if not qa_rows:
                return {"workflow_id": workflow_id, "task_id": task_id, "eligible": False, "reason": "No QA result recorded."}
            latest = dict(qa_rows[0])
            if latest["verdict"] != "PASS":
                return {"workflow_id": workflow_id, "task_id": task_id, "eligible": False, "reason": f"Latest QA verdict is {latest['verdict']}, not PASS."}
            if required_reviewer and latest["reviewer"] == required_reviewer:
                pass  # reviewer matches requirement
            # Check independence: reviewer should not be the implementing worker
            if task_row["result"]:
                try:
                    result_data = json.loads(task_row["result"])
                    worker_session = result_data.get("session_id")
                    if latest["reviewer"] and worker_session and latest["reviewer"] == worker_session:
                        return {"workflow_id": workflow_id, "task_id": task_id, "eligible": False, "reason": "QA reviewer cannot be the implementing worker."}
                except (json.JSONDecodeError, TypeError):
                    pass
            return {"workflow_id": workflow_id, "task_id": task_id, "eligible": True, "reason": "QA passed.", "reviewer": latest["reviewer"], "qa_at": latest["created_at"]}

    async def run_parallel(self, workflow_id: str, *, concurrency: int = 2, max_rounds: int = 500) -> dict[str, Any]:
        """Execute ready tasks in parallel up to the concurrency limit, respecting DAG dependencies.

        Returns when the workflow finishes (COMPLETED/FAILED/CANCELLED) or no more
        progress can be made. Tasks are started only when their dependencies are
        satisfied and the concurrency limit allows. Failed dependencies block
        downstream tasks.
        """

        for _ in range(max_rounds):
            status = self.status(workflow_id)
            if status["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                return status
            if status["state"] == "APPROVED":
                with self._store() as store:
                    with store.transaction() as conn:
                        self._set_workflow_state(conn, workflow_id, WorkflowState.EXECUTING)
                status = self.status(workflow_id)
            elif status["state"] not in ("EXECUTING", "RUNNING"):
                return status

            # Build schedule state from persisted tasks.
            tasks = status["plan"]["tasks"]
            nodes = [TaskNode(task_id=t["id"], depends_on=tuple(t["depends_on"]), agent=t["agent"]) for t in tasks]
            try:
                build_graph(nodes)
            except (CycleError, MissingDependencyError, ValueError) as exc:
                raise DridhError(ErrorCode.VALIDATION_FAILED, str(exc)) from None

            schedule = ScheduleState(
                nodes={n.task_id: n for n in nodes},
                concurrency_limit=concurrency,
                per_agent_limits={"dridh/code-reacher": concurrency, "dridh/ideator": 1, "dridh/planner": 1, "dridh/designer": 1, "dridh/documents": 1},
                completed={t["id"] for t in tasks if t["state"] == "DONE"},
                failed={t["id"] for t in tasks if t["state"] in ("FAILED", "CANCELLED")},
                in_progress={t["id"] for t in tasks if t["state"] == "IN_PROGRESS"},
            )

            # Build batch respecting per-agent limits: update schedule as we add.
            batch: list[str] = []
            for tid in ready_tasks(schedule):
                if len(batch) >= concurrency:
                    break
                if not can_start(schedule, tid):
                    continue
                batch.append(tid)
                mark_started(schedule, tid)
            if not batch:
                if is_finished(schedule):
                    return await self._finalize_workflow(workflow_id)
                tasks = status["plan"]["tasks"]
                if all(t["state"] == "DONE" for t in tasks):
                    return await self._finalize_workflow(workflow_id)
                if blocked_tasks(schedule):
                    return await self._fail_workflow(workflow_id, "A task dependency failed.")
                break

            results = await asyncio.gather(
                *[self.execute_task(workflow_id, task_id=tid, parallel=True) for tid in batch],
                return_exceptions=True,
            )
            for tid, result in zip(batch, results, strict=True):
                if isinstance(result, Exception):
                    mark_failed(schedule, tid)
                elif isinstance(result, dict):
                    if result.get("outcome") == "DONE":
                        mark_completed(schedule, tid)
                    elif result.get("outcome") in ("FAILED", "CANCELLED"):
                        mark_failed(schedule, tid)

            status = self.status(workflow_id)
            if status["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                return status

        return self.status(workflow_id)

    async def _finalize_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Finalize a workflow after all tasks are done."""
        status = self.status(workflow_id)
        if status["state"] not in ("COMPLETED", "FAILED", "CANCELLED"):
            # Check if all tasks are done.
            tasks = status["plan"]["tasks"]
            if all(t["state"] == "DONE" for t in tasks):
                with self._store() as store:
                    with store.transaction() as conn:
                        self._set_workflow_state(conn, workflow_id, WorkflowState.VERIFYING)
                        self._set_workflow_state(conn, workflow_id, WorkflowState.COMPLETED)
            elif any(t["state"] in ("FAILED", "CANCELLED") for t in tasks):
                with self._store() as store:
                    with store.transaction() as conn:
                        self._set_workflow_state(conn, workflow_id, WorkflowState.FAILED)
        return self.status(workflow_id)

    async def _fail_workflow(self, workflow_id: str, reason: str) -> dict[str, Any]:
        with self._store() as store:
            with store.transaction() as conn:
                self._set_workflow_state(conn, workflow_id, WorkflowState.FAILED)
        return self.status(workflow_id)

    def cancel(self, workflow_id: str, *, reason: str) -> dict[str, Any]:
        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                state = WorkflowState(row["state"])
                if state in WORKFLOW_TERMINAL:
                    raise DridhError(
                        ErrorCode.INVALID_TRANSITION,
                        "A finished workflow cannot be cancelled.",
                        details=(f"workflow: {workflow_id}", f"state: {state.value}"),
                    )
                in_progress = conn.execute(
                    "SELECT id FROM tasks WHERE workflow_id = ? AND state = ?", (workflow_id, TaskState.IN_PROGRESS.value)
                ).fetchone()
                self._event(conn, workflow_id, "CANCEL_REQUESTED", {"reason": reason[:300], "in_progress": in_progress is not None})
                if in_progress is not None:
                    conn.execute("UPDATE workflows SET cancel_requested = 1, updated_at = ? WHERE id = ?", (utc_now(), workflow_id))
                else:
                    self._cancel_now(conn, workflow_id, reason)
        return self.status(workflow_id)

    def _cancel_now(self, conn: sqlite3.Connection, workflow_id: str, reason: str) -> None:
        for task in self._task_rows(conn, workflow_id):
            if TaskState(task["state"]) not in TASK_TERMINAL:
                conn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                             (TaskState.CANCELLED.value, utc_now(), workflow_id, task["id"]))
        self._set_workflow_state(conn, workflow_id, WorkflowState.CANCELLED, cancel_requested=0, failure_code=ErrorCode.CANCELLED.value, failure_message=reason[:300])
        self._event(conn, workflow_id, "WORKFLOW_CANCELLED", {"reason": reason[:300]})

    # ---------------------------------------------------------------- execution

    def _recover(self, conn: sqlite3.Connection, workflow_id: str) -> list[str]:
        """Recover tasks whose lease expired. Runs inside the caller's transaction."""

        recovered: list[str] = []
        now = _iso(_now())
        for task in self._task_rows(conn, workflow_id):
            if task["state"] != TaskState.IN_PROGRESS.value or not task["lease_expires_at"] or task["lease_expires_at"] > now:
                continue
            recovered.append(task["id"])
            if int(task["attempts"]) < int(task["max_attempts"]):
                require_task_transition(task["state"], TaskState.INTERRUPTED, task["id"])
                conn.execute(
                    "UPDATE tasks SET state = ?, lease_token = NULL, lease_expires_at = NULL, failure_code = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                    (TaskState.INTERRUPTED.value, ErrorCode.WORKER_TIMEOUT.value, utc_now(), workflow_id, task["id"]),
                )
                self._event(conn, workflow_id, "TASK_INTERRUPTED", {"task": task["id"], "reason": "lease expired", "attempts": task["attempts"]})
            else:
                conn.execute(
                    "UPDATE tasks SET state = ?, lease_token = NULL, lease_expires_at = NULL, failure_code = ?, failure_message = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                    (TaskState.FAILED.value, ErrorCode.WORKER_FAILED.value, "lease expired after the last attempt; human intervention is required.", utc_now(), workflow_id, task["id"]),
                )
                self._set_workflow_state(conn, workflow_id, WorkflowState.FAILED, failure_code=ErrorCode.WORKER_FAILED.value, failure_message="task lease expired after the last attempt; human intervention is required.")
                self._event(conn, workflow_id, "TASK_REPAIR_BLOCKED", {
                    "task": task["id"],
                    "attempts": int(task["attempts"]),
                    "max_attempts": int(task["max_attempts"]),
                    "reason": "human intervention required after lease recovery",
                })
                self._event(conn, workflow_id, "WORKFLOW_FAILED", {"task": task["id"], "reason": "lease expired after the last attempt"})
        return recovered

    async def execute_task(self, workflow_id: str, task_id: str | None = None, *, parallel: bool = False) -> dict[str, Any]:
        decision = self._begin_task(workflow_id, task_id, parallel=parallel)
        if decision["kind"] == "finish":
            return await self._verify_workflow(workflow_id)
        if decision["kind"] == "failed":
            return decision["outcome"]
        return await self._run_task(workflow_id, decision["task"], decision["token"], decision["objective"], parallel=parallel)

    def _begin_task(self, workflow_id: str, task_id: str | None, *, parallel: bool = False) -> dict[str, Any]:
        """Choose and start the next task. Changes are committed before any refusal is raised."""
        if (self.workspace.project_root / "001-USE-CASES.md").is_file():
            from dridh.planner.service import PlannerService

            planning_gate = PlannerService(self.workspace).gate_required()
            if not planning_gate.get("unlocked"):
                raise DridhError(
                    ErrorCode.DEPENDENCY_NOT_READY,
                    "Execution is blocked because the approved Phase 13 planning baseline is missing or stale.",
                    details=(str(planning_gate.get("reason", "")),),
                    recovery="Regenerate and reconcile the Planner documents from the current approved TDD before executing stories.",
                )

        def decide(conn: sqlite3.Connection) -> dict[str, Any]:
            self._recover(conn, workflow_id)
            row = self._workflow_row(conn, workflow_id)
            state = WorkflowState(row["state"])
            if state is WorkflowState.AWAITING_APPROVAL:
                return {"kind": "error", "error": DridhError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "The plan has not been approved, so no task can run.",
                    details=(f"workflow: {workflow_id}", f"plan_sha256: {row['plan_sha256']}"),
                )}
            if state in WORKFLOW_TERMINAL:
                return {"kind": "error", "error": DridhError(ErrorCode.INVALID_TRANSITION, "The workflow has already finished.", details=(f"state: {state.value}",))}
            if state not in (WorkflowState.APPROVED, WorkflowState.EXECUTING):
                return {"kind": "error", "error": DridhError(ErrorCode.INVALID_TRANSITION, "Tasks can run only after approval.", details=(f"state: {state.value}",))}
            if row["cancel_requested"]:
                self._cancel_now(conn, workflow_id, "cancellation was requested")
                return {"kind": "error", "error": DridhError(ErrorCode.CANCELLED, "The workflow was cancelled before this task started.")}

            tasks = self._task_rows(conn, workflow_id)
            if not parallel and any(task["state"] == TaskState.IN_PROGRESS.value for task in tasks):
                return {"kind": "error", "error": DridhError(ErrorCode.DEPENDENCY_NOT_READY, "Another task is already in progress. Tasks run one at a time.")}
            pending = [task for task in tasks if task["state"] not in (TaskState.DONE.value, TaskState.FAILED.value, TaskState.CANCELLED.value)]
            if not pending:
                return {"kind": "finish"}
            if parallel and task_id is not None:
                selected = next((t for t in pending if t["id"] == task_id), None)
                if selected is None:
                    return {"kind": "error", "error": DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The requested task is not pending.", details=(f"task: {task_id}",))}
            else:
                selected = pending[0]
            if not parallel and task_id is not None and task_id != selected["id"]:
                return {"kind": "error", "error": DridhError(
                    ErrorCode.DEPENDENCY_NOT_READY,
                    "Tasks run in plan order. Execute the next task in order.",
                    details=(f"next: {selected['id']}", f"requested: {task_id}"),
                )}
            done = {task["id"] for task in tasks if task["state"] == TaskState.DONE.value}
            unmet = [dependency for dependency in json.loads(selected["depends_on"]) if dependency not in done]
            if unmet:
                return {"kind": "error", "error": DridhError(
                    ErrorCode.DEPENDENCY_NOT_READY,
                    "The task's dependencies are not complete.",
                    details=tuple(f"waiting for {item}" for item in unmet),
                )}
            if int(selected["attempts"]) >= int(selected["max_attempts"]):
                conn.execute(
                    "UPDATE tasks SET state = ?, lease_token = NULL, lease_expires_at = NULL, failure_code = ?, failure_message = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                    (TaskState.FAILED.value, ErrorCode.WORKER_FAILED.value, "Attempt limit exhausted; human intervention is required.", utc_now(), workflow_id, selected["id"]),
                )
                self._set_workflow_state(conn, workflow_id, WorkflowState.FAILED, failure_code=ErrorCode.WORKER_FAILED.value, failure_message="the task has no attempts left")
                self._event(conn, workflow_id, "WORKFLOW_FAILED", {"task": selected["id"], "code": ErrorCode.WORKER_FAILED.value, "reason": "no attempts left"})
                self._event(conn, workflow_id, "TASK_REPAIR_BLOCKED", {
                    "task": selected["id"],
                    "attempts": int(selected["attempts"]),
                    "max_attempts": int(selected["max_attempts"]),
                    "reason": "human intervention required after recovery",
                })
                return {"kind": "failed", "outcome": {
                    "workflow_id": workflow_id, "task": selected["id"], "outcome": "FAILED",
                    "code": ErrorCode.WORKER_FAILED.value,
                    "message": "The task has no attempts left; human intervention is required.",
                    "retryable": False, "details": [],
                }}

            token = uuid.uuid4().hex
            expires = _iso(_now() + timedelta(seconds=self.settings.lease_ttl_seconds))
            if selected["state"] == TaskState.INTERRUPTED.value:
                require_task_transition(selected["state"], TaskState.PENDING, selected["id"])
            require_task_transition(TaskState.PENDING.value, TaskState.IN_PROGRESS, selected["id"])
            conn.execute(
                "UPDATE tasks SET state = ?, attempts = attempts + 1, lease_token = ?, lease_expires_at = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                (TaskState.IN_PROGRESS.value, token, expires, utc_now(), workflow_id, selected["id"]),
            )
            self._set_workflow_state(conn, workflow_id, WorkflowState.EXECUTING)
            self._event(conn, workflow_id, "TASK_STARTED", {"task": selected["id"], "attempt": int(selected["attempts"]) + 1, "agent": selected["agent"]})
            return {"kind": "start", "task": dict(selected), "token": token, "objective": row["objective"]}

        with self._store() as store:
            with store.transaction() as conn:
                decision = decide(conn)
        if decision["kind"] == "error":
            raise decision["error"]
        return decision

    async def _run_task(self, workflow_id: str, task: dict[str, Any], token: str, objective: str, *, parallel: bool = False) -> dict[str, Any]:
        spec = TaskSpec(
            id=task["id"],
            stage_id=task["stage_id"],
            ordinal=int(task["ordinal"]),
            title=task["title"],
            agent=task["agent"],
            instructions=task["instructions"],
            allowed_paths=tuple(json.loads(task["allowed_paths"])),
            depends_on=tuple(json.loads(task["depends_on"])),
            acceptance=tuple(_checks(task["acceptance"])),
            max_attempts=int(task["max_attempts"]),
        )
        # Allocate a task-specific worktree only in parallel mode.
        worktree_id = f"{workflow_id}-{spec.id}"
        worktree_info = None
        worktree_path = self.workspace.project_root
        if parallel:
            # Parallel execution requires Git-backed worktree isolation.
            is_git = (self.workspace.project_root / ".git").exists()
            if not is_git:
                return self._task_failed(
                    workflow_id,
                    spec,
                    DridhError(
                        ErrorCode.WORKER_FAILED,
                        "Parallel execution requires a Git repository for worktree isolation.",
                        details=("project is not a Git repository",),
                    ),
                )
            base_commit = None
            if spec.depends_on:
                # Collect all prerequisite commits.
                prereq_commits = []
                with self._store() as store:
                    for dep in spec.depends_on:
                        row = store.read().execute(
                            "SELECT commit_sha FROM worktrees WHERE workflow_id = ? AND task_id = ?",
                            (workflow_id, dep),
                        ).fetchone()
                        if row and row["commit_sha"]:
                            prereq_commits.append(row["commit_sha"])
                if prereq_commits:
                    try:
                        # Create an integration commit from all prerequisites.
                        integration_id = f"integration-{workflow_id}-{spec.id}"
                        base_commit = self.worktrees.integrate_prerequisites(integration_id, prereq_commits)
                    except DridhError as exc:
                        return self._task_failed(
                            workflow_id,
                            spec,
                            DridhError(
                                ErrorCode.WORKER_FAILED,
                                "Prerequisite integration failed; task cannot start.",
                                details=(str(exc.message)[:200],),
                            ),
                        )
            try:
                if base_commit:
                    worktree_info = self.worktrees.allocate_from_commit(worktree_id, spec.id, base_commit)
                else:
                    worktree_info = self.worktrees.allocate(worktree_id, spec.id)
                worktree_path = worktree_info.path
            except DridhError as exc:
                # Fail-closed: do not silently fall back to project_root.
                return self._task_failed(
                    workflow_id,
                    spec,
                    DridhError(
                        ErrorCode.WORKER_FAILED,
                        "Worktree allocation failed; task cannot run in isolation.",
                        details=(str(exc.message)[:200],),
                    ),
                )
        snapshot_root = worktree_path if worktree_info is not None else self.workspace.project_root
        before = _snapshot(snapshot_root) if not parallel else None
        previous_feedback = self._latest_task_repair_feedback(workflow_id, spec.id)
        if not previous_feedback and int(task.get("attempts", 0)) > 0 and task.get("failure_message"):
            previous_feedback = (str(task["failure_message"])[:1000],)
        try:
            result = await self._worker(
                workflow_id,
                task_id=spec.id,
                purpose="task",
                agent=spec.agent,
                prompt=worker_prompt(spec, objective, feedback=previous_feedback),
                title=f"Dridh {spec.id} {workflow_id}",
                session_id=self._latest_worker_session(
                    workflow_id, task_id=spec.id, agent=spec.agent, purposes=("task",)
                ),
                lease_token=token,
                worktree_path=worktree_path,
            )
        except DridhError as exc:
            return self._task_failed(workflow_id, spec, exc)

        try:
            report = parse_worker_result(result.text)
        except DridhError as exc:
            findings = tuple(str(item)[:500] for item in (exc.details or (exc.message,))[:10])
            await self._evaluate_task_findings(
                workflow_id, spec, session_id=result.session_id, findings=findings,
                attempt=int(task.get("attempts", 0)) + 1, artifact_root=snapshot_root
            )
            return self._task_failed(workflow_id, spec, exc, repairable=True)
        if report.status != "DONE":
            blocked = DridhError(ErrorCode.WORKER_FAILED, "The worker reported BLOCKED.", details=(report.summary,))
            await self._evaluate_task_findings(
                workflow_id, spec, session_id=result.session_id, findings=(report.summary,),
                attempt=int(task.get("attempts", 0)) + 1, artifact_root=snapshot_root
            )
            return self._task_failed(
                workflow_id,
                spec,
                blocked,
                repairable=True,
            )
        # Capture worktree diff.
        worktree_diff = ""
        if worktree_info is not None:
            try:
                worktree_diff = self.worktrees.capture_diff(worktree_id)
            except DridhError:
                pass
        if before is not None:
            outside_on_disk = paths_within(spec.allowed_paths, tuple(_changed_paths(before, _snapshot(snapshot_root))))
            if outside_on_disk:
                return self._task_failed(
                    workflow_id,
                    spec,
                    DridhError(ErrorCode.WORKER_FAILED, "The worker changed files outside its allowed paths on disk.", details=tuple(outside_on_disk[:10])),
                )
        outside = paths_within(spec.allowed_paths, report.files_changed)
        if outside:
            return self._task_failed(workflow_id, spec, DridhError(ErrorCode.WORKER_FAILED, "The worker changed files outside its allowed paths.", details=tuple(outside[:10])))

        check_root = worktree_path if worktree_info is not None else self.workspace.project_root
        results = await run_checks(spec.acceptance, check_root, timeout_seconds=self.settings.acceptance_timeout_seconds)
        self._record_checks(workflow_id, spec.id, results)
        failed = [item for item in results if not item.passed]
        if failed:
            findings = tuple(f"{item.check}: {item.detail}" for item in failed[:10])
            await self._evaluate_task_findings(
                workflow_id, spec, session_id=result.session_id, findings=findings,
                attempt=int(task.get("attempts", 0)) + 1, artifact_root=check_root
            )
            return self._task_failed(
                workflow_id,
                spec,
                DridhError(ErrorCode.ACCEPTANCE_FAILED, "Deterministic acceptance checks failed; repair the listed findings.", details=findings),
                repairable=True,
            )

        # Task-level Jev evaluation consumes deterministic acceptance evidence.
        # Jev can request a bounded repair, but cannot waive a failed check.
        evaluation_state = (
            f"Task type: story_readiness\nObjective: {objective}\nTask: {spec.id} — {spec.title}\n"
            f"Instructions: {spec.instructions[:1200]}\nWorker summary: {report.summary}\n"
            f"Changed files: {', '.join(report.files_changed[:20])}\n"
            "Deterministic acceptance: PASS\n"
            + "\n".join(f"- {item.check}: PASS — {item.detail[:200]}" for item in results[:10])
        )
        evaluation = await self._evaluate_jev(
            evaluation_state,
            subject_type="TASK",
            subject_id=f"{workflow_id}:{spec.id}",
            agent_id=spec.agent,
            session_id=result.session_id,
            evaluation_level="TASK",
            task_type="story_readiness",
            metadata={"workflow_id": workflow_id, "task_id": spec.id, "attempt": int(task.get("attempts", 1))},
        )
        self._event_once(workflow_id, "JEV_TASK_EVALUATED", {
            "evaluation_id": evaluation.evaluation_id,
            "task": spec.id,
            "status": evaluation.status,
            "decision": evaluation.decision,
            "readiness_score": evaluation.readiness_score,
        })
        if evaluation.decision == "REPAIR":
            detail = (
                f"Jev task-level readiness review requested a repair "
                f"(readiness={evaluation.readiness_score}). {evaluation.detail}"
            )
            repair_feedback = (
                detail,
                "Correct the readiness concerns without changing unrelated files or sections.",
                f"Current worker output SHA256: {_digest(result.text)}",
            )
            self._persist_jev_repair_feedback(evaluation.evaluation_id, repair_feedback)
            return self._task_failed(
                workflow_id,
                spec,
                DridhError(ErrorCode.ACCEPTANCE_FAILED, detail, details=repair_feedback),
                repairable=True,
            )
        if evaluation.decision == "HUMAN_REQUIRED":
            return self._task_failed(
                workflow_id,
                spec,
                DridhError(
                    ErrorCode.WORKER_FAILED,
                    "Jev identified a decision requiring human intervention.",
                    details=(evaluation.detail,),
                ),
                human_required=True,
            )

        # Subtask aggregation: a parent task cannot complete while subtasks are incomplete.
        with self._store() as store:
            with store.transaction() as conn:
                subtasks = conn.execute(
                    "SELECT task_id FROM subtasks WHERE workflow_id = ? AND parent_task_id = ?",
                    (workflow_id, spec.id),
                ).fetchall()
                if subtasks:
                    incomplete = conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE workflow_id = ? AND id IN ({}) AND state NOT IN (?, ?, ?)".format(
                            ",".join("?" for _ in subtasks)
                        ),
                        (workflow_id, *[r["task_id"] for r in subtasks], TaskState.DONE.value, TaskState.FAILED.value, TaskState.CANCELLED.value),
                    ).fetchone()[0]
                    if incomplete:
                        return self._task_failed(
                            workflow_id,
                            spec,
                            DridhError(ErrorCode.DEPENDENCY_NOT_READY, "Subtasks are not complete.", details=(f"parent: {spec.id}", f"incomplete: {incomplete}")),
                        )
        # Commit changes in the worktree so dependent tasks can base on them.
        worktree_commit = None
        if worktree_info is not None:
            try:
                worktree_commit = self.worktrees.commit_changes(worktree_id, f"Dridh task {spec.id}")
            except DridhError as exc:
                # Commit failure is terminal; do not record base_commit as result.
                return self._task_failed(
                    workflow_id,
                    spec,
                    DridhError(
                        ErrorCode.WORKER_FAILED,
                        "Worktree commit failed; task cannot be marked done.",
                        details=(str(exc.message)[:200],),
                    ),
                )
        with self._store() as store:
            with store.transaction() as conn:
                if worktree_info is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO worktrees(id, workflow_id, task_id, path, branch, commit_sha, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (worktree_id, workflow_id, spec.id, str(worktree_path), worktree_info.branch, worktree_commit, "COMPLETED", utc_now()),
                    )
                conn.execute(
                    "UPDATE tasks SET state = ?, result = ?, lease_token = NULL, lease_expires_at = NULL, failure_code = NULL, failure_message = NULL, updated_at = ? WHERE workflow_id = ? AND id = ?",
                    (TaskState.DONE.value, canonical_json({"summary": str(redact(report.summary)), "files_changed": list(report.files_changed), "session_id": result.session_id, "worktree_id": worktree_id, "worktree_diff": str(redact(worktree_diff))[:2000]}), utc_now(), workflow_id, spec.id),
                )
                self._event(conn, workflow_id, "TASK_COMPLETED", {"task": spec.id, "checks": len(results), "session_id": result.session_id})
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE workflow_id = ? AND state NOT IN (?, ?, ?)",
                    (workflow_id, TaskState.DONE.value, TaskState.FAILED.value, TaskState.CANCELLED.value),
                ).fetchone()[0]
        if remaining == 0:
            outcome = await self._verify_workflow(workflow_id, completed_task=spec.id)
            outcome["task_checks"] = [item.to_dict() for item in results]
            return outcome
        return {"workflow_id": workflow_id, "task": spec.id, "outcome": "DONE", "summary": report.summary, "remaining_tasks": remaining, "session_id": result.session_id}

    def _record_checks(self, workflow_id: str, task_id: str | None, results: list[Any]) -> None:
        with self._store() as store:
            with store.transaction() as conn:
                for item in results:
                    conn.execute(
                        "INSERT INTO acceptance_results(workflow_id, task_id, check_index, check_type, passed, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (workflow_id, task_id, item.index, item.check, 1 if item.passed else 0, str(redact(item.detail))[:500], utc_now()),
                    )
                    self._event(conn, workflow_id, "ACCEPTANCE_CHECKED", {"task": task_id, "check": item.check, "passed": item.passed})

    def _task_failed(
        self,
        workflow_id: str,
        task: TaskSpec,
        error: DridhError,
        *,
        repairable: bool = False,
        human_required: bool = False,
    ) -> dict[str, Any]:
        """Persist a failed attempt and bounded repair feedback.

        Infrastructure retries and correctable output/acceptance failures share the
        task's durable attempt counter. Security-boundary violations are never
        marked repairable by their call sites.
        """
        infrastructure_retry = error.code in (
            ErrorCode.WORKER_TIMEOUT,
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            ErrorCode.PROCESS_EXECUTION_FAILED,
        )
        retryable = infrastructure_retry or repairable
        with self._store() as store:
            with store.transaction() as conn:
                row = conn.execute(
                    "SELECT attempts, max_attempts FROM tasks WHERE workflow_id = ? AND id = ?",
                    (workflow_id, task.id),
                ).fetchone()
                attempts = int(row["attempts"])
                max_attempts = int(row["max_attempts"])
                attempts_left = attempts < max_attempts
                if error.code is ErrorCode.CANCELLED:
                    outcome_state = TaskState.CANCELLED
                elif retryable and attempts_left:
                    outcome_state = TaskState.INTERRUPTED
                else:
                    outcome_state = TaskState.FAILED
                details = tuple(str(redact(item))[:500] for item in error.details[:10])
                failure_message = str(redact(error.message))[:500]
                if details:
                    failure_message = (failure_message + " Findings: " + " | ".join(details))[:1200]
                if retryable and not attempts_left:
                    failure_message = (
                        f"Attempt limit exhausted after {attempts} of {max_attempts} attempts; "
                        "human intervention is required. " + failure_message
                    )[:1200]
                conn.execute(
                    "UPDATE tasks SET state = ?, lease_token = NULL, lease_expires_at = NULL, failure_code = ?, failure_message = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                    (outcome_state.value, error.code.value, failure_message, utc_now(), workflow_id, task.id),
                )
                self._event(
                    conn,
                    workflow_id,
                    f"TASK_{outcome_state.value}",
                    {
                        "task": task.id,
                        "code": error.code.value,
                        "attempt": attempts,
                        "max_attempts": max_attempts,
                        "repairable": repairable,
                        "details": list(details[:5]),
                    },
                )
                if human_required or (retryable and not attempts_left):
                    self._event(conn, workflow_id, "TASK_REPAIR_BLOCKED", {
                        "task": task.id,
                        "attempts": attempts,
                        "max_attempts": max_attempts,
                        "reason": "human intervention required",
                    })
                if outcome_state is TaskState.CANCELLED:
                    self._cancel_now(conn, workflow_id, error.message)
                elif outcome_state is TaskState.FAILED:
                    self._set_workflow_state(
                        conn,
                        workflow_id,
                        WorkflowState.FAILED,
                        failure_code=error.code.value,
                        failure_message=failure_message,
                    )
                    self._event(conn, workflow_id, "WORKFLOW_FAILED", {"task": task.id, "code": error.code.value})
        return {
            "workflow_id": workflow_id,
            "task": task.id,
            "outcome": outcome_state.value,
            "code": error.code.value,
            "message": error.message,
            "retryable": outcome_state is TaskState.INTERRUPTED,
            "repairable": repairable,
            "blocked_for_human": human_required or (retryable and not attempts_left),
            "escalation": "HUMAN_REQUIRED" if human_required or (retryable and not attempts_left) else None,
            "details": list(error.details)[:10],
        }

    async def _verify_workflow(self, workflow_id: str, completed_task: str | None = None) -> dict[str, Any]:
        with self._store() as store:
            with store.transaction() as conn:
                row = self._workflow_row(conn, workflow_id)
                if row["cancel_requested"]:
                    self._cancel_now(conn, workflow_id, "cancellation was requested")
                    raise DridhError(ErrorCode.CANCELLED, "The workflow was cancelled.")
                require_workflow_transition(row["state"], WorkflowState.VERIFYING, workflow_id)
                self._set_workflow_state(conn, workflow_id, WorkflowState.VERIFYING)
                self._event(conn, workflow_id, "WORKFLOW_VERIFYING", {})
                plan_checks = _plan_checks(conn, workflow_id)

        results = await run_checks(plan_checks, self.workspace.project_root, timeout_seconds=self.settings.acceptance_timeout_seconds)
        self._record_checks(workflow_id, None, results)
        failed = [item for item in results if not item.passed]
        with self._store() as store:
            with store.transaction() as conn:
                if failed:
                    details = tuple(f"{item.check}: {item.detail}" for item in failed[:5])
                    self._set_workflow_state(conn, workflow_id, WorkflowState.FAILED, failure_code=ErrorCode.ACCEPTANCE_FAILED.value, failure_message="workflow acceptance failed")
                    self._event(conn, workflow_id, "WORKFLOW_FAILED", {"code": ErrorCode.ACCEPTANCE_FAILED.value, "details": list(details)})
                else:
                    self._set_workflow_state(conn, workflow_id, WorkflowState.COMPLETED, failure_code=None, failure_message=None)
                    self._event(conn, workflow_id, "WORKFLOW_COMPLETED", {"checks": len(results)})
        report = self.report(workflow_id)
        return {
            "workflow_id": workflow_id,
            "task": completed_task,
            "outcome": "FAILED" if failed else "COMPLETED",
            "report": report["report_path"],
            "checks": [item.to_dict() for item in results],
        }

    # ------------------------------------------------------------------ workers

    def _default_runner(self, *, worktree_path: Path | None = None) -> WorkerRunner:
        if self._runner is not None:
            # Test mode: use the injected worker regardless of worktree.
            return self._runner
        root = worktree_path if worktree_path is not None else self.workspace.project_root
        return OpenCodeRunner(
            workspace_root=root,
            timeout=self.settings.worker_timeout_seconds,
        )

    async def _worker(
        self,
        workflow_id: str,
        *,
        task_id: str | None,
        purpose: str,
        agent: str,
        prompt: str,
        title: str,
        session_id: str | None = None,
        lease_token: str | None = None,
        worktree_path: Path | None = None,
    ) -> AgentRunResult:
        with self._store() as store:
            with store.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO worker_runs(workflow_id, task_id, purpose, agent, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (workflow_id, task_id, purpose, agent, "RUNNING", utc_now()),
                )
                run_id = int(cursor.lastrowid)
                self._event(conn, workflow_id, "WORKER_STARTED", {"agent": agent, "purpose": purpose, "task": task_id})

        runner = self._default_runner(worktree_path=worktree_path)
        worker = asyncio.ensure_future(runner.run(agent=agent, prompt=prompt, title=title, session_id=session_id, worktree_path=worktree_path))
        watcher = asyncio.ensure_future(self._watch(workflow_id, task_id, lease_token, worker))
        try:
            result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            self._finish_run(workflow_id, run_id, "CANCELLED", None, None, None, ErrorCode.CANCELLED.value)
            raise DridhError(ErrorCode.CANCELLED, "The worker run was cancelled.") from None
        except AgentRunError as exc:
            mapped = _map_runner_error(exc)
            self._finish_run(workflow_id, run_id, mapped.code.value, exc.session_id, None, None, mapped.code.value)
            raise mapped from None
        finally:
            watcher.cancel()

        output_digest = _digest(result.text)
        self._finish_run(workflow_id, run_id, "SUCCEEDED", result.session_id, result.exit_code, output_digest, None, result.duration_seconds)
        # Agents are instructed to call dridh.jev_decide at the end of each
        # turn. Verify that the tool persisted an evaluation for this session;
        # use a marked Python fallback only when the agent omitted the call.
        try:
            subject_id = result.session_id or f"worker-run-{run_id}"
            recorded = None
            if result.session_id:
                with self._store() as store:
                    recorded = store.read().execute(
                        "SELECT id,status,decision,output_json FROM jev_evaluation WHERE workspace_id=? AND subject_type='TURN' AND subject_id=? ORDER BY started_at DESC,id DESC LIMIT 1",
                        (self.workspace.workspace_id, result.session_id),
                    ).fetchone()
            if recorded is not None:
                self._event_once(workflow_id, "JEV_TURN_EVALUATED", {
                    "evaluation_id": recorded["id"],
                    "agent": agent,
                    "task": task_id,
                    "status": recorded["status"],
                    "decision": recorded["decision"],
                    "agent_tool_call": True,
                })
            else:
                turn_state = (
                    f"Agent: {agent}\nPurpose: {purpose}\nTask: {task_id or 'planning'}\n"
                    f"Output SHA256: {output_digest}\nOutput excerpt:\n{result.text[:1800]}"
                )
                evaluation = await self._evaluate_jev(
                    turn_state,
                    subject_type="TURN",
                    subject_id=subject_id,
                    agent_id=agent,
                    session_id=result.session_id,
                    evaluation_level="TURN",
                    task_type=purpose,
                    metadata={
                        "workflow_id": workflow_id,
                        "task_id": task_id,
                        "worker_run_id": run_id,
                        "agent_tool_call_missing": True,
                        "source_revision_sha": output_digest,
                    },
                )
                self._event_once(workflow_id, "JEV_TURN_EVALUATED", {
                    "evaluation_id": evaluation.evaluation_id,
                    "agent": agent,
                    "task": task_id,
                    "status": evaluation.status,
                    "decision": evaluation.decision,
                    "agent_tool_call": False,
                    "fallback_reason": "agent did not persist a turn-level Jev call",
                })
        except Exception as exc:  # noqa: BLE001
            self._event_once(workflow_id, "JEV_TURN_EVALUATION_FAILED", {
                "agent": agent,
                "task": task_id,
                "error_type": type(exc).__name__,
            })
        return result

    def _finish_run(
        self,
        workflow_id: str,
        run_id: int,
        status: str,
        session_id: str | None,
        exit_code: int | None,
        output_digest: str | None,
        error_code: str | None,
        duration: float | None = None,
    ) -> None:
        with self._store() as store:
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE worker_runs SET status = ?, session_id = ?, exit_code = ?, output_sha256 = ?, error_code = ?, duration_seconds = ?, finished_at = ? WHERE id = ?",
                    (status, session_id, exit_code, output_digest, error_code, duration, utc_now(), run_id),
                )
                self._event(conn, workflow_id, "WORKER_FINISHED", {"run": run_id, "status": status, "session_id": session_id, "error_code": error_code})

    async def _watch(self, workflow_id: str, task_id: str | None, lease_token: str | None, worker: asyncio.Future) -> None:
        """Heartbeat the lease, honour cancellation requests, and stop an orphaned worker."""

        last_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        while not worker.done():
            await asyncio.sleep(self.settings.cancel_poll_seconds)
            if worker.done():
                return
            if os.getppid() != self._parent_pid:
                self._event_once(workflow_id, "WORKER_ORPHANED", {"task": task_id})
                worker.cancel()
                return
            with self._store() as store:
                flag = store.read().execute("SELECT cancel_requested FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
                if flag is not None and flag[0]:
                    worker.cancel()
                    return
                if task_id and lease_token and loop.time() - last_heartbeat >= self.settings.heartbeat_seconds:
                    with store.transaction() as conn:
                        conn.execute(
                            "UPDATE tasks SET lease_expires_at = ? WHERE workflow_id = ? AND id = ? AND lease_token = ?",
                            (_iso(_now() + timedelta(seconds=self.settings.lease_ttl_seconds)), workflow_id, task_id, lease_token),
                        )
                    last_heartbeat = loop.time()

    def _event_once(self, workflow_id: str, event: str, payload: dict[str, Any]) -> None:
        with self._store() as store:
            with store.transaction() as conn:
                self._event(conn, workflow_id, event, payload)

    # ------------------------------------------------------------------ queries

    def status(self, workflow_id: str) -> dict[str, Any]:
        with self._store() as store:
            conn = store.read()
            row = self._workflow_row(conn, workflow_id)
            tasks = self._task_rows(conn, workflow_id)
            stages = list(conn.execute("SELECT id, title, ordinal FROM stages WHERE workflow_id = ? ORDER BY ordinal", (workflow_id,)))
            approvals = [dict(item) for item in conn.execute(
                "SELECT decision, actor, method, plan_sha256, created_at FROM approvals WHERE workflow_id = ? ORDER BY id", (workflow_id,))]
            ledger = verify_ledger(conn)
        state = WorkflowState(row["state"])
        return {
            "workflow_id": workflow_id,
            "workspace_id": self.workspace.workspace_id,
            "state": state.value,
            "objective": row["objective"],
            "cancel_requested": bool(row["cancel_requested"]),
            "plan": {
                "sha256": row["plan_sha256"],
                "revision": int(row["plan_revision"]),
                "stages": [dict(item) for item in stages],
                "tasks": [
                    {
                        "id": task["id"],
                        "stage_id": task["stage_id"],
                        "title": task["title"],
                        "agent": task["agent"],
                        "state": task["state"],
                        "attempts": int(task["attempts"]),
                        "max_attempts": int(task["max_attempts"]),
                        "depends_on": json.loads(task["depends_on"]),
                        "instructions": task["instructions"],
                        "allowed_paths": json.loads(task["allowed_paths"]),
                        "acceptance": json.loads(task["acceptance"]),
                        "parent_task_id": task["parent_task_id"],
                        "failure_code": task["failure_code"],
                        "blocked_for_human": bool(task["failure_message"] and "human intervention is required" in str(task["failure_message"]).lower()),
                    }
                    for task in tasks
                ],
            },
            "approvals": approvals,
            "failure": None if row["failure_code"] is None else {
                "code": row["failure_code"],
                "message": row["failure_message"],
                "blocked_for_human": bool(
                    row["failure_message"]
                    and "human intervention is required" in str(row["failure_message"]).lower()
                ),
            },
            "report_path": row["report_path"],
            "ledger": {"events": ledger["events"], "verified": ledger["ok"], "head": ledger["head"]},
            "next_action": _next_action(state, row["plan_sha256"]),
        }

    def next_task(self, workflow_id: str) -> dict[str, Any]:
        with self._store() as store:
            conn = store.read()
            row = self._workflow_row(conn, workflow_id)
            tasks = self._task_rows(conn, workflow_id)
        state = WorkflowState(row["state"])
        if state is WorkflowState.AWAITING_APPROVAL:
            return {"workflow_id": workflow_id, "ready": False, "reason": "APPROVAL_REQUIRED", "state": state.value}
        pending = [task for task in tasks if task["state"] not in (TaskState.DONE.value, TaskState.FAILED.value, TaskState.CANCELLED.value)]
        if not pending:
            return {"workflow_id": workflow_id, "ready": False, "reason": "NO_PENDING_TASKS", "state": state.value}
        candidate = pending[0]
        done = {task["id"] for task in tasks if task["state"] == TaskState.DONE.value}
        unmet = [item for item in json.loads(candidate["depends_on"]) if item not in done]
        return {
            "workflow_id": workflow_id,
            "ready": not unmet and candidate["state"] in (TaskState.PENDING.value, TaskState.INTERRUPTED.value),
            "task": candidate["id"],
            "agent": candidate["agent"],
            "title": candidate["title"],
            "state": candidate["state"],
            "waiting_for": unmet,
            "attempts": int(candidate["attempts"]),
            "max_attempts": int(candidate["max_attempts"]),
        }

    def report(self, workflow_id: str) -> dict[str, Any]:
        from dridh.reporting.report import build_report

        snapshot = self.status(workflow_id)
        with self._store() as store:
            conn = store.read()
            evidence = [dict(item) for item in conn.execute(
                "SELECT task_id, check_index, check_type, passed, detail FROM acceptance_results WHERE workflow_id = ? ORDER BY id", (workflow_id,))]
            runs = [dict(item) for item in conn.execute(
                "SELECT task_id, purpose, agent, session_id, status, exit_code, duration_seconds, error_code FROM worker_runs WHERE workflow_id = ? ORDER BY id", (workflow_id,))]
            ledger_head = verify_ledger(conn)
        markdown, document = build_report(snapshot, evidence, runs, ledger_head)
        report_path = self.workspace.report_path(workflow_id)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown, encoding="utf-8")
        json_path = self.workspace.artifact_dir(workflow_id) / "report.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        relative_report = report_path.relative_to(self.workspace.project_root).as_posix()
        relative_json = json_path.relative_to(self.workspace.project_root).as_posix()
        with self._store() as store:
            with store.transaction() as conn:
                conn.execute("UPDATE workflows SET report_path = ? WHERE id = ?", (relative_report, workflow_id))
                self._event(conn, workflow_id, "REPORT_WRITTEN", {"report_sha256": _digest(markdown)})
        return {
            "workflow_id": workflow_id,
            "report_path": relative_report,
            "json_path": relative_json,
            "report_sha256": _digest(markdown),
        }

    def verify_ledger(self) -> dict[str, Any]:
        with self._store() as store:
            return verify_ledger(store.read())


def _checks(raw: str) -> list[Any]:
    from dridh.orchestration.plan import AcceptanceCheck

    return [
        AcceptanceCheck(
            type=item["type"],
            path=item.get("path"),
            text=item.get("text"),
            argv=tuple(item.get("argv", ())),
        )
        for item in json.loads(raw)
    ]


def _plan_checks(conn: sqlite3.Connection, workflow_id: str) -> tuple[Any, ...]:
    """Workflow-level acceptance checks stored with the approved plan."""

    row = conn.execute("SELECT plan_acceptance FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return tuple(_checks(row["plan_acceptance"])) if row is not None else ()


def _map_runner_error(exc: AgentRunError) -> DridhError:
    if exc.code is AgentErrorCode.TIMEOUT:
        return DridhError(ErrorCode.WORKER_TIMEOUT, "The worker run exceeded its time limit.", details=(exc.code.value,), retryable=True)
    if exc.code is AgentErrorCode.CANCELLED:
        return DridhError(ErrorCode.CANCELLED, "The worker run was cancelled.", details=(exc.code.value,))
    if exc.code in (AgentErrorCode.OPENCODE_UNAVAILABLE,):
        return DridhError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The OpenCode runtime is unavailable.", details=(exc.code.value,))
    if exc.code is AgentErrorCode.PROCESS_FAILURE:
        return DridhError(ErrorCode.PROCESS_EXECUTION_FAILED, "The worker process failed.", details=(exc.code.value,))
    # Keep the (already redacted) provider diagnostic: "Free usage limit reached" is useless as a bare QUOTA_EXHAUSTED.
    diagnostics = tuple(item for item in exc.details if item.startswith("provider_diagnostic:"))[:1]
    recovery = ("The provider refused the request (quota, rate or free-tier limit). Wait and retry, switch model, or start a "
                "new session: Dridh's state is kept, so a fresh Coordinator session can continue.") if exc.code is AgentErrorCode.QUOTA_EXHAUSTED else None
    return DridhError(ErrorCode.WORKER_FAILED, "The worker run did not produce a usable result.", details=(exc.code.value, *diagnostics),
                      **({"recovery": recovery} if recovery else {}))


def _next_action(state: WorkflowState, plan_sha: str | None) -> str:
    return {
        WorkflowState.CREATED: "Request a plan with plan.",
        WorkflowState.PLANNING: "Submit a valid plan, or request a new one.",
        WorkflowState.AWAITING_APPROVAL: "A human must approve the plan in the terminal: dridh workflow approve.",
        WorkflowState.APPROVED: "Execute the next task.",
        WorkflowState.EXECUTING: "Execute the next task.",
        WorkflowState.VERIFYING: "Wait for workflow acceptance to finish.",
        WorkflowState.COMPLETED: "Read the completion report.",
        WorkflowState.FAILED: "Inspect the failure and the ledger, then decide the next step.",
        WorkflowState.CANCELLED: "No further action. The workflow was cancelled.",
    }[state]
