"""Phase 12 designer bridge operations are allow-listed and approval stays terminal-only."""

from __future__ import annotations

from pathlib import Path

from dridh.common.protocol import ALLOWED_OPERATIONS, DESIGNER_OPERATIONS, JEV_OPERATIONS, ORCHESTRATION_OPERATIONS, RUNTIME_OPERATIONS

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "src" / "dridh" / "orchestration" / "bridge_ops.py"


def test_designer_operations_are_allowlisted_without_approval() -> None:
    expected = {
        "designer.start",
        "designer.answer",
        "designer.status",
        "designer.generate",
        "designer.parity",
        "designer.gate",
    }
    assert DESIGNER_OPERATIONS == expected
    assert DESIGNER_OPERATIONS <= ALLOWED_OPERATIONS
    assert not any("approve" in operation or "reject" in operation for operation in ALLOWED_OPERATIONS)


def test_coordinator_dispatches_designer_operations_but_never_approval() -> None:
    source = BRIDGE.read_text(encoding="utf-8")
    for operation in DESIGNER_OPERATIONS:
        assert operation.split(".", 1)[1] in source
    assert "designer.approve" not in source
    assert "Only the Dridh Coordinator" in source
    from dridh.common.protocol import CODE_REACHER_OPERATIONS
    from dridh.hooks import HOOK_OPERATIONS

    assert ALLOWED_OPERATIONS == (set(RUNTIME_OPERATIONS) | set(JEV_OPERATIONS) | set(ORCHESTRATION_OPERATIONS)
                                  | set(CODE_REACHER_OPERATIONS) | set(HOOK_OPERATIONS))
