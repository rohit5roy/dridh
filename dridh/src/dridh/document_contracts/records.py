"""Records a technical design names must have their fields defined: deterministic checks, no model involved.

The Executor builds from its story card, which carries the approved API references verbatim. A reference such as
``place_hold(member_id, item_id) -> Hold`` is unusable when the design never says what a ``Hold`` holds, and a data
model that says "an orders table with one row per order" leaves every column to guesswork. So:

- the Data Model section is ``N/A`` or ``OPEN`` with a reason, or it defines fields: an ``erDiagram`` entity with
  attributes, a table, a schema code block, or a list of ``field: type`` items;
- every entity of an ``erDiagram`` lists its attributes (``ORDER { string id PK }``);
- every record type an API reference in Table 13 takes or returns (a capitalised name such as ``Hold`` or
  ``list[Hold]``) is defined somewhere in the design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from dridh.document_contracts.markdown import Heading, Table

# Capitalised names that are language or library types, not records the design has to define.
KNOWN_TYPES = frozenset({
    "Any", "Array", "BigDecimal", "BigInteger", "Bool", "Boolean", "Buffer", "Byte", "Bytes", "Callable", "Char",
    "Collection", "Date", "DateTime", "Datetime", "Decimal", "Dict", "Double", "Duration", "Enum", "Error", "Exception",
    "False", "Float", "FrozenSet", "Future", "Generator", "Instant", "Int", "Integer", "Iterable", "Iterator", "JSON",
    "Json", "List", "Literal", "LocalDate", "LocalDateTime", "LocalTime", "Long", "Map", "Mapping", "Never", "None",
    "NoReturn", "Null", "Number", "Object", "Observable", "OffsetDateTime", "Optional", "Path", "Promise", "Result",
    "Self", "Sequence", "Set", "Short", "Stream", "Str", "String", "Task", "Time", "Timedelta", "Timestamp", "True",
    "Tuple", "Type", "UUID", "Uuid", "Union", "Unit", "URI", "URL", "Uri", "Url", "Void", "ZonedDateTime",
})

_ER_RELATION = re.compile(r"^\s*([A-Za-z_][\w-]*)\s+[|}o{.\-]+\s+([A-Za-z_][\w-]*)\s*(?::|$)")
_ER_BLOCK = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*(?:\[[^\]]*\])?\s*\{\s*$")
_TYPE_NAME = re.compile(r"\b[A-Z][A-Za-z0-9]*\b")
_FIELD_ITEM = re.compile(r"^\s*[-*+]\s+`?[A-Za-z_][\w.]*`?\s*(?::|\(|—|–| - )")
_DEFINES = r"(?:class|interface|record|struct|type|message|enum|data\s+class|table|create\s+table(?:\s+if\s+not\s+exists)?)"


def _key(name: str) -> str:
    """``order_line``, ``OrderLine`` and ``ORDER-LINE`` name the same record."""
    return re.sub(r"[\s_\-]", "", name).lower()


@dataclass
class ErModel:
    """The entities of every ``erDiagram`` in the document, and which of them list attributes."""

    named: dict[str, int] = field(default_factory=dict)  # entity -> line first named
    with_fields: set[str] = field(default_factory=set)


def er_model(lines: list[str]) -> ErModel:
    model = ErModel()
    fence, er, block, attributes, block_line = None, False, None, 0, 0
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if fence is None:
            if stripped.startswith(("```", "~~~")):
                fence, er = stripped[:3], False
            continue
        if stripped.startswith(fence):
            fence, block = None, None
            continue
        if stripped == "erDiagram":
            er = True
            continue
        if not er or not stripped or stripped.startswith("%%"):
            continue
        if block is not None:
            if stripped.startswith("}"):
                if attributes:
                    model.with_fields.add(_key(block))
                block = None
            else:
                attributes += 1
            continue
        if match := _ER_BLOCK.match(stripped):
            block, attributes, block_line = match.group(1), 0, number
            model.named.setdefault(block, block_line)
            continue
        if match := _ER_RELATION.match(stripped):
            for name in match.groups():
                model.named.setdefault(name, number)
    return model


def _section(lines: list[str], headings: list[Heading], title: str) -> tuple[int, int] | None:
    """(first line, end line exclusive) of a level-2 section's body, 1-based."""
    level2 = [h for h in headings if h.level == 2]
    for index, heading in enumerate(level2):
        if heading.title == title:
            end = level2[index + 1].line if index + 1 < len(level2) else len(lines) + 1
            return heading.line + 1, end
    return None


def _fences(lines: list[str], start: int, end: int) -> list[tuple[str, list[str]]]:
    """(info string, body lines) of every fenced block that opens between ``start`` and ``end``."""
    out, index = [], start
    while index < end:
        stripped = lines[index - 1].strip()
        if stripped.startswith(("```", "~~~")):
            marker, info, body = stripped[:3], stripped[3:].strip().lower(), []
            index += 1
            while index <= len(lines) and not lines[index - 1].strip().startswith(marker):
                body.append(lines[index - 1])
                index += 1
            out.append((info, body))
        index += 1
    return out


def _defines_fields(lines: list[str], start: int, end: int, tables: list[Table], er: ErModel) -> bool:
    """Whether lines ``start..end`` define at least one field."""
    if any(start <= t.line < end and t.rows for t in tables):
        return True
    for info, body in _fences(lines, start, end):
        text = "\n".join(body)
        if info.startswith("mermaid"):
            if "erDiagram" in text and er_model(["```", *body, "```"]).with_fields:
                return True
            continue
        if text.strip():
            return True  # a schema: SQL, a class, a JSON or protobuf definition
    return any(_FIELD_ITEM.match(line) for line in lines[start - 1:end - 1])


def _record_types(reference: str) -> list[str]:
    """Capitalised type names a callable reference takes or returns: ``f(a: Item) -> list[Hold]`` -> Item, Hold."""
    from dridh.planner.contract import calls

    found: list[str] = []
    for call in calls(reference):
        start = reference.find(call.text)
        inside = call.text[call.text.index("(") + 1:-1]
        annotations = " ".join(part.split(":", 1)[1].split("=", 1)[0] for part in re.split(r",(?![^\[\(<{]*[\]\)>}])", inside) if ":" in part)
        tail = reference[start + len(call.text):]
        returned = tail.split("->", 1)[1].split(";", 1)[0] if tail.lstrip().startswith("->") else ""
        java_style = " ".join(part.split()[0] for part in inside.split(",") if len(part.split()) == 2 and ":" not in part)
        for name in _TYPE_NAME.findall(f"{annotations} {returned} {java_style}"):
            if name not in KNOWN_TYPES and name not in found:
                found.append(name)
    return found


def _defined_elsewhere(name: str, lines: list[str], headings: list[Heading], tables: list[Table], er: ErModel) -> bool:
    key = _key(name)
    if key in er.with_fields:
        return True
    text = "\n".join(lines)
    if re.search(rf"(?im)^\s*(?:export\s+|public\s+|@\w+\s+)*{_DEFINES}\s+[`\"]?{re.escape(name)}\b", text):
        return True
    if re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*(?:TypedDict|NamedTuple)\b", text):
        return True
    for table in tables:  # a table with a Record/Entity/Type column that has a row for it
        columns = [i for i, h in enumerate(table.header) if _key(h) in {"record", "entity", "type", "model", "table", "dto"}]
        if columns and any(_key(row.cells[i].strip("` *")) == key for row in table.rows for i in columns if i < len(row.cells)):
            return True
    for index, heading in enumerate(headings):  # a heading naming it, with fields under it
        if heading.level >= 3 and re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", heading.title.replace("`", "")):
            end = next((h.line for h in headings[index + 1:] if h.level <= heading.level), len(lines) + 1)
            if _defines_fields(lines, heading.line + 1, end, tables, er):
                return True
    return False


def record_findings(lines: list[str], headings: list[Heading], tables: list[Table], api_cells: list[tuple[int, str]]) -> list[dict]:
    """``[{code, message, line, entity, hint}]`` for each record named without its fields."""
    findings: list[dict] = []
    er = er_model(lines)
    for name, line in er.named.items():
        if _key(name) not in er.with_fields:
            findings.append({"code": "TDD_RECORD_WITHOUT_FIELDS", "line": line, "entity": name,
                             "message": f"erDiagram entity {name} lists no attributes",
                             "hint": f"Add its fields, for example {name} {{ string id PK }}"})
    section = _section(lines, headings, "Data Model")
    if section is not None:
        start, end = section
        body = [line.strip() for line in lines[start - 1:end - 1] if line.strip() and not line.lstrip().startswith("#")]
        first = re.sub(r"^[*_>\s]+", "", body[0]) if body else ""
        if body and not re.match(r"(?i)(N/?A|OPEN)\b", first) and not _defines_fields(lines, start, end, tables, er):
            findings.append({"code": "TDD_DATA_MODEL_NO_FIELDS", "line": start - 1, "entity": "Data Model",
                             "message": "The Data Model names records but defines no fields",
                             "hint": "List each record's fields (an erDiagram with attributes, a table or a schema), "
                                     "or write N/A or OPEN with the reason"})
    seen: set[str] = set()
    for line, cell in api_cells:
        for reference in cell.split(";"):
            for name in _record_types(reference):
                if name in seen:
                    continue
                seen.add(name)
                if not _defined_elsewhere(name, lines, headings, tables, er):
                    findings.append({"code": "TDD_RECORD_WITHOUT_FIELDS", "line": line, "entity": name,
                                     "message": f"Table 13 names the record {name} but the design never defines its fields",
                                     "hint": f"Define {name}'s fields in the Data Model or API Design (a table, an erDiagram "
                                             "entity with attributes, or a schema)"})
    return findings
