"""DAG-based dependency scheduler for Phase 3.

Extends Phase 2's sequential engine with directed-acyclic-graph scheduling,
ready-task selection, bounded concurrency, and dependency validation. The
scheduler does not execute tasks itself; it decides which tasks are eligible
to run and enforces concurrency and dependency constraints.

The Phase 2 engine remains the authoritative writer of task state. The scheduler
is a pure-ish decision layer that the engine consults.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskNode:
    task_id: str
    depends_on: tuple[str, ...] = ()
    agent: str = ""
    max_concurrency_group: str = "default"


@dataclass
class ScheduleState:
    """Mutable scheduling state for one workflow run."""

    nodes: dict[str, TaskNode] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    in_progress: set[str] = field(default_factory=set)
    cancelled: set[str] = field(default_factory=set)
    concurrency_limit: int = 1
    per_agent_limits: dict[str, int] = field(default_factory=dict)


class CycleError(Exception):
    """Raised when the dependency graph contains a cycle."""


class MissingDependencyError(Exception):
    """Raised when a task references a non-existent dependency."""


def build_graph(nodes: list[TaskNode]) -> dict[str, TaskNode]:
    """Validate and return a task graph. Raises on missing deps or cycles."""
    graph = {n.task_id: n for n in nodes}
    # Check uniqueness.
    if len(graph) != len(nodes):
        ids = [n.task_id for n in nodes]
        dupes = [tid for tid in set(ids) if ids.count(tid) > 1]
        raise ValueError(f"Duplicate task IDs: {dupes}")
    # Check missing dependencies.
    for node in nodes:
        for dep in node.depends_on:
            if dep not in graph:
                raise MissingDependencyError(f"Task {node.task_id} depends on unknown task {dep}")
    # Cycle detection (Kahn's algorithm).
    in_degree = {tid: 0 for tid in graph}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dep in node.depends_on:
            in_degree[node.task_id] += 1
            dependents[dep].append(node.task_id)
    queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
    ordered: list[str] = []
    while queue:
        current = queue.popleft()
        ordered.append(current)
        for dependent in dependents[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)
    if len(ordered) != len(graph):
        # Cycle exists.
        remaining = set(graph) - set(ordered)
        raise CycleError(f"Dependency cycle detected among: {sorted(remaining)}")
    return graph


def ready_tasks(state: ScheduleState) -> list[str]:
    """Return task IDs that are ready to execute now."""
    ready = []
    for tid, node in state.nodes.items():
        if tid in state.completed or tid in state.failed or tid in state.in_progress or tid in state.cancelled:
            continue
        # All dependencies must be completed.
        if not all(dep in state.completed for dep in node.depends_on):
            # Check if any dependency failed -> this task is blocked.
            if any(dep in state.failed for dep in node.depends_on):
                continue
            # Still waiting on dependencies.
            continue
        ready.append(tid)
    return ready


def can_start(state: ScheduleState, task_id: str) -> bool:
    """Check if a task can start given concurrency limits."""
    node = state.nodes.get(task_id)
    if node is None:
        return False
    if len(state.in_progress) >= state.concurrency_limit:
        return False
    # Per-agent limit.
    agent = node.agent
    if agent:
        agent_count = sum(1 for tid in state.in_progress if state.nodes[tid].agent == agent)
        agent_limit = state.per_agent_limits.get(agent, state.concurrency_limit)
        if agent_count >= agent_limit:
            return False
    return True


def blocked_tasks(state: ScheduleState) -> list[str]:
    """Tasks that cannot proceed because a dependency failed."""
    blocked = []
    for tid, node in state.nodes.items():
        if tid in state.completed or tid in state.failed or tid in state.in_progress or tid in state.cancelled:
            continue
        if any(dep in state.failed for dep in node.depends_on):
            blocked.append(tid)
    return blocked


def waiting_tasks(state: ScheduleState) -> list[str]:
    """Tasks waiting on incomplete dependencies (not failed)."""
    waiting = []
    for tid, node in state.nodes.items():
        if tid in state.completed or tid in state.failed or tid in state.in_progress or tid in state.cancelled:
            continue
        if any(dep in state.failed for dep in node.depends_on):
            continue
        if not all(dep in state.completed for dep in node.depends_on):
            waiting.append(tid)
    return waiting


def mark_started(state: ScheduleState, task_id: str) -> None:
    state.in_progress.add(task_id)


def mark_completed(state: ScheduleState, task_id: str) -> None:
    state.in_progress.discard(task_id)
    state.completed.add(task_id)


def mark_failed(state: ScheduleState, task_id: str) -> None:
    state.in_progress.discard(task_id)
    state.failed.add(task_id)


def mark_cancelled(state: ScheduleState, task_id: str) -> None:
    state.in_progress.discard(task_id)
    state.cancelled.add(task_id)


def is_finished(state: ScheduleState) -> bool:
    return len(state.completed) + len(state.failed) + len(state.cancelled) == len(state.nodes)


def summary(state: ScheduleState) -> dict[str, Any]:
    return {
        "total": len(state.nodes),
        "completed": len(state.completed),
        "failed": len(state.failed),
        "cancelled": len(state.cancelled),
        "in_progress": len(state.in_progress),
        "ready": len(ready_tasks(state)),
        "blocked": len(blocked_tasks(state)),
        "waiting": len(waiting_tasks(state)),
    }
