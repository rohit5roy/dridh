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


_DOC_TAG_LINE = ("@", ":param", ":return", ":type", ":rtype", ":raises", ">>>", "...")


def clean_doc(raw: str, limit: int = 1000) -> str:
    """A doc comment as plain prose: comment markers, tag lines (``@param``, ``:return:``), doctest lines, ``{@code x}``
    wrappers and HTML tags removed, whitespace collapsed."""
    lines = []
    for value in raw.strip().removeprefix("/**").removeprefix("/*").removesuffix("*/").splitlines():
        value = value.strip().lstrip("*").strip()
        if value and not value.startswith(_DOC_TAG_LINE):
            lines.append(value)
    prose = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", " ".join(lines))
    return squash(re.sub(r"<[^>]+>", " ", prose), limit)


def doc_comment(node: Any, wrappers: tuple[str, ...] = ()) -> str:
    """The ``/** ... */`` comment written directly above ``node`` (Java, TypeScript, JavaScript), or "".

    Decorators between the comment and the declaration are skipped. When the declaration is wrapped (``export``,
    ``const x = () => ...``), the comment sits above the outermost wrapper, so ``wrappers`` names the node types to
    climb through first."""
    while node.parent is not None and node.parent.type in wrappers and _previous(node) is None:
        node = node.parent
    previous = _previous(node)
    while previous is not None and previous.type == "decorator":
        previous = _previous(previous)
    if previous is not None and previous.type in ("comment", "block_comment") and text(previous).startswith("/**"):
        return text(previous)
    return ""


def _previous(node: Any) -> Any:
    previous = node.prev_sibling
    while previous is not None and not previous.is_named and text(previous) in ("export", "default", ";", ","):
        previous = previous.prev_sibling
    return previous if previous is not None and previous.is_named else None
