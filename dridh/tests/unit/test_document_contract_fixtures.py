"""Named invalid fixtures: one file per rule family, each checked by an explicit validator.

Parameterised mutation tests in test_document_contracts.py cover the rule matrix. These
files are the readable, reviewable evidence that a real defective document fails with
the expected rule codes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dridh.document_contracts.findings import ValidationResult
from dridh.document_contracts.validator import (
    validate_central_plan,
    validate_coordinator,
    validate_document,
    validate_ledger,
    validate_phase_file,
    validate_tdd,
    validate_test_plan,
    validate_use_cases,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
INVALID = FIXTURES / "invalid"


def _run(validator: Callable[..., ValidationResult], name: str) -> ValidationResult:
    return validator((INVALID / name).read_text(encoding="utf-8"), file=name)


CASES: list[tuple[str, Callable[..., ValidationResult], set[str]]] = [
    ("uc-1a-wrong-column-order.md", validate_use_cases, {"TABLE_HEADER_MISMATCH"}),
    ("uc-1a-separator-width.md", validate_use_cases, {"TABLE_SEPARATOR_INVALID"}),
    ("uc-1a-row-width.md", validate_use_cases, {"ROW_WIDTH"}),
    ("uc-1b-duplicate-flow-id.md", validate_use_cases, {"DUPLICATE_ID", "FLOW_TYPE_MISMATCH"}),
    ("uc-1b-origin-in-other-uc.md", validate_use_cases, {"ORIGIN_INVALID"}),
    ("central-dependency-cycle.md", validate_central_plan, {"DEPENDENCY_CYCLE"}),
    ("central-unlocks-stale.md", validate_central_plan, {"UNLOCKS_MISMATCH"}),
    ("coordinator-missing-table-8.md", validate_coordinator, {"TABLE_MISSING"}),
    ("tdd-unresolved-placeholder.md", validate_tdd, {"TDD_PLACEHOLDER"}),
    ("tdd-table-13-unknown-decision.md", validate_tdd, {"TDD_UNRESOLVED_DECISION"}),
    ("test-plan-invalid-dependency-mode.md", validate_test_plan, {"ENUM_INVALID"}),
    ("test-plan-wiremock-unjustified.md", validate_test_plan, {"WIREMOCK_JUSTIFICATION_INCOMPLETE"}),
    ("phase-story-card-missing.md", validate_phase_file, set()),
]


@pytest.mark.parametrize(("name", "validator", "expected"), CASES, ids=[c[0] for c in CASES])
def test_named_invalid_fixture_fails_with_expected_rule(name: str, validator: Callable[..., ValidationResult], expected: set[str]) -> None:
    result = _run(validator, name)
    assert result.ok is False, f"{name} must not pass"
    if expected:
        assert expected <= result.codes, (name, sorted(result.codes))


def test_phase_file_without_story_card_reports_missing_card() -> None:
    result = _run(validate_phase_file, "phase-story-card-missing.md")
    assert "STORY_CARD_MISSING" in result.codes


def test_unapproved_ledger_deviation_is_not_accepted() -> None:
    text = (INVALID / "ledger-unapproved-deviation.md").read_text(encoding="utf-8")
    result = validate_ledger(text, file="LEDGER.md", decisions=None)
    assert {"APPROVAL_REQUIRED", "LEDGER_APPROVAL_NOT_VERIFIED"} <= result.codes


def test_stale_source_revision_fixture_is_detected() -> None:
    text = (INVALID / "tdd-stale-source-revision.md").read_text(encoding="utf-8")
    result = validate_document("feature_tdd.md", text, expected_revision="rev-usecases-v1")
    assert "DOC_STALE_REVISION" in result.codes


def test_unsupported_document_fixture_never_passes() -> None:
    text = (INVALID / "notes-unsupported.md").read_text(encoding="utf-8")
    result = validate_document("notes-unsupported.md", text)
    assert result.ok is False
    assert result.codes == {"UNSUPPORTED_DOCUMENT"}


def test_every_named_invalid_fixture_is_covered_by_a_case() -> None:
    covered = {name for name, _, _ in CASES} | {
        "ledger-unapproved-deviation.md",
        "tdd-stale-source-revision.md",
        "notes-unsupported.md",
    }
    on_disk = {p.name for p in INVALID.glob("*.md")}
    assert on_disk == covered, on_disk ^ covered
