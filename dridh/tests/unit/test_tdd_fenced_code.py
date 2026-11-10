"""HTML-comment detection in TDDs must not fire on Mermaid arrows or other `-->` inside fenced code."""

from __future__ import annotations

from pathlib import Path

from dridh.document_contracts.validator import validate_tdd

VALID = (Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts" / "feature_tdd.md").read_text(encoding="utf-8")


def _codes(content: str) -> list[str]:
    return [error.split(" ", 1)[0] for error in validate_tdd(content, "feature_tdd.md").errors]


def test_mermaid_arrows_in_a_fenced_block_are_not_html_comments() -> None:
    diagram = "\n```mermaid\nflowchart LR\n    A[Caller] --> B[Service]\n```\n"
    assert "TDD_HTML_COMMENT" not in _codes(VALID + diagram)


def test_html_comments_in_prose_are_still_rejected() -> None:
    assert "TDD_HTML_COMMENT" in _codes(VALID + "\n<!-- leftover note -->\n")


def test_a_comment_after_a_closed_fence_is_still_rejected() -> None:
    content = VALID + "\n```text\na --> b\n```\n\nstray --> arrow outside code\n"
    assert "TDD_HTML_COMMENT" in _codes(content)
