"""Recovery scenario 5: a half-written Markdown document (fault injection).

Two failure shapes: the writing process dies mid-write, and a canonical document is truncated on disk afterwards.
Neither may leave a half-written document in place or let a truncated document pass a gate.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dridh.document_contracts.validator import validate_use_cases
from tests.e2e.planner_test_helpers import tick_review
from tests.unit.test_ideator_service import complete_discovery, load_proposal, service_for, workspace

WRITER = textwrap.dedent("""
    import importlib, os, sys, time
    module = importlib.import_module(sys.argv[1])
    real_fsync = os.fsync
    def slow_fsync(fd):
        real_fsync(fd)
        open(sys.argv[3], "w").close()   # tell the parent the temporary file is fully written but not yet renamed
        time.sleep(30)
    os.fsync = slow_fsync
    module._atomic_write(__import__("pathlib").Path(sys.argv[2]), "# NEW DOCUMENT\\n" + "row | row | row\\n" * 200000)
""")


@pytest.mark.parametrize("module", ["dridh.ideator.service", "dridh.planner.service"])
def test_a_writer_killed_mid_write_leaves_the_previous_document(tmp_path: Path, module: str) -> None:
    target = tmp_path / "docs" / "001-USE-CASES.md"
    target.parent.mkdir()
    target.write_text("# PREVIOUS DOCUMENT\n", encoding="utf-8")
    ready = tmp_path / "ready"
    process = subprocess.Popen([sys.executable, "-c", WRITER, module, str(target), str(ready)], start_new_session=True)
    deadline = time.monotonic() + 60
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "the writer never reached the point of no return"
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=30)

    assert target.read_text(encoding="utf-8") == "# PREVIOUS DOCUMENT\n"  # never half-written
    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers and all(name.startswith(".001-USE-CASES.md.") and name.endswith(".tmp") for name in leftovers)
    # The stray temporary file does not get in the way of the next write.
    __import__(module, fromlist=["_atomic_write"])._atomic_write(target, "# NEXT DOCUMENT\n")
    assert target.read_text(encoding="utf-8") == "# NEXT DOCUMENT\n"


def test_a_row_cut_off_mid_cell_is_reported_but_tables_written_without_trailing_pipes_are_not() -> None:
    from dridh.document_contracts.markdown import parse_tables

    cut = parse_tables("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | fo")[0]
    assert [row.terminated for row in cut.rows] == [True, False] and cut.header_terminated
    bare = parse_tables("A | B\n---|---\n1 | 2\n")
    assert bare == [] or all(row.terminated is False and not table.header_terminated for table in bare for row in table.rows)
    escaped = parse_tables("| A | B |\n|---|---|\n| 1 | ends with \\| |\n| 2 | ends badly \\|\n")[0]
    assert [row.terminated for row in escaped.rows] == [True, False]


def test_the_designer_writes_its_document_atomically() -> None:
    source = Path(__import__("dridh.designer.service", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "_atomic_write(target, document)" in source and "target.write_text(document" not in source


def test_a_truncated_approved_use_case_document_is_caught_and_relocks_the_designer(tmp_path: Path) -> None:
    project, info = workspace(tmp_path)
    service = service_for(info)
    session_id = service.start("Build order intake")["session_id"]
    complete_discovery(service, session_id)
    service.generate(session_id, proposal=load_proposal(), run_workers=False)
    challenge = service.approval_challenge(session_id)
    tick_review(service.workspace, challenge["review_id"])
    service.approve(session_id, reviewer="Reviewer", confirmation=challenge["confirmation"])
    assert service.gate(session_id)["unlocked"] is True

    target = project / "001-USE-CASES.md"
    text = target.read_text(encoding="utf-8")
    target.write_text(text[: len(text) // 2], encoding="utf-8")  # as if a crash or a bad copy cut it in half

    result = validate_use_cases(target.read_text(encoding="utf-8"), file=target.name)
    assert result.ok is False and "TABLE_ROW_UNTERMINATED" in {f.code for f in result.findings}
    gate = service.gate(session_id)
    assert gate["unlocked"] is False
    from dridh.common.errors import DridhError
    from dridh.designer.service import DesignerService

    with pytest.raises(DridhError):
        DesignerService(info).start(session_id, feature="Order intake")  # the Designer only starts from the exact approved document
