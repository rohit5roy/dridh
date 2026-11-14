"""The approved contract on story cards, and file ownership entries: deterministic checks, no model involved.

Python owns the facts. The approved TDD's API and event references (Table 13) are attached to each story the Executor
receives, copied verbatim, so the Executor never has to read the TDD and never builds from a card that restated a
signature. A story card that restates an approved signature with different parameters is rejected, and so is a file
ownership entry that is not a plain path or glob.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# ------------------------------------------------------------------ the approved contract of a story


def reference_text(kind: str, name: str) -> str:
    """The reference as the TDD cell wrote it. The importer keeps an API reference whole and drops an event's
    ``Event:`` prefix, so the prefix is put back."""
    return f"Event: {name}" if kind == "EVENT" else name


def story_contract(story: dict[str, Any], api_events: Iterable[Any]) -> list[str]:
    """The approved API/event references for one story, verbatim and in document order.

    A reference belongs to the story when its flow is one of the story's flows, or, when it names no flow, when its use
    case is one of the story's use cases. References of the story's own component are used when there are any;
    otherwise all of the story's references are (a story may call an API another component provides).
    Infrastructure stories have no use cases and get none."""
    flows, use_cases = set(story.get("flow_ids") or ()), set(story.get("use_case_ids") or ())
    if story.get("infrastructure") or not (flows or use_cases):
        return []
    events = sorted(api_events, key=lambda e: e.position)
    mine = [e for e in events if (e.flow_id in flows) if e.flow_id] + [e for e in events if not e.flow_id and e.use_case_id in use_cases]
    mine.sort(key=lambda e: e.position)
    own = [e for e in mine if e.component_id == story.get("component_id")]
    chosen: list[str] = []
    for event in own or mine:
        text = reference_text(event.reference_kind, event.reference_name)
        if text not in chosen:
            chosen.append(text)
    return chosen


# ------------------------------------------------------------------ signatures

_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")
_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_RECEIVERS = {"self", "cls", "this"}
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class Call:
    name: str  # the last segment: ``library.place_hold`` -> ``place_hold``
    text: str  # as written, from the name to the closing parenthesis
    params: tuple[str, ...] | None  # parameter names in order; None when the mention passes values, not names


def _split(arguments: str) -> list[str]:
    """Split on commas that are not nested in brackets (``dict[str, int]`` is one part)."""
    parts, depth, current = [], 0, []
    for char in arguments:
        if char in "([{<":
            depth += 1
        elif char in ")]}>" and depth:
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _param_name(part: str) -> str | None:
    """The parameter name of one argument, ignoring annotations and defaults; None when it is a value, not a name.

    ``item_id: str = "x"`` -> ``item_id``; ``String itemId`` -> ``itemId``; ``*args`` -> ``*args``; ``"B1"`` -> None."""
    head = re.split(r"[:=]", part, maxsplit=1)[0].strip()
    stars = re.match(r"\*{0,2}", head).group(0)
    words = head[len(stars):].split()
    if not words:
        return None
    name = words[-1]  # "String itemId" (Java, C#, TypeScript without annotations): the name comes last
    return stars + name if _IDENTIFIER.fullmatch(name) else None


def calls(text: str) -> list[Call]:
    """Every ``name(...)`` in ``text`` with balanced parentheses; a bare ``name`` without parentheses is not a call."""
    found: list[Call] = []
    resume = 0  # calls nested in another call's arguments (a default such as date.today()) are not signatures
    for match in _NAME.finditer(text):
        end = match.end()
        if match.start() < resume or end >= len(text) or text[end] != "(" or (match.start() and (text[match.start() - 1].isalnum() or text[match.start() - 1] == "_")):
            continue
        depth, index = 0, end
        while index < len(text):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if depth:
            continue  # unbalanced: not a call we can read
        arguments = text[end + 1:index]
        names = [_param_name(part) for part in _split(arguments) if part not in ("*", "/")]  # Python's marker arguments
        params = None if any(name is None for name in names) else tuple(n for n in names if n not in _RECEIVERS)  # type: ignore[misc]
        found.append(Call(match.group(0).rsplit(".", 1)[-1], text[match.start():index + 1], params))
        resume = index + 1
    return found


def approved_signatures(contract: Iterable[str]) -> dict[str, list[Call]]:
    """``name -> [approved Call]`` for every reference with a callable form whose parameters are names. The text kept is
    the whole reference (``renew(item_id, today) -> date``), which is what an error shows as the approved signature."""
    signatures: dict[str, list[Call]] = {}
    for reference in contract:
        found = [call for call in calls(reference) if call.params is not None]
        for call in found:
            text = reference.strip() if len(found) == 1 else call.text
            signatures.setdefault(call.name, []).append(Call(call.name, text, call.params))
    return signatures


STORY_TEXT_FIELDS = ("title", "description", "todos", "acceptance_criteria", "definition_of_done")


def contract_conflicts(story: dict[str, Any], signatures: dict[str, list[Call]]) -> list[tuple[str, str]]:
    """``(found, approved)`` for each mention in the card of an approved name with a different parameter list.

    Parameter names are compared in order, ignoring whitespace, annotations, defaults and return types. A mention that
    passes values (``renew("B1", today)``) is an example call, not a restated signature, and is not compared."""
    conflicts: list[tuple[str, str]] = []
    for field in STORY_TEXT_FIELDS:
        value = story.get(field)
        for text in ([value] if isinstance(value, str) else value or []):
            for call in calls(text):
                approved = signatures.get(call.name)
                if not approved or call.params is None:
                    continue
                if all(call.params != item.params for item in approved):
                    found = (call.text, " or ".join(item.text for item in approved))
                    if found not in conflicts:
                        conflicts.append(found)
    return conflicts


# ------------------------------------------------------------------ file ownership

# A plain project-relative path or glob: letters, digits and . _ - / plus * ? for globs and [ ] @ + that real file
# names use (Next.js routes such as app/[id]/page.tsx). Whitespace, parentheses, commas and everything else are refused.
OWNERSHIP_ENTRY = re.compile(r"[A-Za-z0-9._@+\-\[\]*?/]+")


def ownership_problem(entry: str) -> str | None:
    """Why ``entry`` is not a usable file ownership entry, or None."""
    if entry.startswith("/") or re.match(r"^[A-Za-z]:", entry) or ".." in entry.split("/"):
        return "it must be a safe project-relative path: no leading /, drive letter or .."
    if not OWNERSHIP_ENTRY.fullmatch(entry):
        return ("it must be a plain path or glob (letters, digits and . _ - / * ? [ ] @ + only) with no spaces, "
                "parentheses, commas or notes")
    return None
