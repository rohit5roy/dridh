"""Document contract validators (Phase 6).

Valid fixtures live in tests/fixtures/document_contracts. Each negative test
mutates one valid fixture by a single exact substitution and asserts the rule code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dridh.document_contracts.markdown import parse_tables, split_row
from dridh.document_contracts.validator import (
    LedgerDecision,
    content_revision,
    ledger_entry_revision,
    sanitize_tdd,
    validate_cross_references,
    validate_document,
    validate_document_collection,
    validate_document_sync,
    validate_file,
    validate_story_dag,
    validate_tdd,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"
USE_CASES = "001-USE-CASES.md"
TDD = "feature_tdd.md"
CENTRAL = "CENTRAL-PLAN-INDEX.md"
COMPONENT = "COMPONENT-MASTER.md"
PHASE = "phase-1-order-service.md"
LEDGER = "LEDGER.md"
TEST_PLAN = "TEST-PLAN.md"
COORDINATOR = "COORDINATOR.md"
QA_LOG = "QA-LOG.md"
USE_CASES_REVISION = "rev-usecases-v1"

VALID = {
    name: (FIXTURES / name).read_text(encoding="utf-8")
    for name in (USE_CASES, TDD, CENTRAL, COMPONENT, PHASE, LEDGER, TEST_PLAN, COORDINATOR, QA_LOG)
}
LEDGER_DECISIONS = {
    "LDG-01": LedgerDecision("approve", ledger_entry_revision(VALID[LEDGER], "LDG-01") or "", "Rohit Roy", "2026-11-06T10:00:00Z"),
}


def mutate(name: str, old: str, new: str) -> dict[str, str]:
    content = VALID[name]
    assert content.count(old) == 1, f"fixture substitution must be unique: {old!r}"
    docs = dict(VALID)
    docs[name] = content.replace(old, new)
    return docs


def single(name: str, old: str, new: str) -> set[str]:
    """Validate one mutated document and return its error codes."""
    docs = mutate(name, old, new)
    return validate_document(name, docs[name], ledger_decisions=LEDGER_DECISIONS).codes


def cross(name: str, old: str, new: str) -> tuple[set[str], list[str]]:
    result = validate_cross_references(mutate(name, old, new))
    return {f.code for f in result.findings if f.severity == "ERROR"}, [f.file or "" for f in result.findings]


# ---------------------------------------------------------------------------
# Valid fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_fixture_passes(name: str) -> None:
    result = validate_document(name, VALID[name], ledger_decisions=LEDGER_DECISIONS)
    assert result.ok, result.errors


def test_valid_collection_passes_with_cross_references() -> None:
    result = validate_document_collection(VALID, ledger_decisions=LEDGER_DECISIONS)
    assert result.ok, result.errors


def test_stage2_tdd_validates_without_stage3_files() -> None:
    docs = {USE_CASES: VALID[USE_CASES], TDD: VALID[TDD]}
    result = validate_document_collection(docs)
    assert result.ok, result.errors


def test_validation_result_is_json_serialisable() -> None:
    result = single_result(USE_CASES, "| ACT-02 | Payment Service | SERVICE | NEW |", "| ACT-02 | Payment Service | PERSON | NEW |")
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "ENUM_INVALID"
    assert payload["findings"][0]["line"] is not None


def single_result(name: str, old: str, new: str):  # noqa: ANN201
    return validate_document(name, mutate(name, old, new)[name])


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------


def test_parser_honours_escaped_pipes_and_skips_fenced_tables() -> None:
    text = "\n".join(
        [
            "| A | B |",
            "|---|---|",
            "| one \\| two | three |",
            "",
            "```",
            "| X | Y |",
            "|---|---|",
            "```",
        ]
    )
    tables = parse_tables(text)
    assert len(tables) == 1
    assert tables[0].rows[0].cells == ("one | two", "three")
    assert split_row("| a | b |") == ["a", "b"]


# ---------------------------------------------------------------------------
# Use Cases (Table 1A, Table 1, Table 1B)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("| Actor ID | Actor Name | Type | Status | Description |", "| Actor ID | Type | Actor Name | Status | Description |", "TABLE_HEADER_MISMATCH"),
        ("| ACT-02 | Payment Service | SERVICE | NEW | Authorises payments |", "| ACT-02 | Payment Service | SERVICE | NEW |", "ROW_WIDTH"),
        ("| ACT-01 | Customer | USER |", "| ACT-01 | Customer | PERSON |", "ENUM_INVALID"),
        ("| UC-02 | Cancel an order | ACT-01 |", "| UC-02 | Cancel an order | ACT-09 |", "ACTOR_UNRESOLVED"),
        ("| AF-01.1 | ALTERNATIVE | SF-01.1 |", "| AF-02.1 | ALTERNATIVE | SF-01.1 |", "FLOW_PARENT_MISMATCH"),
        ("| EF-01.1 | EXCEPTION | SF-01.2 |", "| EF-01.1 | EXCEPTION | SF-01.9 |", "ORIGIN_INVALID"),
        ("| EF-02.1 | EXCEPTION | SF-02.1 |", "| EF-02.1 | EXCEPTION | SF-01.2 |", "ORIGIN_INVALID"),
        ("| BR-01.1 | BUSINESS_RULE | SF-01.2 |", "| BR-01.1 | BUSINESS_RULE | — |", "ORIGIN_INVALID"),
        ("| SF-01.2 | SUCCESS | — |", "| SF-01.2 | SUCCESS | SF-01.1 |", "ORIGIN_NOT_ALLOWED"),
        ("| BE-01.1 | BOUNDARY |", "| BE-01.1 | EXCEPTION |", "FLOW_TYPE_MISMATCH"),
        ("| SF-01.2 | SUCCESS |", "| SF-01.0 | SUCCESS |", "SF_ORDER"),
        ("| BE-01.1 | BOUNDARY | SF-01.1 |", "| AF-01.1 | BOUNDARY | SF-01.1 |", "DUPLICATE_ID"),
        ("### UC-02: Cancel an order", "### UC-22: Cancel an order", "UC_SECTION_MISSING"),
        ("| ID | Use Case | Primary Actor | Trigger | Category |", "| ID | Use Case | Primary Actor | Trigger | Area |", "TABLE_HEADER_MISMATCH"),
        ("| Flow ID | Type | Origin Step | Subject | Actor ID | Action | Result | Priority |\n|---|---|---|---|---|---|---|---|\n| SF-02.1", "| Flow ID | Type | Origin Step | Actor ID | Subject | Action | Result | Priority |\n|---|---|---|---|---|---|---|---|\n| SF-02.1", "TABLE_HEADER_MISMATCH"),
    ],
)
def test_use_case_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(USE_CASES, old, new)


def test_use_case_missing_flow_table_is_reported() -> None:
    content = VALID[USE_CASES]
    start = content.index("### UC-02: Cancel an order")
    end = content.index("## Assumptions and Constraints")
    docs = dict(VALID)
    docs[USE_CASES] = content[:start] + "### UC-02: Cancel an order\n\n" + content[end:]
    assert "FLOW_TABLE_MISSING" in validate_document(USE_CASES, docs[USE_CASES]).codes


def test_use_case_document_validates_success_order_and_parent_mapping() -> None:
    result = validate_document(USE_CASES, VALID[USE_CASES])
    assert result.ok, result.errors


# ---------------------------------------------------------------------------
# Central plan (Table 2) and story DAG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("| Parallel With | Status |", "| Parallel | Status |", "TABLE_HEADER_MISMATCH"),
        ("| 2 | ST-1.2 | CMP-01 — Order Service | 1 | UC-01 | Persist orders | ST-1.1 |", "| 2 | ST-1.2 | CMP-01 — Order Service | 1 | UC-01 | Persist orders | ST-9.9 |", "UNKNOWN_DEPENDENCY"),
        ("| None | ST-1.2 |", "| None | — |", "UNLOCKS_MISMATCH"),
        ("| Domain model | None |", "| Domain model | ST-1.2 |", "DEPENDENCY_CYCLE"),
        ("| 2 | ST-1.2 |", "| 5 | ST-1.2 |", "ORDER_SEQUENCE"),
        ("| CMP-02 — Notification Service |", "| CMP-02 Notification Service |", "COMPONENT_FORMAT"),
        ("| Persist orders | ST-1.1 | — | — |", "| Persist orders | ST-1.1 | — | ST-1.1 |", "PARALLEL_DEPENDENCY_CONFLICT"),
        ("| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | None | ST-1.2 | ST-2.1 | NOT_STARTED |", "| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | None | ST-1.2 | ST-2.1 | BOGUS |", "ENUM_INVALID"),
    ],
)
def test_central_plan_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(CENTRAL, old, new)


def test_central_plan_missing_table_is_reported() -> None:
    result = validate_document(CENTRAL, "# Plan\n\n> **Standards Version:** 2.1.0  \n")
    assert "TABLE_MISSING" in result.codes


def test_story_dag_detects_cycles_and_unknown_dependencies() -> None:
    assert "DEPENDENCY_CYCLE" in validate_story_dag({"A": ["B"], "B": ["A"]}).codes
    assert "UNKNOWN_DEPENDENCY" in validate_story_dag({"A": ["Z"]}).codes
    assert "UNLOCKS_MISMATCH" in validate_story_dag({"A": [], "B": ["A"]}, unlocks={"A": [], "B": []}).codes


def test_story_dag_accepts_diamond_and_checks_topological_order() -> None:
    deps = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    unlocks = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
    order = {"A": 1, "B": 2, "C": 3, "D": 4}
    assert validate_story_dag(deps, unlocks=unlocks, order=order).ok
    assert "TOPOLOGICAL_ORDER" in validate_story_dag({"A": ["B"], "B": []}, order={"A": 1, "B": 2}).codes


# ---------------------------------------------------------------------------
# Phase files (Table 3 story cards)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("| **Unlocks** | ST-1.2 |\n", "", "STORY_CARD_FIELDS"),
        ("| **Status** | NOT_STARTED |\n| **Dependencies** | None |", "| **Status** | DONE |\n| **Dependencies** | None |", "ENUM_INVALID"),
        ("## ST-1.2: Persist orders", "## ST-1.2: Persist order data", "STORY_TITLE_MISMATCH"),
        ("| **Dependencies** | ST-1.1 |", "| **Dependencies** | ST-1.x |", "ID_FORMAT"),
        ("| **Definition of Done** | Integration tests pass against a real database. |", "| **Definition of Done** | — |", "STORY_FIELD_EMPTY"),
        ("| **Flow IDs** | SF-01.2 |", "| **Flow IDs** | UC-01 |", "ID_FORMAT"),
        ("| Field | Value |\n|:------|:------|\n| **Use Case ID** | UC-01 |\n| **Flow IDs** | SF-01.1, SF-01.2 |", "| Key | Value |\n|:------|:------|\n| **Use Case ID** | UC-01 |\n| **Flow IDs** | SF-01.1, SF-01.2 |", "STORY_CARD_MISSING"),
    ],
)
def test_phase_story_card_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(PHASE, old, new)


def test_story_card_flow_ids_may_not_contain_parent_uc() -> None:
    assert "ID_FORMAT" in single(PHASE, "| **Flow IDs** | SF-01.2 |", "| **Flow IDs** | UC-01 |")


# ---------------------------------------------------------------------------
# Component master (Tables 4 and 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("## Component Overview", "### CMP-01: Order Service\n\n## Component Overview", "CMP_REDEFINED"),
        ("| **Total** | 2 | 2 | 0 | 0 | 0 | 0 |", "| **Total** | 3 | 2 | 0 | 0 | 0 | 0 |", "TOTAL_MISMATCH"),
        ("| 1 | 2 | 2 | 0 | 0 | 0 | 0 |", "| 1 | 2 | 1 | 1 | 0 | 0 | 0 |", "COUNT_MISMATCH"),
        ("| Story ID | Phase | Story Name | Use Cases | Status |", "| Story ID | Phase | Name | Use Cases | Status |", "TABLE_HEADER_MISMATCH"),
    ],
)
def test_component_master_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(COMPONENT, old, new)


# ---------------------------------------------------------------------------
# Ledger (Table 14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("| Rohit Roy |", "| PENDING |", "APPROVAL_REQUIRED"),
        ("| DEVIATED | ST-1.2", "| APPROVED | ST-1.2", "ENUM_INVALID"),
        ("#### LDG-01: Review limit replaced by rejection", "#### LDG-09: Review limit replaced by rejection", "LEDGER_DEFINITION_MISSING"),
        ("| LDG-02 | UC-02 | DONE", "| LDG-01 | UC-02 | DONE", "DUPLICATE_ID"),
        ("| LDG-02 | UC-02", "| LDG-2 | UC-02", "ID_FORMAT"),
        ("| ST-1.2 | Orders over", "| ST-1 | Orders over", "ID_FORMAT"),
    ],
)
def test_ledger_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(LEDGER, old, new)


def test_ledger_agent_placeholder_approval_cannot_satisfy_deviation() -> None:
    for placeholder in ("—", "N/A", "TBD", ""):
        assert "APPROVAL_REQUIRED" in single(LEDGER, "| Rohit Roy |", f"| {placeholder} |")


# ---------------------------------------------------------------------------
# Test plan (Table 15)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("owner: Payments team; ", "", "WIREMOCK_JUSTIFICATION_INCOMPLETE"),
        (" | DOCKER_COMPOSE | ", " | MOCKS | ", "ENUM_INVALID"),
        ("Report under verification/order-intake/s1 | NOT_STARTED |", "Report under verification/order-intake/s1 | DONE |", "ENUM_INVALID"),
        ("| S1 | Order placement journey |", "| Q1 | Order placement journey |", "ID_FORMAT"),
        ("| Test Scope | Use Case / Flow IDs |", "| Use Case / Flow IDs | Test Scope |", "TABLE_HEADER_MISMATCH"),
        ("| UC-01 (SF-01.1, SF-01.2) | CMP-01 |", "| UC-1 (SF-01.1, SF-01.2) | CMP-01 |", "ID_FORMAT"),
    ],
)
def test_test_plan_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(TEST_PLAN, old, new)


# ---------------------------------------------------------------------------
# TDD schema, sanitisation and placeholders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("## Dependencies\n\nThe payment gateway must expose an authorisation endpoint.\n\n", "", "TDD_MISSING_SECTION"),
        ("## Rollback Plan\n\nN/A — feature flag removal is the rollback.\n", "## Rollback Plan\n", "TDD_EMPTY_SECTION"),
        ("## Document Control\n", "<!-- note -->\n## Document Control\n", "TDD_HTML_COMMENT"),
        ("## Scope\n", "<aside>Explain scope.</aside>\n## Scope\n", "TDD_ASIDE"),
        ("The order service is a new component that calls the existing payment gateway.", "The order service is a new component.\nReplace the illustrative diagram above with the actual architecture.", "TDD_BOILERPLATE"),
        ("Orders must be captured and paid for without duplicate charges.", "Orders must be captured <FEATURE NAME> without duplicate charges.", "TDD_PLACEHOLDER"),
        ("| UC ID | Flow IDs | Catering Component(s) | API / Event | Design Decision (DD-N) |", "| UC ID | Flow IDs | Catering Component(s) | Design Decision (DD-N) | API / Event |", "TABLE_HEADER_MISMATCH"),
        ("| UC-01 | SF-01.1, SF-01.2, EF-01.1 | CMP-01 — Order Service |", "| UC-01 | SF-01.1, SF-01.2, EF-01.1 | CMP-07 — Order Service |", "TDD_UNRESOLVED_COMPONENT"),
        ("| POST /orders | DD-1 |", "| POST /orders | DD-9 |", "TDD_UNRESOLVED_DECISION"),
        ("| UC-01 | SF-01.1, SF-01.2, EF-01.1 | CMP-01 — Order Service | POST /orders | DD-1 |", "| UC-01 | UC-01 | CMP-01 — Order Service | POST /orders | DD-1 |", "ID_FORMAT"),
    ],
)
def test_tdd_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(TDD, old, new)


def test_tdd_sanitiser_removes_comments_and_callouts_and_reports_actions() -> None:
    residue = VALID[TDD] + "\n<!-- TEMPLATE AUTHORING CONTRACT -->\n<aside>Tip</aside>\n"
    raw = validate_tdd(residue, file=TDD).codes
    assert {"TDD_HTML_COMMENT", "TDD_ASIDE", "TDD_BOILERPLATE"} <= raw

    sanitized, actions = sanitize_tdd(residue)
    assert "<!--" not in sanitized
    assert "<aside" not in sanitized
    assert {a["action"] for a in actions} == {"remove_html_comment", "remove_aside_callout"}
    assert validate_tdd(sanitized, file=TDD).ok, validate_tdd(sanitized, file=TDD).errors


def test_tdd_residue_fixture_is_rejected_after_sanitisation() -> None:
    raw = (FIXTURES / "feature_tdd_residue.md").read_text(encoding="utf-8")
    sanitized, _ = sanitize_tdd(raw)
    codes = validate_tdd(sanitized, file=TDD).codes
    assert "TDD_BOILERPLATE" in codes
    assert "TDD_PLACEHOLDER" in codes
    assert "TDD_HTML_COMMENT" not in codes
    assert "TDD_ASIDE" not in codes


def test_tdd_optional_sections_are_retained_and_marked() -> None:
    text = VALID[TDD]
    assert "## Monitoring & Alerting\n\nN/A" in text
    assert "## AI Systems\n\nN/A" in text


# ---------------------------------------------------------------------------
# Cross-document references (source-to-target, owning scope)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "old", "new", "target", "source_file"),
    [
        (LEDGER, "| ST-1.2 | Orders over", "| ST-8.8 | Orders over", "ST-8.8", LEDGER),
        (LEDGER, "| BR-01.1 | DEVIATED", "| BR-09.1 | DEVIATED", "BR-09.1", LEDGER),
        (CENTRAL, "| CMP-02 — Notification Service | 2 |", "| CMP-09 — Notification Service | 2 |", "CMP-09", CENTRAL),
        (TEST_PLAN, "| CMP-01 | TESTCONTAINERS", "| CMP-05 | TESTCONTAINERS", "CMP-05", TEST_PLAN),
        (PHASE, "| **Dependencies** | ST-1.1 |", "| **Dependencies** | ST-7.7 |", "ST-7.7", PHASE),
        (PHASE, "| **Use Case ID** | UC-01 |\n| **Flow IDs** | SF-01.1, SF-01.2 |", "| **Use Case ID** | UC-07 |\n| **Flow IDs** | SF-01.1, SF-01.2 |", "UC-07", PHASE),
        (COMPONENT, "| ST-1.2 | 1 | Persist orders | UC-01 |", "| ST-1.2 | 1 | Persist orders | UC-09 |", "UC-09", COMPONENT),
    ],
)
def test_cross_reference_missing_targets_name_source_document(name: str, old: str, new: str, target: str, source_file: str) -> None:
    codes, files = cross(name, old, new)
    assert "XREF_MISSING_TARGET" in codes
    assert source_file in files


def test_cross_reference_uncovered_use_case_in_traceability_matrix() -> None:
    codes, _ = cross(TDD, "| UC-02 | SF-02.1, EF-02.1 |", "| UC-05 | SF-02.1, EF-02.1 |")
    assert {"XREF_MISSING_TARGET", "XREF_UC_UNCOVERED"} <= codes


def test_cross_reference_is_stage_aware_without_stage3_documents() -> None:
    docs = {TDD: VALID[TDD]}
    result = validate_cross_references(docs)
    assert result.ok, result.errors


def test_cross_reference_rejects_stage2_component_defined_only_in_stage3() -> None:
    docs = {USE_CASES: VALID[USE_CASES], TDD: VALID[TDD].replace("### CMP-02: Notification Service", "### CMP-03: Notification Service")}
    result = validate_cross_references(docs)
    assert "XREF_MISSING_TARGET" in result.codes


# ---------------------------------------------------------------------------
# Revision binding, metadata and unknown documents
# ---------------------------------------------------------------------------


def test_stale_source_revision_is_detected() -> None:
    assert validate_document_sync(VALID[TDD], USE_CASES_REVISION).ok
    stale = validate_document_sync(VALID[TDD], "rev-usecases-v2")
    assert "DOC_STALE_REVISION" in stale.codes
    assert validate_document_sync("# no metadata", USE_CASES_REVISION).codes == {"DOC_REVISION_MISSING"}


def test_validate_document_with_expected_revision_flags_stale_tdd() -> None:
    assert "DOC_STALE_REVISION" in validate_document(TDD, VALID[TDD], expected_revision="rev-usecases-v2").codes


def test_content_revision_is_stable_across_line_endings() -> None:
    assert content_revision("a\r\nb") == content_revision("a\nb")


def test_unsupported_standards_version_is_rejected() -> None:
    assert "UNSUPPORTED_STANDARDS_VERSION" in single(USE_CASES, "**Standards Version:** 2.1.0", "**Standards Version:** 1.0.0")


def test_unknown_document_type_never_passes_silently() -> None:
    result = validate_document("notes.md", "Some prose with no schema.")
    assert result.ok is False
    assert result.codes == {"UNSUPPORTED_DOCUMENT"}


def test_validate_file_reads_canonical_file_and_reports_missing(tmp_path: Path) -> None:
    target = tmp_path / LEDGER
    target.write_text(VALID[LEDGER], encoding="utf-8")
    assert validate_file(target, ledger_decisions=LEDGER_DECISIONS).ok
    missing = validate_file(tmp_path / "missing" / LEDGER)
    assert missing.codes == {"FILE_NOT_FOUND"}


# ---------------------------------------------------------------------------
# Structural separator rows (malformed separators must not pass)
# ---------------------------------------------------------------------------


def _line_number(content: str, line: str) -> int:
    return content.splitlines().index(line) + 1


def test_separator_width_mismatch_is_rejected_with_exact_line() -> None:
    old = "|---|---|---|---|---|\n| ACT-01 | Customer"
    new_separator = "|---|---|---|---|"
    docs = mutate(USE_CASES, old, f"{new_separator}\n| ACT-01 | Customer")
    result = validate_document(USE_CASES, docs[USE_CASES])
    findings = [f for f in result.findings if f.code == "TABLE_SEPARATOR_INVALID"]
    assert findings, result.errors
    assert findings[0].line == _line_number(docs[USE_CASES], new_separator)
    assert "columns" in findings[0].message
    assert result.ok is False


def test_invalid_separator_cell_syntax_is_rejected() -> None:
    codes = single(USE_CASES, "|---|---|---|---|---|\n| ACT-01 | Customer", "|---|---|x|---|---|\n| ACT-01 | Customer")
    assert "TABLE_SEPARATOR_INVALID" in codes


def test_malformed_separator_in_any_table_fails_the_document() -> None:
    codes = single(COORDINATOR, "|---|---|---|---|---|---|---|---|\n| CMP-01", "|---|---|---|\n| CMP-01")
    assert "TABLE_SEPARATOR_INVALID" in codes


def test_escaped_pipes_and_fenced_tables_still_behave() -> None:
    escaped = mutate(USE_CASES, "| ACT-01 | Customer | USER | EXISTING | Places and cancels orders |", "| ACT-01 | Customer | USER | EXISTING | Places \\| cancels orders |")
    assert validate_document(USE_CASES, escaped[USE_CASES]).ok
    fenced = VALID[USE_CASES] + "\n```\n| Broken | table |\n|---|\n```\n"
    assert validate_document(USE_CASES, fenced).ok


# ---------------------------------------------------------------------------
# Coordinator tables (6, 7, 8, 12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("| 1 | Ideation | Ideator | REVIEWED |", "| 2 | Ideation | Ideator | REVIEWED |", "ORDER_SEQUENCE"),
        ("| IN_PROGRESS | `.opencode/dridh/documents/CENTRAL-PLAN-INDEX.md`", "| IN_PROGRES | `.opencode/dridh/documents/CENTRAL-PLAN-INDEX.md`", "ENUM_INVALID"),
        ("`.opencode/dridh/documents/feature_tdd.md`", "`/etc/feature_tdd.md`", "ID_FORMAT"),
        ("`.opencode/dridh/documents/001-USE-CASES.md` | 2026-11-05", "`.opencode/dridh/documents/001-USE-CASES.md` | 05/11/2026", "DATE_FORMAT"),
        ("| 10 | Retrospective | Coordinator | N/A | — | — | — |\n", "", "PIPELINE_STAGE_COUNT"),
        ("| CMP-01 — Order Service | 1 | phase-1/order-service | — | NOT_CREATED | NOT_STARTED | 0 | 2 |", "| CMP-01 — Order Service | 1 | phase-1/order-service | — | NOT_CREATED | NOT_STARTED | 3 | 2 |", "COUNT_MISMATCH"),
        ("| NOT_CREATED | NOT_STARTED | 0 | 2 |", "| BOGUS | NOT_STARTED | 0 | 2 |", "ENUM_INVALID"),
        ("| CMP-02 — Notification Service | 2 |", "| CMP-02 Notification Service | 2 |", "COMPONENT_FORMAT"),
        ("| READY_FOR_REVIEW |", "| SHIPPED |", "ENUM_INVALID"),
        ("| 92/100 |", "| 150/100 |", "ID_FORMAT"),
        ("| 9/10 |", "| 11/10 |", "COUNT_MISMATCH"),
        ("| Document | Readiness | Checklist | Sections | Blockers | Last Reviewer |", "| Document | Readiness | Checklist | Sections | Last Reviewer | Blockers |", "TABLE_HEADER_MISMATCH"),
        ("| Component | Phase | Branch | PR # | PR Status | Build Status | Stories Done | Stories Total |", "| Name | Phase | Branch | PR # | PR Status | Build Status | Stories Done | Stories Total |", "TABLE_MISSING"),
    ],
)
def test_coordinator_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(COORDINATOR, old, new)


def test_coordinator_valid_fixture_has_all_four_tables() -> None:
    tables = parse_tables(VALID[COORDINATOR])
    headers = {t.header[0] for t in tables}
    assert {"#", "Document", "Component"} <= headers
    assert len(tables) == 4


# ---------------------------------------------------------------------------
# Q&A log (Table 11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("| 2 | Is refund handling", "| 5 | Is refund handling", "ORDER_SEQUENCE"),
        ("| N/A — refunds are a later feature |", "| N/A |", "QA_ANSWER_INVALID"),
        ("| Product owner | Affects UC-01 |", "|  | Affects UC-01 |", "QA_SOURCE_MISSING"),
        ("| Yes, see BR-01.1 |", "| — |", "QA_ANSWER_MISSING"),
        ("| Ideator, UC-01 |", "|  |", "STORY_FIELD_EMPTY"),
    ],
)
def test_qa_log_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(QA_LOG, old, new)


def test_qa_open_question_is_visible_as_warning_not_silent_pass() -> None:
    result = validate_document(QA_LOG, VALID[QA_LOG])
    assert result.ok
    assert any(f.code == "QA_OPEN_QUESTION" and f.severity == "WARNING" for f in result.findings)


def test_qa_log_requires_rows_or_an_explicit_empty_row() -> None:
    meta = "> **Standards Version:** 2.1.0  \n"
    header = "| # | Question | Asked In | Answer | Source | Impact |\n|---|---|---|---|---|---|\n"
    assert "QA_EMPTY_TABLE" in validate_document(QA_LOG, "# Q&A\n\n" + meta + header).codes
    assert validate_document(QA_LOG, "# Q&A\n\n" + meta + header + "| — | — | — | — | — | — |\n").ok
    assert "TABLE_MISSING" in validate_document(QA_LOG, "# Q&A\n\n" + meta + "Question: is it in scope? Answer: yes.\n").codes
    assert "UNSUPPORTED_DOCUMENT" in validate_document("notes.md", "# Q&A\n\nQuestion: is it in scope? Answer: yes.\n").codes


# ---------------------------------------------------------------------------
# Risks (Table 9) and milestones (Table 10)
# ---------------------------------------------------------------------------

_RISK_ROW = "| R1 | Duplicate payment authorisation | Medium | High | Idempotency keys | Platform team |"
_MILESTONE_ROW = "| M1 | Orders persisted | ST-1.1 and ST-1.2 complete | — | NOT_STARTED |"


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (_RISK_ROW, _RISK_ROW.replace("| Medium |", "| Severe |"), "ENUM_INVALID"),
        (_RISK_ROW, _RISK_ROW.replace("| Idempotency keys |", "| — |"), "STORY_FIELD_EMPTY"),
        (_RISK_ROW, _RISK_ROW.replace("| R1 |", "| X1 |"), "ID_FORMAT"),
        (_MILESTONE_ROW, _MILESTONE_ROW.replace("| — |", "| 11/06/2026 |"), "DATE_FORMAT"),
        (_MILESTONE_ROW, _MILESTONE_ROW.replace("NOT_STARTED |", "DONE |"), "ENUM_INVALID"),
        (_MILESTONE_ROW, _MILESTONE_ROW.replace("| M1 |", "| M2 |"), "MILESTONE_DEFINITION_MISSING"),
        ("### M1: Orders persisted", "### M9: Orders persisted", "MILESTONE_DEFINITION_MISSING"),
        (_MILESTONE_ROW, _MILESTONE_ROW.replace("| Orders persisted | ST-1.1 and ST-1.2 complete |", "| Orders persisted | — |"), "STORY_FIELD_EMPTY"),
    ],
)
def test_risk_and_milestone_rule_violations(old: str, new: str, code: str) -> None:
    assert code in single(CENTRAL, old, new)


def test_unassigned_risk_owner_is_a_warning() -> None:
    docs = mutate(CENTRAL, _RISK_ROW, _RISK_ROW.replace("| Platform team |", "| — |"))
    result = validate_document(CENTRAL, docs[CENTRAL])
    assert result.ok
    assert any(f.code == "RISK_OWNER_UNASSIGNED" and f.severity == "WARNING" for f in result.findings)


# ---------------------------------------------------------------------------
# Cross-document: RISK, CDD and duplicate definitions, unknown documents
# ---------------------------------------------------------------------------


def test_central_risk_reference_must_resolve_to_a_definition() -> None:
    codes, files = cross(CENTRAL, _RISK_ROW, _RISK_ROW.replace("| R1 |", "| R9 |"))
    assert "XREF_MISSING_TARGET" in codes
    assert CENTRAL in files


def test_risk_defined_in_tdd_and_central_is_a_duplicate() -> None:
    docs = dict(VALID)
    docs[CENTRAL] = VALID[CENTRAL] + "\n### R1: Duplicate payment authorisation\n\nRestated.\n"
    result = validate_cross_references(docs)
    assert "DUPLICATE_DEFINITION" in result.codes


def test_cdd_defined_in_two_component_masters_is_a_duplicate() -> None:
    docs = {"COMPONENT-MASTER.md": VALID[COMPONENT], "COMPONENT-MASTER-2.md": VALID[COMPONENT]}
    result = validate_cross_references(docs)
    assert "DUPLICATE_DEFINITION" in result.codes


def test_feature_and_component_decisions_stay_distinct() -> None:
    assert "TDD_CDD_REFERENCE" in single(TDD, "| POST /orders | DD-1 |", "| POST /orders | CDD-1 |")
    assert "COMPONENT_DD_DEFINITION" in single(COMPONENT, "#### CDD-1: Store orders in one table", "#### DD-2: Store orders in one table")


def test_unknown_document_in_collection_is_an_error_not_skipped() -> None:
    docs = dict(VALID)
    docs["notes.md"] = "Free prose with no schema."
    result = validate_cross_references(docs)
    assert "UNSUPPORTED_DOCUMENT" in result.codes


# ---------------------------------------------------------------------------
# Human ledger decisions (Table 14)
# ---------------------------------------------------------------------------


def test_deviation_without_recorded_decision_is_not_verified() -> None:
    assert "LEDGER_APPROVAL_NOT_VERIFIED" in validate_document(LEDGER, VALID[LEDGER]).codes


def test_markdown_approved_by_cell_alone_never_satisfies_the_gate() -> None:
    assert "LEDGER_APPROVAL_MISSING" in validate_document(LEDGER, VALID[LEDGER], ledger_decisions={}).codes


def test_rejected_decision_blocks_the_deviation() -> None:
    rejected = {"LDG-01": LedgerDecision("reject", LEDGER_DECISIONS["LDG-01"].entry_sha256, "Rohit Roy", "2026-11-06T10:00:00Z")}
    assert "LEDGER_REJECTED" in validate_document(LEDGER, VALID[LEDGER], ledger_decisions=rejected).codes


def test_edited_entry_makes_approval_stale() -> None:
    docs = mutate(LEDGER, "Review workflow is deferred", "Review workflow is deferred for now")
    assert "LEDGER_APPROVAL_STALE" in validate_document(LEDGER, docs[LEDGER], ledger_decisions=LEDGER_DECISIONS).codes


def test_approver_must_match_recorded_reviewer() -> None:
    other = {"LDG-01": LedgerDecision("approve", LEDGER_DECISIONS["LDG-01"].entry_sha256, "Someone Else", "2026-11-06T10:00:00Z")}
    assert "LEDGER_APPROVER_MISMATCH" in validate_document(LEDGER, VALID[LEDGER], ledger_decisions=other).codes


def test_done_entry_needs_no_decision() -> None:
    docs = mutate(LEDGER, "| LDG-01 | BR-01.1 | DEVIATED | ST-1.2 |", "| LDG-01 | BR-01.1 | DONE | ST-1.2 |")
    docs[LEDGER] = docs[LEDGER].replace("| Rohit Roy |", "| — |")
    codes = validate_document(LEDGER, docs[LEDGER], ledger_decisions={}).codes
    assert not {"LEDGER_APPROVAL_NOT_VERIFIED", "LEDGER_APPROVAL_MISSING"} & codes


def test_ledger_decisions_persist_in_audit_chain_and_bind_to_entry(tmp_path: Path) -> None:
    from dridh.document_contracts.ledger_store import load_ledger_decisions, record_ledger_decision
    from dridh.state.store import StateStore, verify_ledger

    entry = ledger_entry_revision(VALID[LEDGER], "LDG-01")
    assert entry is not None
    with StateStore(tmp_path / "state.db", "ws-ledger-test") as store:
        with store.transaction() as connection:
            record_ledger_decision(connection, ledger_id="LDG-01", decision="reject", reviewer="Rohit Roy", reason="needs evidence", entry_sha256=entry, document_sha256="doc", actor="cli:tester")
        with store.transaction() as connection:
            record_ledger_decision(connection, ledger_id="LDG-01", decision="approve", reviewer="Rohit Roy", reason="", entry_sha256=entry, document_sha256="doc", actor="cli:tester")
            latest = dict(load_ledger_decisions(connection))
            verdict = verify_ledger(connection)
    assert latest["LDG-01"].decision == "approve"
    assert latest["LDG-01"].entry_sha256 == entry
    assert verdict["ok"] is True


def test_record_ledger_decision_rejects_unknown_decision(tmp_path: Path) -> None:
    from dridh.document_contracts.ledger_store import record_ledger_decision
    from dridh.state.store import StateStore

    with StateStore(tmp_path / "state.db", "ws-ledger-test") as store, store.transaction() as connection:
        with pytest.raises(ValueError):
            record_ledger_decision(connection, ledger_id="LDG-01", decision="maybe", reviewer="x", reason="", entry_sha256="e", document_sha256="d", actor="cli:tester")


def test_story_graph_validator_reports_cycles_only() -> None:
    from dridh.document_contracts.validator import validate_story_graph

    cyclic = mutate(CENTRAL, "| Domain model | None |", "| Domain model | ST-1.2 |")[CENTRAL]
    assert "DEPENDENCY_CYCLE" in validate_story_graph(cyclic).codes
    assert validate_story_graph(VALID[CENTRAL]).ok
