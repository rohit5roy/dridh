"""Import LEDGER.md into queryable rows and reconcile it with the database (plan 17.3).

``import_ledger`` stores the validated Table 14 entries. ``reconcile`` compares file, rows and the rest of the state and reports
findings; with ``repair=True`` it only ever re-imports the rows from the file. It never edits Markdown, never records an approval
and never touches a Use Case table: the original approved requirement can only change through its own reviewed flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.ledger_store import load_ledger_decisions
from dridh.document_contracts.markdown import parse_headings, parse_tables, plain
from dridh.document_contracts.validator import (
    H_LEDGER,
    LEDGER_APPROVAL_REQUIRED,
    STANDARDS_VERSION,
    ledger_entry_revision,
    validate_ledger,
)
from dridh.state.documents import DocumentRepository, content_hash
from dridh.state.ledger_entries import LedgerEntryRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo
from dridh.workspace.paths import ensure_within_root

# Approval findings describe a missing human decision; they are reported by reconcile, not a reason to refuse an import.
APPROVAL_CODES = {"APPROVAL_REQUIRED", "LEDGER_APPROVAL_MISSING", "LEDGER_APPROVAL_STALE", "LEDGER_APPROVER_MISMATCH",
                  "LEDGER_REJECTED", "LEDGER_APPROVAL_NOT_VERIFIED"}
REPAIRABLE = {"LEDGER_DRIFT", "LEDGER_ROWS_TAMPERED"}
MAX_BYTES = 2_000_000


def parse_entries(content: str) -> list[dict[str, Any]]:
    """The Table 14 rows of a ledger document, with the title from each ``#### LDG-NN: Title`` heading."""
    import re

    titles = {m.group(1): m.group(2).strip() for h in parse_headings(content) if h.level == 4 and (m := re.match(r"(LDG-\d{2}):\s+(\S.*)", h.title))}
    entries: list[dict[str, Any]] = []
    for table in parse_tables(content):
        if table.header != H_LEDGER:
            continue
        for row in table.rows:
            cells = [plain(c) for c in row.cells]
            if len(cells) != len(H_LEDGER):
                continue
            entries.append({
                "ledger_id": cells[0], "title": titles.get(cells[0], ""), "origin_id": cells[1], "deviation_status": cells[2],
                "linked_story_id": None if cells[3] in ("—", "-", "") else cells[3], "original_requirement": cells[4],
                "actual_behavior": cells[5], "justification": cells[6], "approved_by": cells[7],
                "entry_sha256": ledger_entry_revision(content, cells[0]) or "",
            })
    return entries


def _finding(code: str, message: str, entity: str | None = None, severity: str = "ERROR") -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "entity": entity}


class LedgerService:
    def __init__(self, workspace: WorkspaceInfo) -> None:
        self.workspace = workspace

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    def _read(self, path: str) -> tuple[str, str, Path]:
        target = Path(ensure_within_root(path, self.workspace.project_root))
        if not target.is_file() or target.is_symlink() or target.stat().st_size > MAX_BYTES:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The ledger must be a regular file inside the project, at most 2 MB.", details=(path,))
        relative = target.relative_to(Path(self.workspace.project_root).resolve()).as_posix() if target.is_absolute() else path
        return target.read_text(encoding="utf-8"), relative, target

    def _use_case_revision(self, connection: Any) -> str | None:
        row = connection.execute("SELECT source_revision_id FROM use_case WHERE workspace_id=? ORDER BY rowid DESC LIMIT 1", (self.workspace.workspace_id,)).fetchone()
        return row["source_revision_id"] if row else None

    # ---------------------------------------------------------------- import

    def import_ledger(self, path: str) -> dict[str, Any]:
        content, relative, target = self._read(path)
        with self._store() as store:
            decisions = dict(load_ledger_decisions(store.read()))
        result = validate_ledger(content, file=target.name, decisions=decisions)
        blocking = [f for f in result.findings if f.severity == "ERROR" and f.code not in APPROVAL_CODES]
        if blocking:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The ledger does not pass structural validation; nothing was imported.",
                             details=tuple(f.render() for f in blocking[:10]))
        entries = parse_entries(content)
        with self._store() as store, store.transaction() as connection:
            documents = DocumentRepository(connection, self.workspace.workspace_id)
            revision, _created = documents.record_revision(relative_path=relative, document_type="ledger", content=content,
                                                           standards_version=STANDARDS_VERSION, source_path=str(target))
            metadata = documents.document_metadata(relative)
            count = LedgerEntryRepository(connection, self.workspace.workspace_id).replace(
                document_id=str(metadata["id"]), revision_id=revision.id, document_sha256=content_hash(content),
                use_case_revision_id=self._use_case_revision(connection), entries=entries)
        awaiting = [e["ledger_id"] for e in entries if e["deviation_status"] in LEDGER_APPROVAL_REQUIRED]
        return {"imported": count, "document": relative, "revision_id": revision.id, "sha256": content_hash(content),
                "awaiting_human_decision": [e for e in awaiting if any(f.entity_id == e and f.code in APPROVAL_CODES for f in result.findings)]}

    # ------------------------------------------------------------- reconcile

    def reconcile(self, path: str, *, repair: bool = False) -> dict[str, Any]:
        report = self._reconcile_once(path)
        repaired = False
        if repair and report["findings"] and {f["code"] for f in report["findings"]} & REPAIRABLE:
            try:
                self.import_ledger(path)  # rows come from the file; the file itself is never edited
                repaired = True
            except DridhError as exc:
                report["repair_refused"] = exc.message
            if repaired:
                report = self._reconcile_once(path)
        report["repaired"] = repaired
        return report

    def _reconcile_once(self, path: str) -> dict[str, Any]:
        content, relative, target = self._read(path)
        findings: list[dict[str, Any]] = []
        skipped: list[str] = []
        with self._store() as store:
            connection = store.read()
            repo = LedgerEntryRepository(connection, self.workspace.workspace_id)
            marker, rows = repo.marker(), repo.entries()
            decisions = dict(load_ledger_decisions(connection))
            documents = DocumentRepository(connection, self.workspace.workspace_id)
            if marker is None:
                findings.append(_finding("LEDGER_NOT_IMPORTED", "The ledger has never been imported; run `dridh ledger import`."))
            else:
                if marker["document_sha256"] != content_hash(content):
                    findings.append(_finding("LEDGER_DRIFT", "LEDGER.md changed after it was imported; the rows are out of date."))
                stored = parse_entries(documents.stored_content(marker["source_revision_id"]))
                keys = ("ledger_id", "title", "origin_id", "deviation_status", "linked_story_id", "original_requirement",
                        "actual_behavior", "justification", "approved_by", "entry_sha256")
                if [{k: e[k] for k in keys} for e in stored] != [{k: r[k] for k in keys} for r in rows]:
                    findings.append(_finding("LEDGER_ROWS_TAMPERED", "The stored ledger rows differ from the imported revision of the document."))
                current_uc = self._use_case_revision(connection)
                if marker["use_case_revision_id"] and current_uc and current_uc != marker["use_case_revision_id"]:
                    findings.append(_finding("LEDGER_USE_CASE_CHANGED", "The approved Use Cases changed after this ledger was imported; re-check each Original Requirement.",
                                             severity="WARNING"))
            file_entries = parse_entries(content)
            for finding in validate_ledger(content, file=target.name, decisions=decisions).findings:
                if finding.severity == "ERROR":
                    findings.append(_finding(finding.code, finding.message, finding.entity_id))
            use_cases = {r[0] for r in connection.execute("SELECT DISTINCT use_case_id FROM use_case WHERE workspace_id=?", (self.workspace.workspace_id,))}
            flows = {r[0]: r[1] for r in connection.execute("SELECT DISTINCT flow_id, parent_use_case_id FROM use_case_flow WHERE workspace_id=?", (self.workspace.workspace_id,))}
            stories = {r["story_id"]: r["status"] for r in connection.execute("SELECT story_id, status FROM story WHERE workspace_id=?", (self.workspace.workspace_id,))}
            whole_use_cases = {r[0] for r in connection.execute("SELECT DISTINCT use_case_id FROM story_use_case WHERE workspace_id=? AND flow_id IS NULL", (self.workspace.workspace_id,))}
            covered_flows = {(r[0], r[1]) for r in connection.execute("SELECT use_case_id, flow_id FROM story_use_case WHERE workspace_id=? AND flow_id IS NOT NULL", (self.workspace.workspace_id,))}
        if use_cases:
            for entry in file_entries:
                if entry["origin_id"] not in use_cases and entry["origin_id"] not in flows:
                    findings.append(_finding("LEDGER_UNKNOWN_REFERENCE", f"{entry['origin_id']} is not an approved Use Case or flow.", entry["ledger_id"]))
        else:
            skipped.append("use-case references: no Use Cases are imported")
        if stories:
            for entry in file_entries:
                story = entry["linked_story_id"]
                if story is None:
                    continue
                if story not in stories:
                    findings.append(_finding("LEDGER_UNKNOWN_REFERENCE", f"{story} is not a planned story.", entry["ledger_id"]))
                elif entry["deviation_status"] in ("NOT_DONE", "WONT_DO") and stories[story] == "COMPLETED":
                    findings.append(_finding("LEDGER_STATUS_MISMATCH", f"{entry['ledger_id']} says {entry['deviation_status']} but {story} is COMPLETED.", entry["ledger_id"]))
                elif entry["deviation_status"] == "DONE" and stories[story] in ("BLOCKED", "NOT_STARTED"):
                    findings.append(_finding("LEDGER_STATUS_MISMATCH", f"{entry['ledger_id']} says DONE but {story} is {stories[story]}.", entry["ledger_id"], severity="WARNING"))
        else:
            skipped.append("story references and coverage: no stories are imported")
        if stories and use_cases:
            # An approved flow needs a story that implements it (or its whole Use Case), or a ledger entry explaining why not.
            origins = {e["origin_id"] for e in file_entries}
            for flow_id, parent in sorted(flows.items()):
                implemented = (parent, flow_id) in covered_flows or parent in whole_use_cases
                explained = flow_id in origins or parent in origins
                if not implemented and not explained:
                    findings.append(_finding("LEDGER_DEVIATION_MISSING", f"Approved flow {flow_id} ({parent}) has no story and no ledger entry.", flow_id))
        errors = [f for f in findings if f["severity"] == "ERROR"]
        return {"ok": not errors, "document": relative, "entries": len(file_entries), "errors": len(errors),
                "warnings": len(findings) - len(errors), "findings": findings[:100], "skipped": skipped}
