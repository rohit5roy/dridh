"""Draft deviation entries, written to LEDGER.md by a script (never by a model) and imported at once.

A draft records that an approved flow was built differently (``DEVIATED``) or not at all (``NOT_DONE``). The original
requirement is copied from the approved Use Case rows, never from an agent. A draft carries no approver: it waits for a
human to run ``dridh ledger approve`` (or ``reject``). Writing the same draft twice changes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.markdown import parse_tables, plain
from dridh.document_contracts.validator import H_LEDGER, STANDARDS_VERSION
from dridh.ledger.flows import approved_flows
from dridh.state.store import StateStore, utc_now
from dridh.workspace.layout import WorkspaceInfo

LEDGER_PATH = ".opencode/dridh/documents/LEDGER.md"
DRAFT_STATUSES = ("DEVIATED", "NOT_DONE")


def ledger_file(workspace: WorkspaceInfo) -> Path:
    return Path(workspace.project_root) / LEDGER_PATH


def _cell(value: str, limit: int = 300) -> str:
    """One table cell: a single line, no pipes."""
    text = re.sub(r"\s+", " ", value.replace("|", "/")).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else (text or "—")


def _skeleton() -> str:
    header = "| " + " | ".join(H_LEDGER) + " |\n|" + "---|" * len(H_LEDGER) + "\n"
    return (f"# Coordinator Ledger\n\n> **Standards Version:** {STANDARDS_VERSION}  \n> **Document Status:** DRAFT  \n"
            f"> **Last Updated:** {utc_now()[:10]}\n\n"
            "Deviations from the approved Use Cases. Dridh writes DEVIATED and NOT_DONE entries as drafts; each needs a human "
            "decision: `dridh ledger approve LDG-NN --reviewer <name>` or `dridh ledger reject`.\n\n" + header)


def _existing(content: str) -> list[list[str]]:
    return [[plain(c) for c in row.cells] for table in parse_tables(content) if table.header == H_LEDGER for row in table.rows]


def add_drafts(workspace: WorkspaceInfo, drafts: list[dict[str, Any]]) -> dict[str, Any]:
    """Append ``drafts`` (``flow``, ``status``, ``story``, ``actual``, ``reason``) to LEDGER.md and import it.

    An entry for the same flow and story is never written twice, whatever its status. Returns the new ledger IDs."""
    for draft in drafts:
        if draft["status"] not in DRAFT_STATUSES:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"A draft ledger entry must be one of {', '.join(DRAFT_STATUSES)}.")
    if not drafts:
        return {"written": []}
    path = ledger_file(workspace)
    content = path.read_text(encoding="utf-8") if path.is_file() else _skeleton()
    rows = _existing(content)
    if not any(True for _ in parse_tables(content) if _.header == H_LEDGER):
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{LEDGER_PATH} has no Table 14; fix it before Dridh can add entries.")
    taken = {(row[1], row[3]) for row in rows if len(row) >= 4}
    numbers = [int(row[0][4:]) for row in rows if re.fullmatch(r"LDG-\d{2}", row[0] if row else "")]
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        flows = approved_flows(store.read(), workspace.workspace_id)
    new_rows, sections, written = [], [], []
    for draft in drafts:
        key = (draft["flow"], draft["story"])
        if key in taken:
            continue
        taken.add(key)
        number = max(numbers, default=0) + 1
        if number > 99:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The ledger is full (LDG-99); split the feature's ledger before adding entries.")
        numbers.append(number)
        ledger_id = f"LDG-{number:02d}"
        original = flows.get(draft["flow"], {}).get("text", "(not in the approved Use Cases)")
        verb = "built differently" if draft["status"] == "DEVIATED" else "not built"
        new_rows.append("| " + " | ".join([ledger_id, draft["flow"], draft["status"], draft["story"], _cell(original), _cell(draft["actual"]),
                                           _cell(draft["reason"]), "—"]) + " |")
        sections.append(f"#### {ledger_id}: {draft['flow']} {verb} in {draft['story']}\n\n"
                        f"- **Originating UC/flow:** {draft['flow']} ({flows.get(draft['flow'], {}).get('use_case', '—')}).\n"
                        f"- **Disposition:** {draft['status']}, recorded by Dridh from {draft.get('source', 'the delivery evidence')}; "
                        "awaiting a human decision.\n")
        written.append(ledger_id)
    if not written:
        return {"written": []}
    lines = content.splitlines()
    last = max(i for i, line in enumerate(lines) if line.startswith("|"))  # Table 14 is the ledger's only table
    lines[last + 1:last + 1] = new_rows
    updated = "\n".join(lines).rstrip("\n") + "\n\n" + "\n".join(sections)
    updated = re.sub(r"(> \*\*Last Updated:\*\* )\S+", lambda m: m.group(1) + utc_now()[:10], updated, count=1)
    from dridh.document_contracts.validator import validate_ledger
    from dridh.ledger.service import APPROVAL_CODES, LedgerService

    broken = [f.render() for f in validate_ledger(updated, file=path.name, decisions={}).findings
              if f.severity == "ERROR" and f.code not in APPROVAL_CODES]
    if broken:  # checked before anything is written: a failed draft never leaves a broken ledger behind
        raise DridhError(ErrorCode.VALIDATION_FAILED, "The draft ledger entries would not pass validation; nothing was written.",
                         details=tuple(broken[:5]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")

    LedgerService(workspace).import_ledger(LEDGER_PATH)
    return {"written": written, "document": LEDGER_PATH}


def drafts_from_report(story_id: str, flows: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Draft entries for the flows an Executor reported CHANGED or NOT_DONE in a story that passed its checks."""
    return [{"flow": flow, "status": "DEVIATED" if item["status"] == "CHANGED" else "NOT_DONE", "story": story_id,
             "actual": item["note"] if item["status"] == "CHANGED" else "Not built in this story.",
             "reason": item["note"] if item["status"] == "NOT_DONE" else "Reported by the Executor; to be confirmed by a human.",
             "source": "the Executor's flow report"}
            for flow, item in sorted(flows.items()) if item["status"] != "IMPLEMENTED"]
