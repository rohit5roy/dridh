"""Workflow and task state machines. Transitions outside these tables are refused."""

from __future__ import annotations

from enum import Enum

from dridh.common.errors import DridhError, ErrorCode


class WorkflowState(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskState(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


WORKFLOW_TERMINAL = frozenset({WorkflowState.COMPLETED, WorkflowState.FAILED, WorkflowState.CANCELLED})
TASK_TERMINAL = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED})

WORKFLOW_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset({WorkflowState.PLANNING, WorkflowState.CANCELLED}),
    WorkflowState.PLANNING: frozenset({WorkflowState.AWAITING_APPROVAL, WorkflowState.APPROVED, WorkflowState.FAILED, WorkflowState.CANCELLED}),
    WorkflowState.AWAITING_APPROVAL: frozenset({WorkflowState.APPROVED, WorkflowState.PLANNING, WorkflowState.CANCELLED}),
    WorkflowState.APPROVED: frozenset({WorkflowState.EXECUTING, WorkflowState.VERIFYING, WorkflowState.CANCELLED}),
    WorkflowState.EXECUTING: frozenset({WorkflowState.VERIFYING, WorkflowState.FAILED, WorkflowState.CANCELLED}),
    WorkflowState.VERIFYING: frozenset({WorkflowState.COMPLETED, WorkflowState.FAILED, WorkflowState.CANCELLED}),
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.FAILED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.IN_PROGRESS, TaskState.CANCELLED}),
    TaskState.IN_PROGRESS: frozenset({TaskState.DONE, TaskState.FAILED, TaskState.INTERRUPTED, TaskState.CANCELLED}),
    TaskState.INTERRUPTED: frozenset({TaskState.PENDING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.DONE: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def require_workflow_transition(current: str, target: WorkflowState, workflow_id: str) -> WorkflowState:
    source = WorkflowState(current)
    if target not in WORKFLOW_TRANSITIONS[source]:
        raise DridhError(
            ErrorCode.INVALID_TRANSITION,
            "The workflow cannot move to the requested state from its current state.",
            details=(f"workflow: {workflow_id}", f"from: {source.value}", f"to: {target.value}"),
        )
    return target


def require_task_transition(current: str, target: TaskState, task_id: str) -> TaskState:
    source = TaskState(current)
    if target not in TASK_TRANSITIONS[source]:
        raise DridhError(
            ErrorCode.INVALID_TRANSITION,
            "The task cannot move to the requested state from its current state.",
            details=(f"task: {task_id}", f"from: {source.value}", f"to: {target.value}"),
        )
    return target
