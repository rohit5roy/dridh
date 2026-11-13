"""May a feature leave execution? One deterministic answer, with every blocker named.

Every approved flow must end in one of two ways:

* **proven**: the story that owns it is COMPLETED, its Executor reported the flow IMPLEMENTED, and a passing test named
  for the flow proves it (``executor/flow_coverage.py``);
* **explained**: the ledger has a DEVIATED, NOT_DONE or WONT_DO entry for the flow, with a current human approval.

A BLOCKED story is acceptable only when every flow it owns is explained. The ledger must also reconcile without errors
(``dridh ledger reconcile``): no drift, no unknown references, no missing or stale approvals. No model is involved.
"""

from __future__ import annotations

from typing import Any

from dridh.ledger.drafts import LEDGER_PATH, ledger_file
from dridh.ledger.flows import approved_flows, delivered_flows, story_flows
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo

EXPLAINING = ("DEVIATED", "NOT_DONE", "WONT_DO")


def delivery_readiness(workspace: WorkspaceInfo) -> dict[str, Any]:
    """``{ready, blockers, awaiting_approval, flows}``; ``flows`` maps each approved flow to how it ended."""
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        connection = store.read()
        flows = approved_flows(connection, workspace.workspace_id)
        owners = story_flows(connection, workspace.workspace_id)
        delivered = delivered_flows(connection, workspace.workspace_id)
        stories = {r[0]: r[1] for r in connection.execute("SELECT story_id, status FROM story WHERE workspace_id=?", (workspace.workspace_id,))}
        entries = [dict(r) for r in connection.execute(
            "SELECT ledger_id, origin_id, deviation_status, linked_story_id FROM ledger_entry WHERE workspace_id=? ORDER BY position",
            (workspace.workspace_id,))]
    blockers: list[str] = []
    if not stories:
        return {"ready": False, "blockers": ["No stories are planned."], "awaiting_approval": [], "flows": {}}

    awaiting: list[str] = []
    if ledger_file(workspace).is_file():
        from dridh.ledger.service import LedgerService

        report = LedgerService(workspace).reconcile(LEDGER_PATH)
        for finding in report["findings"]:
            if finding["severity"] != "ERROR":
                continue
            if finding["code"] in ("LEDGER_APPROVAL_MISSING", "APPROVAL_REQUIRED", "LEDGER_APPROVAL_STALE", "LEDGER_APPROVER_MISMATCH"):
                if finding.get("entity") and finding["entity"] not in awaiting:
                    awaiting.append(finding["entity"])
                continue
            blockers.append(f"Ledger: {finding['message']}")
    explained = {e["origin_id"] for e in entries if e["deviation_status"] in EXPLAINING}
    by_flow_story = {(e["origin_id"], e["linked_story_id"]) for e in entries if e["deviation_status"] in EXPLAINING}

    owner_of = {flow: story for story, items in owners.items() for flow in items}
    outcome: dict[str, str] = {}
    for story_id, status in sorted(stories.items()):
        if status not in ("COMPLETED", "BLOCKED"):
            blockers.append(f"{story_id} is {status}; every story must be COMPLETED, or BLOCKED with its flows recorded as NOT_DONE.")
        elif status == "BLOCKED":
            missing = [f for f in owners.get(story_id, []) if f not in explained]
            if missing:
                blockers.append(f"{story_id} is BLOCKED: rework it, or record its flows as not done with "
                                f"`dridh ledger draft --story {story_id} --reason \"...\"` ({', '.join(missing)}).")
    for flow in sorted(flows):
        story = owner_of.get(flow)
        item = delivered.get(story or "", {}).get(flow)
        if flow in explained:
            outcome[flow] = "explained in the ledger"
        elif story is None:
            outcome[flow] = "no story"
            blockers.append(f"{flow} has no story and no ledger entry.")
        elif stories.get(story) != "COMPLETED":
            outcome[flow] = f"{story} is {stories.get(story)}"
        elif item is None:
            outcome[flow] = "no evidence"
            blockers.append(f"{flow} ({story}): the story has no flow report or test proof; re-run the story, or record the flow in the ledger.")
        elif item.get("status") != "IMPLEMENTED":
            outcome[flow] = f"reported {item.get('status')}"
            if (flow, story) not in by_flow_story:
                blockers.append(f"{flow} ({story}) was reported {item.get('status')} but has no ledger entry.")
        elif not item.get("proven"):
            outcome[flow] = "not proven"
            blockers.append(f"{flow} ({story}) is not proven by a passing test named for it; add one, or record the flow in the ledger.")
        else:
            outcome[flow] = f"proven ({item.get('method')})"
    if awaiting:
        blockers.append(f"{len(awaiting)} ledger entr{'y' if len(awaiting) == 1 else 'ies'} await a human decision: "
                        + ", ".join(f"`dridh ledger approve {ldg} --reviewer <name>`" for ldg in awaiting[:5]))
    return {"ready": not blockers, "blockers": blockers, "awaiting_approval": awaiting, "flows": outcome}
