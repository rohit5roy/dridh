"""Subprocess-level tests for `dridh validate` and `dridh ledger` (Phase 6).

These run the real CLI in a child process. Ledger approval is exercised through a
pseudo-terminal so the TTY gate is tested as a human would meet it, and through a
pipe to prove that non-interactive input is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dridh.document_contracts.ledger_store import load_ledger_decisions, record_ledger_decision
from dridh.document_contracts.validator import ledger_entry_revision
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
LEDGER_TEXT = (FIXTURES / "LEDGER.md").read_text(encoding="utf-8")


def run_cli(*args: str, stdin_text: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dridh", *args],
        input=stdin_text if stdin_text is not None else None,
        stdin=None if stdin_text is not None else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=180,
    )


def run_cli_on_tty(*args: str, typed: str) -> tuple[int, str, str]:
    """Run the CLI with a pseudo-terminal as stdin and type `typed` into it."""

    import pty

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "dridh", *args],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
    finally:
        os.close(slave)
    os.write(master, typed.encode())
    stdout, stderr = process.communicate(timeout=180)
    os.close(master)
    return process.returncode, stdout, stderr


def write_fixture(tmp_path: Path, name: str, content: str | None = None) -> Path:
    target = tmp_path / name
    target.write_text(content if content is not None else (FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_store(workspace: Path) -> StateStore:
    from dridh.workspace.layout import open_workspace

    info = open_workspace(workspace)
    return StateStore(info.state_db, info.workspace_id)


def ledger_events(workspace: Path) -> list[str]:
    with open_store(workspace) as store:
        rows = store.connection.execute("SELECT event_type FROM ledger ORDER BY seq").fetchall()  # type: ignore[union-attr]
    return [row["event_type"] for row in rows]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    init_workspace(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# validate: exit codes, JSON, human output, read-only behaviour
# ---------------------------------------------------------------------------


def test_valid_use_cases_exit_zero_with_json(tmp_path: Path) -> None:
    path = write_fixture(tmp_path, "001-USE-CASES.md")
    result = run_cli("validate", "use-cases", str(path), "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "PASS"
    assert document["command"] == "validate use-cases"
    assert document["findings"] == []


def test_invalid_document_exit_one_with_line_and_expected_actual(tmp_path: Path) -> None:
    text = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
    broken = text.replace("| Actor ID | Actor Name | Type | Status | Description |", "| Actor ID | Type | Actor Name | Status | Description |", 1)
    path = write_fixture(tmp_path, "001-USE-CASES.md", broken)
    result = run_cli("validate", "use-cases", str(path), "--json", cwd=tmp_path)
    assert result.returncode == 1
    findings = json.loads(result.stdout)["findings"]
    mismatch = next(f for f in findings if f["code"] == "TABLE_HEADER_MISMATCH")
    assert mismatch["line"] == broken.splitlines().index("| Actor ID | Type | Actor Name | Status | Description |") + 1
    assert mismatch["expected"] == "Actor ID | Actor Name | Type | Status | Description"
    assert mismatch["actual"] == "Actor ID | Type | Actor Name | Status | Description"


def test_human_output_reports_precise_location(tmp_path: Path) -> None:
    text = (FIXTURES / "001-USE-CASES.md").read_text(encoding="utf-8")
    path = write_fixture(tmp_path, "001-USE-CASES.md", text.replace("| ACT-02 | Payment Service | SERVICE | NEW | Authorises payments |", "| ACT-02 | Payment Service | PERSON | NEW | Authorises payments |"))
    result = run_cli("validate", "use-cases", str(path), cwd=tmp_path)
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL")
    assert "ENUM_INVALID 001-USE-CASES.md:" in result.stdout
    assert "expected:" in result.stdout and "actual:" in result.stdout


def test_missing_file_is_usage_error_exit_two(tmp_path: Path) -> None:
    result = run_cli("validate", "tdd", str(tmp_path / "absent.md"), "--json", cwd=tmp_path)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "USAGE_ERROR"


def test_validation_never_mutates_the_source(tmp_path: Path) -> None:
    path = write_fixture(tmp_path, "feature_tdd.md", (FIXTURES / "feature_tdd_residue.md").read_text(encoding="utf-8") + (FIXTURES / "feature_tdd.md").read_text(encoding="utf-8"))
    before = sha(path)
    run_cli("validate", "tdd", str(path), "--sanitize", "--json", cwd=tmp_path)
    run_cli("validate", "tdd", str(path), cwd=tmp_path)
    assert sha(path) == before


def test_tdd_sanitize_reports_actions_and_still_rejects_boilerplate(tmp_path: Path) -> None:
    residue = (FIXTURES / "feature_tdd.md").read_text(encoding="utf-8") + "\n<!-- note -->\n<aside>tip</aside>\nReplace the illustrative diagram above.\n"
    path = write_fixture(tmp_path, "feature_tdd.md", residue)
    result = run_cli("validate", "tdd", str(path), "--sanitize", "--json", cwd=tmp_path)
    assert result.returncode == 1
    document = json.loads(result.stdout)
    actions = {a["action"] for a in document["details"]["sanitization"]}
    assert actions == {"remove_html_comment", "remove_aside_callout"}
    codes = {f["code"] for f in document["findings"]}
    assert "TDD_BOILERPLATE" in codes
    assert "TDD_HTML_COMMENT" not in codes


def test_unsupported_document_in_cross_refs_is_failure(tmp_path: Path) -> None:
    notes = write_fixture(tmp_path, "notes.md", "Prose only.\n")
    use_cases = write_fixture(tmp_path, "001-USE-CASES.md")
    result = run_cli("validate", "cross-refs", str(notes), str(use_cases), "--json", cwd=tmp_path)
    assert result.returncode == 1
    assert "UNSUPPORTED_DOCUMENT" in {f["code"] for f in json.loads(result.stdout)["findings"]}


def test_cross_refs_pass_for_consistent_set_with_recorded_ledger_decision(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    entry = ledger_entry_revision(LEDGER_TEXT, "LDG-01")
    assert entry is not None
    with open_store(workspace) as store:
        with store.transaction() as connection:
            record_ledger_decision(connection, ledger_id="LDG-01", decision="approve", reviewer="Rohit Roy", reason="", entry_sha256=entry, document_sha256="x", actor="cli:test")
    paths = [str(FIXTURES / name) for name in ("001-USE-CASES.md", "feature_tdd.md", "CENTRAL-PLAN-INDEX.md", "COMPONENT-MASTER.md", "phase-1-order-service.md", "TEST-PLAN.md", "COORDINATOR.md", "QA-LOG.md")]
    result = run_cli("validate", "cross-refs", *paths, str(ledger), "--project", str(workspace), "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout


def test_cross_refs_missing_target_names_source_file_and_line(tmp_path: Path) -> None:
    text = (FIXTURES / "LEDGER.md").read_text(encoding="utf-8").replace("| ST-1.2 | Orders over", "| ST-8.8 | Orders over")
    ledger = write_fixture(tmp_path, "LEDGER.md", text)
    central = FIXTURES / "CENTRAL-PLAN-INDEX.md"
    use_cases = FIXTURES / "001-USE-CASES.md"
    result = run_cli("validate", "cross-refs", str(use_cases), str(central), str(ledger), "--json", cwd=tmp_path)
    missing = [f for f in json.loads(result.stdout)["findings"] if f["code"] == "XREF_MISSING_TARGET" and f["entity_id"] == "ST-8.8"]
    assert missing and missing[0]["file"] == "LEDGER.md" and isinstance(missing[0]["line"], int)


@pytest.mark.parametrize(
    ("command", "fixture"),
    [
        ("central-plan", "CENTRAL-PLAN-INDEX.md"),
        ("component-master", "COMPONENT-MASTER.md"),
        ("coordinator", "COORDINATOR.md"),
        ("qa-log", "QA-LOG.md"),
        ("test-plan", "TEST-PLAN.md"),
        ("story-dag", "CENTRAL-PLAN-INDEX.md"),
    ],
)
def test_named_validators_pass_on_canonical_fixtures(command: str, fixture: str, tmp_path: Path) -> None:
    path = FIXTURES / fixture
    result = run_cli("validate", command, str(path), "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase_files_command_checks_story_cards(tmp_path: Path) -> None:
    result = run_cli("validate", "phase-files", str(FIXTURES / "phase-1-order-service.md"), "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# ledger approve / reject: TTY-only, attributed, bound to the entry, audited
# ---------------------------------------------------------------------------


def test_ledger_approve_refuses_non_interactive_input(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    result = run_cli(
        "ledger", "approve", "LDG-01", "--reviewer", "Rohit Roy", "--ledger-file", str(ledger), "--project", str(workspace),
        stdin_text="APPROVE LDG-01\n",
    )
    assert result.returncode == 5
    assert "interactive terminal" in result.stderr
    assert "ledger.decision" not in ledger_events(workspace)


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminal test is POSIX-only")
def test_ledger_approve_on_terminal_is_attributed_bound_and_audited(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    returncode, stdout, stderr = run_cli_on_tty(
        "ledger", "approve", "LDG-01", "--reviewer", "Rohit Roy", "--ledger-file", str(ledger), "--project", str(workspace),
        typed="APPROVE LDG-01\n",
    )
    assert returncode == 0, stderr
    decision = json.loads(stdout)
    assert decision["decision"] == "approve" and decision["reviewer"] == "Rohit Roy"
    assert decision["entry_sha256"] == ledger_entry_revision(LEDGER_TEXT, "LDG-01")
    assert "ledger.decision" in ledger_events(workspace)
    validated = run_cli("validate", "ledger", str(ledger), "--project", str(workspace), "--json", cwd=tmp_path)
    assert validated.returncode == 0, validated.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminal test is POSIX-only")
def test_wrong_typed_confirmation_records_nothing(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    returncode, _, _ = run_cli_on_tty(
        "ledger", "approve", "LDG-01", "--reviewer", "Rohit Roy", "--ledger-file", str(ledger), "--project", str(workspace),
        typed="yes please\n",
    )
    assert returncode == 5
    assert "ledger.decision" not in ledger_events(workspace)


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminal test is POSIX-only")
def test_reject_on_terminal_blocks_the_deviation(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    returncode, _, stderr = run_cli_on_tty(
        "ledger", "reject", "LDG-01", "--reviewer", "Rohit Roy", "--reason", "needs evidence", "--ledger-file", str(ledger), "--project", str(workspace),
        typed="REJECT LDG-01\n",
    )
    assert returncode == 0, stderr
    validated = run_cli("validate", "ledger", str(ledger), "--project", str(workspace), "--json", cwd=tmp_path)
    assert "LEDGER_REJECTED" in {f["code"] for f in json.loads(validated.stdout)["findings"]}


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminal test is POSIX-only")
def test_editing_an_approved_entry_makes_it_stale_on_the_cli(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    returncode, _, stderr = run_cli_on_tty(
        "ledger", "approve", "LDG-01", "--reviewer", "Rohit Roy", "--ledger-file", str(ledger), "--project", str(workspace),
        typed="APPROVE LDG-01\n",
    )
    assert returncode == 0, stderr
    ledger.write_text(LEDGER_TEXT.replace("Review workflow is deferred", "Review workflow is deferred for now"), encoding="utf-8")
    validated = run_cli("validate", "ledger", str(ledger), "--project", str(workspace), "--json", cwd=tmp_path)
    assert "LEDGER_APPROVAL_STALE" in {f["code"] for f in json.loads(validated.stdout)["findings"]}


def test_done_entries_cannot_be_approved(workspace: Path, tmp_path: Path) -> None:
    ledger = write_fixture(tmp_path, "LEDGER.md")
    result = run_cli("ledger", "approve", "LDG-02", "--reviewer", "Rohit Roy", "--ledger-file", str(ledger), "--project", str(workspace), stdin_text="APPROVE LDG-02\n")
    assert result.returncode == 4
    assert "only DEVIATED, NOT_DONE and WONT_DO" in result.stderr


def test_bridge_plugin_exposes_no_ledger_or_approval_tools() -> None:
    plugin_root = Path(__file__).resolve().parents[2] / "opencode" / "plugins" / "dridh-bridge"
    sources = [
        p
        for p in plugin_root.rglob("*")
        if p.is_file() and p.suffix in {".ts", ".js", ".mjs"} and "node_modules" not in p.parts and "dist" not in p.parts
    ]
    assert sources, "plugin sources must be present for this check"
    for source in sources:
        text = source.read_text(encoding="utf-8", errors="ignore")
        assert "ledger approve" not in text and "ledger reject" not in text, source
        assert "record_ledger_decision" not in text, source


def test_ledger_decisions_loader_is_empty_for_fresh_workspace(workspace: Path) -> None:
    with open_store(workspace) as store:
        assert dict(load_ledger_decisions(store.connection)) == {}  # type: ignore[arg-type]



# Packaged Phase 6 contract registry


def test_contract_bundle_cli_reports_integrity_verified(tmp_path: Path) -> None:
    result = run_cli("contracts", "verify", "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["contracts"]["standards_version"] == "2.1.0"
    assert len(payload["contracts"]["standards_sha256"]) == 64
    assert len(payload["contracts"]["template_sha256"]) == 64


def test_contract_bundle_cli_show_exposes_manifest_metadata(tmp_path: Path) -> None:
    result = run_cli("contracts", "show", "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["contracts"]["manifest"]["schema"] == "dridh.document-contracts/1"
    assert payload["contracts"]["template_version"] == "2.1.0"
