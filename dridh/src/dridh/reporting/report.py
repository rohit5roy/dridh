"""Build the completion report from persisted state only."""

from __future__ import annotations

from typing import Any

from dridh.common.redaction import redact

REPORT_SCHEMA = "dridh.report"
REPORT_SCHEMA_VERSION = 1


def build_report(
    snapshot: dict[str, Any],
    evidence: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    ledger: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return the Markdown report and the machine-readable document for the same facts."""

    document: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "schema_version": REPORT_SCHEMA_VERSION,
        "workflow_id": snapshot["workflow_id"],
        "workspace_id": snapshot["workspace_id"],
        "objective": snapshot["objective"],
        "state": snapshot["state"],
        "plan": {
            "sha256": snapshot["plan"]["sha256"],
            "revision": snapshot["plan"]["revision"],
            "tasks": snapshot["plan"]["tasks"],
        },
        "approvals": snapshot["approvals"],
        "acceptance": evidence,
        "worker_runs": runs,
        "failure": snapshot["failure"],
        "ledger": {"events": ledger["events"], "verified": ledger["ok"], "head": ledger["head"]},
    }
    document = redact(document)

    lines = [
        f"# Dridh workflow report: {snapshot['workflow_id']}",
        "",
        f"- **State:** {snapshot['state']}",
        f"- **Objective:** {snapshot['objective']}",
        f"- **Workspace:** {snapshot['workspace_id']}",
        f"- **Plan SHA-256:** {snapshot['plan']['sha256'] or '—'} (revision {snapshot['plan']['revision']})",
        f"- **Ledger:** {ledger['events']} events, verified: {'yes' if ledger['ok'] else 'NO'}, head: {ledger['head'] or '—'}",
        "",
        "## Approvals",
        "",
    ]
    if snapshot["approvals"]:
        lines += ["| Decision | Actor | Method | Plan SHA-256 | At |", "|---|---|---|---|---|"]
        lines += [
            f"| {item['decision']} | {item['actor']} | {item['method']} | {item['plan_sha256']} | {item['created_at']} |"
            for item in snapshot["approvals"]
        ]
    else:
        lines.append("No approval has been recorded.")
    lines += ["", "## Tasks", "", "| Task | Stage | Agent | State | Attempts | Failure |", "|---|---|---|---|---|---|"]
    for task in snapshot["plan"]["tasks"]:
        lines.append(
            f"| {task['id']} | {task['stage_id']} | {task['agent']} | {task['state']} | {task['attempts']}/{task['max_attempts']} | {task['failure_code'] or '—'} |"
        )
    lines += ["", "## Acceptance evidence", ""]
    if evidence:
        lines += ["| Task | # | Check | Result | Detail |", "|---|---|---|---|---|"]
        lines += [
            f"| {item['task_id'] or 'workflow'} | {item['check_index']} | {item['check_type']} | {'PASS' if item['passed'] else 'FAIL'} | {str(redact(item['detail']))} |"
            for item in evidence
        ]
    else:
        lines.append("No acceptance check has run.")
    lines += ["", "## Worker runs", ""]
    if runs:
        lines += ["| Task | Purpose | Agent | Session | Status | Exit | Seconds | Error |", "|---|---|---|---|---|---|---|---|"]
        lines += [
            f"| {item['task_id'] or '—'} | {item['purpose']} | {item['agent']} | {item['session_id'] or '—'} | {item['status']} | {item['exit_code'] if item['exit_code'] is not None else '—'} | {round(item['duration_seconds'], 2) if item['duration_seconds'] is not None else '—'} | {item['error_code'] or '—'} |"
            for item in runs
        ]
    else:
        lines.append("No worker has run.")
    if snapshot["failure"]:
        lines += ["", "## Failure", "", f"- **Code:** {snapshot['failure']['code']}", f"- **Message:** {redact(snapshot['failure']['message'])}"]
    lines += ["", "_Generated from persisted state. Worker replies are not proof; acceptance evidence is._", ""]
    return "\n".join(lines), document
