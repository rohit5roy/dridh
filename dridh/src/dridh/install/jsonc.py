"""JSON-with-comments reader for OpenCode configuration files.

OpenCode accepts ``opencode.json`` and ``opencode.jsonc``. The reader accepts
``//`` and ``/* */`` comments and trailing commas, rejects duplicate object keys
(which JSON silently resolves to the last value), and reports whether comments
were present so the caller can decide whether a rewrite would lose them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from dridh.common.errors import DridhError, ErrorCode


@dataclass(frozen=True)
class JsoncDocument:
    value: Any
    had_comments: bool
    had_trailing_commas: bool


def _strip_comments(text: str) -> tuple[str, bool]:
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    had_comments = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            had_comments = True
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            had_comments = True
            end = text.find("*/", index + 2)
            if end == -1:
                raise DridhError(
                    ErrorCode.CONFIGURATION_ERROR,
                    "OpenCode configuration has an unterminated block comment.",
                )
            out.append("\n" * text.count("\n", index, end))
            index = end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out), had_comments


def _strip_trailing_commas(text: str) -> tuple[str, bool]:
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    removed = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            probe = index + 1
            while probe < length and text[probe] in " \t\r\n":
                probe += 1
            if probe < length and text[probe] in "}]":
                removed = True
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out), removed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def load_jsonc(text: str, *, source: str = "OpenCode configuration") -> JsoncDocument:
    """Parse ``text`` as JSON or JSONC. Raises ``CONFIGURATION_ERROR`` when invalid."""

    without_comments, had_comments = _strip_comments(text)
    cleaned, had_trailing = _strip_trailing_commas(without_comments)
    try:
        value = json.loads(cleaned, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise DridhError(
            ErrorCode.CONFIGURATION_ERROR,
            f"{source} is not valid JSON or JSONC.",
            details=(f"line {exc.lineno}, column {exc.colno}: {exc.msg}",),
        ) from None
    except ValueError as exc:
        raise DridhError(
            ErrorCode.CONFIGURATION_ERROR,
            f"{source} is invalid.",
            details=(str(exc),),
        ) from None
    return JsoncDocument(value=value, had_comments=had_comments, had_trailing_commas=had_trailing)
