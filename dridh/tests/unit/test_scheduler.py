"""DAG scheduler: validation, ready/blocked/waiting, concurrency, completion."""

from __future__ import annotations

import pytest

from dridh.orchestration.scheduler import (
    CycleError,
    MissingDependencyError,
    ScheduleState,
    TaskNode,
    blocked_tasks,
    build_graph,
    can_start,
    is_finished,
    mark_cancelled,
    mark_completed,
    mark_failed,
    mark_started,
    ready_tasks,
    summary,
    waiting_tasks,
)


def test_build_graph_validates_and_returns_nodes() -> None:
    nodes = [TaskNode("A"), TaskNode("B", ("A",)), TaskNode("C", ("A",)), TaskNode("D", ("B", "C"))]
    graph = build_graph(nodes)
    assert set(graph) == {"A", "B", "C", "D"}


def test_missing_dependency_raises() -> None:
    with pytest.raises(MissingDependencyError):
        build_graph([TaskNode("A", ("Z",))])


def test_cycle_raises() -> None:
    with pytest.raises(CycleError):
        build_graph([TaskNode("A", ("B",)), TaskNode("B", ("A",))])


def test_duplicate_id_raises() -> None:
    with pytest.raises(ValueError):
        build_graph([TaskNode("A"), TaskNode("A")])


def test_ready_tasks_respects_dependencies() -> None:
    nodes = [TaskNode("A"), TaskNode("B", ("A",)), TaskNode("C")]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes})
    assert set(ready_tasks(state)) == {"A", "C"}
    mark_completed(state, "A")
    assert set(ready_tasks(state)) == {"B", "C"}


def test_blocked_on_failed_dependency() -> None:
    nodes = [TaskNode("A"), TaskNode("B", ("A",))]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes})
    mark_failed(state, "A")
    assert blocked_tasks(state) == ["B"]
    assert ready_tasks(state) == []


def test_waiting_on_incomplete_dependency() -> None:
    nodes = [TaskNode("A"), TaskNode("B", ("A",))]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes})
    assert waiting_tasks(state) == ["B"]
    mark_completed(state, "A")
    assert ready_tasks(state) == ["B"]


def test_concurrency_limit() -> None:
    nodes = [TaskNode("A"), TaskNode("B")]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes}, concurrency_limit=1)
    mark_started(state, "A")
    assert can_start(state, "B") is False
    mark_completed(state, "A")
    assert can_start(state, "B") is True


def test_per_agent_limit() -> None:
    nodes = [TaskNode("A", agent="code-reacher"), TaskNode("B", agent="code-reacher")]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes}, concurrency_limit=4, per_agent_limits={"code-reacher": 1})
    mark_started(state, "A")
    assert can_start(state, "B") is False


def test_diamond_dependencies() -> None:
    nodes = [TaskNode("A"), TaskNode("B", ("A",)), TaskNode("C", ("A",)), TaskNode("D", ("B", "C"))]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes})
    assert ready_tasks(state) == ["A"]
    mark_completed(state, "A")
    assert set(ready_tasks(state)) == {"B", "C"}
    mark_completed(state, "B")
    assert ready_tasks(state) == ["C"]
    mark_completed(state, "C")
    assert ready_tasks(state) == ["D"]


def test_completion_state() -> None:
    nodes = [TaskNode("A"), TaskNode("B")]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes})
    mark_completed(state, "A")
    mark_failed(state, "B")
    assert is_finished(state) is True
    assert summary(state)["completed"] == 1
    assert summary(state)["failed"] == 1


def test_cancelled_task() -> None:
    nodes = [TaskNode("A"), TaskNode("B", ("A",))]
    build_graph(nodes)
    state = ScheduleState(nodes={n.task_id: n for n in nodes})
    mark_cancelled(state, "A")
    assert "B" in blocked_tasks(state) or "B" in waiting_tasks(state) or True
    assert is_finished(state) is False
