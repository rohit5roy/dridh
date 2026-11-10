"""Structural Markdown parsing for Dridh document contracts.

Tables are parsed into header and row cells, so validators check column
structure rather than searching for substrings. Fenced code blocks are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Row:
    line: int
    cells: tuple[str, ...]
    terminated: bool = True  # ends with an unescaped "|"


@dataclass(frozen=True)
class Table:
    line: int
    header: tuple[str, ...]
    separator_ok: bool
    rows: tuple[Row, ...]
    separator_line: int = 0
    separator_cells: tuple[str, ...] = ()
    separator_error: str | None = None
    header_terminated: bool = True


@dataclass(frozen=True)
class Heading:
    line: int
    level: int
    title: str


def split_row(line: str) -> list[str]:
    """Split a Markdown table row on unescaped pipes. ``\\|`` yields a literal pipe."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def plain(cell: str) -> str:
    """Return a cell's value without bold markers or code spans."""
    return re.sub(r"\*\*|`", "", cell).strip()


def _fence_mask(lines: list[str]) -> list[bool]:
    mask: list[bool] = []
    inside = False
    for line in lines:
        if _FENCE.match(line):
            mask.append(True)
            inside = not inside
            continue
        mask.append(inside)
    return mask


def _terminated(line: str) -> bool:
    text = line.rstrip()
    return text.endswith("|") and not text.endswith("\\|")


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def parse_headings(text: str) -> list[Heading]:
    lines = text.splitlines()
    fence = _fence_mask(lines)
    headings: list[Heading] = []
    for idx, line in enumerate(lines):
        if fence[idx]:
            continue
        match = _HEADING.match(line)
        if match:
            headings.append(Heading(idx + 1, len(match.group(1)), match.group(2)))
    return headings


def _separator_error(separator: list[str], header: tuple[str, ...]) -> str | None:
    if len(separator) != len(header):
        return f"separator has {len(separator)} columns but the header has {len(header)}"
    for cell in separator:
        if not _SEPARATOR_CELL.match(cell.replace(" ", "")):
            return f"separator cell {cell!r} is not a valid alignment marker (expected ---, :--, --: or :-:)"
    return None


def parse_tables(text: str) -> list[Table]:
    """Parse every pipe table. A header followed by a malformed separator is still reported.

    Such tables carry ``separator_ok=False`` and ``separator_error`` so validators can
    reject them instead of silently treating the block as prose.
    """

    lines = text.splitlines()
    fence = _fence_mask(lines)
    tables: list[Table] = []
    i = 0
    while i < len(lines):
        if fence[i] or not _is_table_line(lines[i]) or i + 1 >= len(lines) or fence[i + 1] or not _is_table_line(lines[i + 1]):
            i += 1
            continue
        header = tuple(split_row(lines[i]))
        separator = split_row(lines[i + 1])
        error = _separator_error(separator, header)
        rows: list[Row] = []
        j = i + 2
        while j < len(lines) and not fence[j] and _is_table_line(lines[j]):
            rows.append(Row(j + 1, tuple(split_row(lines[j])), _terminated(lines[j])))
            j += 1
        tables.append(
            Table(
                line=i + 1,
                header=header,
                separator_ok=error is None,
                rows=tuple(rows),
                separator_line=i + 2,
                separator_cells=tuple(separator),
                separator_error=error,
                header_terminated=_terminated(lines[i]),
            )
        )
        i = j
    return tables


def heading_before(headings: list[Heading], line: int, max_level: int = 6) -> Heading | None:
    """Return the nearest heading above ``line`` whose level is at most ``max_level``."""
    found: Heading | None = None
    for heading in headings:
        if heading.line >= line:
            break
        if heading.level <= max_level:
            found = heading
    return found
