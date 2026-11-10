"""Persisted human decisions for Table 14 ledger entries.

Decisions are appended to the existing hash-chained ``ledger`` audit table as
``ledger.decision`` events. Each event binds the reviewer, the timestamp, the
decision, the exact entry content hash and the document hash. The Markdown
``Approved By`` cell is never treated as evidence on its own.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from dridh.document_contracts.validator import LedgerDecision
from dridh.state.store import append_ledger, utc_now

LEDGER_DECISION_EVENT = "ledger.decision"
DECISIONS = ("approve", "reject")


def record_ledger_decision(
    connection: sqlite3.Connection,
    *,
    ledger_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    entry_sha256: str,
    document_sha256: str,
    actor: str,
) -> int:
    """Append one decision event. Call inside a transaction; never called by agents."""

    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    payload = {
        "ledger_id": ledger_id,
        "decision": decision,
        "reviewer": reviewer,
        "reason": reason,
        "entry_sha256": entry_sha256,
        "document_sha256": document_sha256,
        "decided_at": utc_now(),
    }
    return append_ledger(connection, None, LEDGER_DECISION_EVENT, actor, payload)


def load_ledger_decisions(connection: sqlite3.Connection) -> Mapping[str, LedgerDecision]:
    """Return the latest decision per ledger entry, in audit-chain order."""

    latest: dict[str, LedgerDecision] = {}
    rows = connection.execute(
        "SELECT payload FROM ledger WHERE event_type = ? ORDER BY seq ASC",
        (LEDGER_DECISION_EVENT,),
    ).fetchall()
    for row in rows:
        data = json.loads(row["payload"])
        latest[data["ledger_id"]] = LedgerDecision(
            decision=data["decision"],
            entry_sha256=data["entry_sha256"],
            reviewer=data["reviewer"],
            decided_at=data["decided_at"],
        )
    return latest
