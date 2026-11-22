"""A design that names a record must define its fields."""

from __future__ import annotations

from pathlib import Path

import pytest

from dridh.document_contracts.markdown import parse_headings, parse_tables
from dridh.document_contracts.records import record_findings
from dridh.document_contracts.validator import validate_tdd

FIXTURE = Path(__file__).parents[1] / "fixtures" / "document_contracts" / "feature_tdd.md"


def findings(text: str, api: list[str] = ()) -> list[tuple[str, str]]:
    lines = text.splitlines()
    return [(f["code"], f["entity"]) for f in record_findings(lines, parse_headings(text), parse_tables(text), [(1, a) for a in api])]


def model(body: str) -> str:
    return f"## Data Model\n\n{body}\n\n## Quality Approach\n\nTests.\n"


@pytest.mark.parametrize("body", [
    "N/A — nothing is persisted; state lives in memory for one request.",
    "OPEN — the storage owner has not confirmed the schema.",
    "Orders, one row per order.\n\n| Record | Field | Type |\n|---|---|---|\n| Order | id | string |",
    "```mermaid\nerDiagram\n    ORDER ||--o{ LINE : has\n    ORDER {\n        string id PK\n    }\n    LINE {\n        string order_id FK\n    }\n```",
    "```sql\nCREATE TABLE orders (id TEXT PRIMARY KEY);\n```",
    "Each hold records:\n\n- `member_id`: string\n- `item_id`: string",
])
def test_a_data_model_that_defines_fields_or_says_why_not_passes(body: str) -> None:
    assert findings(model(body)) == []


def test_a_data_model_that_only_names_records_is_rejected() -> None:
    assert findings(model("Orders table with one row per order.")) == [("TDD_DATA_MODEL_NO_FIELDS", "Data Model")]


def test_an_er_entity_without_attributes_is_named_in_the_finding() -> None:
    body = "```mermaid\nerDiagram\n    ORDER ||--o{ LINE : has\n    ORDER {\n        string id PK\n    }\n```"
    assert findings(model(body)) == [("TDD_RECORD_WITHOUT_FIELDS", "LINE")]


def test_a_record_an_api_returns_must_be_defined() -> None:
    text = model("| Record | Field | Type |\n|---|---|---|\n| Member | id | string |")
    api = ["place_hold(member: Member, item_id: str) -> Hold; holds_for(item_id: str) -> list[Hold]"]
    assert findings(text, api) == [("TDD_RECORD_WITHOUT_FIELDS", "Hold")]


@pytest.mark.parametrize("definition", [
    "### Hold\n\n- `member_id`: string\n- `item_id`: string",
    "```python\n@dataclass\nclass Hold:\n    member_id: str\n```",
    "```mermaid\nerDiagram\n    HOLD {\n        string member_id\n    }\n```",
    "| Entity | Field | Type |\n|---|---|---|\n| Hold | member_id | string |",
])
def test_a_record_is_defined_by_a_heading_with_fields_a_class_an_er_entity_or_a_table(definition: str) -> None:
    assert findings(model(definition), ["place_hold(member_id: str) -> Hold"]) == []


def test_language_types_rest_paths_and_events_are_not_records() -> None:
    text = model("N/A — no persistence.")
    api = ["renew(item_id: str, today: date) -> Optional[datetime]", "POST /orders", "Event: order.cancelled",
           "String findTitle(String itemId)", "UUID create(Map<String, Object> fields)"]
    assert findings(text, api) == []


def test_validate_tdd_reports_records_without_fields() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert validate_tdd(text).ok
    start = text.index("## Data Model")
    end = text.index("## ", start + 3)
    broken = text[:start] + "## Data Model\n\nOrders table with one row per order.\n\n" + text[end:]
    result = validate_tdd(broken)
    assert not result.ok
    assert [f.code for f in result.findings if f.severity == "ERROR"] == ["TDD_DATA_MODEL_NO_FIELDS"]


def test_the_designer_sends_a_record_without_fields_back_to_its_worker() -> None:
    from dridh.common.errors import DridhError, ErrorCode
    from dridh.designer.service import DesignerService

    trace = [{"api_event": "place_hold(member_id: str) -> Hold"}]
    with pytest.raises(DridhError) as raised:
        DesignerService._check_records({"data_model": "Holds table, one row per hold."}, trace)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert any(detail.startswith("Hold:") for detail in raised.value.details)
    assert any(detail.startswith("Data Model:") for detail in raised.value.details)
    DesignerService._check_records({"data_model": "| Record | Field | Type |\n|---|---|---|\n| Hold | member_id | string |"}, trace)
