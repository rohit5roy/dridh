"""Pure delivery rules: PR base selection (16.5) and check evaluation (16.3)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

FAILING = {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
NOT_PASSING_UNLESS_OPTIONAL = {"skipped", "neutral"}


def evaluate_checks(checks: list[dict[str, Any]], *, required: list[str], optional: list[str]) -> dict[str, Any]:
    """PASS only when every required check finished successfully; a missing result is never success.

    With no explicit ``required`` list, every reported check is required except those named in ``optional``.
    Skipped or neutral results pass only for optional checks.
    """
    if not checks:
        return {"state": "PENDING", "reasons": ["No check results have been reported for this commit yet."]}
    # GitHub reports a job once per triggering event (push and pull_request) under the same name, so a name can have several
    # runs. Every run of a required name must succeed: one failing duplicate must never be masked by a passing one.
    runs: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        runs.setdefault(check["name"], []).append(check)
    names = required or [name for name in runs if name not in optional]
    if not names:
        return {"state": "PENDING", "reasons": ["No required checks are configured or reported."]}
    failed: list[str] = []
    waiting: list[str] = []
    for name in names:
        group = runs.get(name)
        if not group:
            waiting.append(f"Required check '{name}' has not been reported.")
            continue
        for check in group:
            if check["status"] != "completed":
                waiting.append(f"Check '{name}' is {check['status']}.")
            elif check["conclusion"] == "success":
                continue
            elif check["conclusion"] in NOT_PASSING_UNLESS_OPTIONAL and name in optional:
                continue
            else:
                failed.append(f"Check '{name}' concluded {check['conclusion']}.")
    if failed:
        return {"state": "FAIL", "reasons": sorted(set(failed)) + sorted(set(waiting))}
    return {"state": "PENDING" if waiting else "PASS", "reasons": sorted(set(waiting))}


def select_base(*, direct: list[str], transitive: set[str], target: str, integration_branch: str,
                predecessor_branch: Callable[[str], str | None], contains_ancestry: Callable[[str, set[str]], bool]) -> dict[str, str]:
    """Deterministic PR base: target, a single predecessor's branch, or the shared integration branch."""
    if not direct:
        return {"base": target, "rule": "no dependencies: the target branch"}
    if len(direct) == 1:
        branch = predecessor_branch(direct[0])
        if branch and contains_ancestry(branch, transitive):
            return {"base": branch, "rule": f"one dependency ({direct[0]}) whose branch holds the full ancestry"}
        return {"base": integration_branch, "rule": "the single predecessor branch lacks the full ancestry: the integration branch"}
    return {"base": integration_branch, "rule": "several dependencies: the shared integration branch"}
