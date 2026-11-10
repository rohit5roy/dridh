"""Phase 12 Designer CLI exposes inquiry, TDD generation and terminal approval commands."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_designer_cli_exposes_the_phase12_lifecycle(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dridh", "designer", "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    for command in ("start", "answer", "status", "parity", "generate", "gate", "approve"):
        assert command in result.stdout
