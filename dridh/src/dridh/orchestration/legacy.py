"""The generic workflow planner (``workflow create`` / ``workflow plan``) is retired.

Features go through the lifecycle instead: Ideator, Designer and Planner, then ``dridh planner approve`` and
``dridh execute``. Creating or planning a generic workflow is refused unless ``DRIDH_LEGACY_WORKFLOW=1`` is set; that
switch exists only for the packaged E2E harness and contract tests, which still use the engine to exercise the bridge,
worker process handling and lease recovery until they are moved onto the lifecycle. Existing workflows can still be
inspected, approved, run and cancelled.
"""

from __future__ import annotations

import os

from dridh.common.errors import DridhError, ErrorCode

LEGACY_ENV = "DRIDH_LEGACY_WORKFLOW"


def legacy_enabled() -> bool:
    return os.environ.get(LEGACY_ENV) == "1"


def require_legacy(what: str) -> None:
    if legacy_enabled():
        return
    raise DridhError(
        ErrorCode.CONFIGURATION_ERROR,
        f"{what} is retired: generic workflows are no longer created or planned.",
        recovery="Use the feature lifecycle through the Coordinator (Ideator, Designer, Planner), then `dridh planner approve` and `dridh execute start`.",
    )
