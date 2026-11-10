"""Small helpers shared by the extractors."""

from __future__ import annotations

import re
from typing import Any

HTTP_VERBS = ("get", "post", "put", "delete", "patch", "options", "head")
_STRING_PARTS = ("string_content", "string_fragment")


def text(node: Any) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def line(node: Any) -> int:
    return int(node.start_point[0]) + 1


def end_line(node: Any) -> int:
    return int(node.end_point[0]) + 1


def squash(value: str, limit: int = 300) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def string_value(node: Any) -> str | None:
    """The literal content of a string node, or None when ``node`` is not a plain string."""
    if node is None or node.type not in ("string", "string_literal", "template_string"):
        return None
    return "".join(text(child) for child in node.children if child.type in _STRING_PARTS)


def join_path(base: str, sub: str) -> str:
    parts = [segment.strip("/") for segment in (base, sub) if segment and segment.strip("/")]
    return "/" + "/".join(parts)


def dotted(*parts: str) -> str:
    return ".".join(part for part in parts if part)
