"""Mermaid diagrams drawn as text for the terminal reviewer."""

from __future__ import annotations

from dridh.ui.mermaid import render, render_markdown


def test_left_to_right_flowchart_from_the_tdd_template() -> None:
    text = render("flowchart LR\n    User[User / Client] --> Entry[Entry Point / API]\n    Entry --> A[Component A]\n")
    assert text is not None
    first_row = text.splitlines()[1]
    assert first_row.index("User / Client") < first_row.index("Entry Point / API") < first_row.index("Component A")
    assert "►" in text


def test_top_down_flowchart_with_labels_and_a_skipped_level() -> None:
    text = render(
        "flowchart TD\n  C[Caller] --> G[greet]\n  C --> F[farewell]\n  G --> V{validate}\n  F --> V\n"
        "  V -->|valid| OK[Format]\n  V -->|invalid| E[Error]\n  C --> E\n"
    )
    assert text is not None
    rows = text.splitlines()
    row_of = {label: next(i for i, r in enumerate(rows) if label in r) for label in ("Caller", "greet", "validate", "Format")}
    assert row_of["Caller"] < row_of["greet"] < row_of["validate"] < row_of["Format"]
    assert "valid" in text and "invalid" in text and text.count("▼") == 5  # one arrowhead per target box; edges into a box share it


def test_cycles_are_listed_rather_than_dropped() -> None:
    text = render("graph TD\n  A --> B\n  B --> A\n")
    assert text is not None and "↺ B → A" in text


def test_sequence_diagram_uses_aliases_and_dashed_replies() -> None:
    text = render("sequenceDiagram\n  participant F as farewell()\n  participant V\n  F->>V: name\n  V-->>F: \"Ada\"\n")
    assert text is not None
    assert "farewell()" in text and "─►" in text and "◄┄" in text


def test_er_diagram_reads_cardinality() -> None:
    assert render("erDiagram\n  ORDER ||--o{ LINE : contains\n") == "ORDER  (one) ──contains──► (zero or more)  LINE"


def test_unsupported_or_malformed_diagrams_keep_their_source() -> None:
    markdown = "x\n```mermaid\npie title Pets\n  \"Dogs\" : 3\n```\n\n```mermaid\nflowchart LR\n  A -->\n```\n"
    out, drawn, kept = render_markdown(markdown)
    assert (drawn, kept) == (0, 2) and out == markdown


def test_markdown_blocks_are_replaced_in_place() -> None:
    out, drawn, kept = render_markdown("# T\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\nafter\n")
    assert (drawn, kept) == (1, 0)
    assert out.startswith("# T\n\n```text\n") and "```mermaid" not in out and out.endswith("\nafter\n")
