"""Document persistence repository (Phase 5 document domain).

A canonical Markdown document is identified within a workspace by its workspace-relative
path. Each distinct content produces one immutable ``document_revision`` with its exact
text, SHA-256 and numbered sections. Validation results are persisted against a specific
revision together with their structured findings.

Writes must run inside an explicit transaction (``StateStore.transaction``) so a revision,
its sections and its validation are either all committed or all absent.

Synchronisation is read-only. ``synchronization_findings`` compares a source file with the
latest persisted revision and reports every inconsistency; it never repairs either side.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.findings import Finding, ValidationResult
from dridh.document_contracts.markdown import parse_headings
from dridh.document_contracts.validator import content_revision
from dridh.state.store import utc_now

SECTION_HASH_LENGTH = 64


def content_hash(text: str) -> str:
    """SHA-256 hex of the LF-normalised text. Matches ``content_revision`` without its prefix."""

    return content_revision(text)[len("sha256:") :]


@dataclass(frozen=True)
class RevisionRecord:
    id: str
    relative_path: str
    revision_number: int
    content_sha256: str
    byte_size: int
    line_count: int
    standards_version: str
    source_path: str
    created_at: str


@dataclass(frozen=True)
class SectionRecord:
    ordinal: int
    level: int
    heading: str
    start_line: int
    end_line: int
    section_sha256: str


@dataclass(frozen=True)
class ValidationRecord:
    id: str
    revision_id: str
    revision_number: int
    validator: str
    validator_version: str
    status: str
    error_count: int
    warning_count: int
    validated_at: str


def compute_sections(content: str) -> list[SectionRecord]:
    """Every Markdown heading starts a section that runs to the line before the next heading."""

    lines = content.splitlines()
    headings = parse_headings(content)
    sections: list[SectionRecord] = []
    for index, heading in enumerate(headings, start=1):
        end = headings[index].line - 1 if index < len(headings) else len(lines)
        body = "\n".join(lines[heading.line : end])
        digest = content_hash(f"{heading.level} {heading.title}\n{body}")
        sections.append(
            SectionRecord(
                ordinal=index,
                level=heading.level,
                heading=heading.title,
                start_line=heading.line,
                end_line=end,
                section_sha256=digest,
            )
        )
    return sections


def _section_key(section: SectionRecord) -> tuple[int, int, str, int, int, str]:
    return (section.ordinal, section.level, section.heading, section.start_line, section.end_line, section.section_sha256)


class DocumentRepository:
    """Repository bound to one open workspace connection."""

    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    # ------------------------------------------------------------------ reads

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(
                ErrorCode.INTERNAL_ERROR,
                "Document writes must run inside a transaction so revisions, sections and validations commit together.",
            )

    def _document_row(self, relative_path: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM document WHERE workspace_id = ? AND relative_path = ?",
            (self.workspace_id, relative_path),
        ).fetchone()

    def document_metadata(self, relative_path: str) -> dict[str, object] | None:
        row = self._document_row(relative_path)
        return None if row is None else {key: row[key] for key in row.keys()}

    def current_revision(self, relative_path: str) -> RevisionRecord | None:
        row = self.connection.execute(
            """SELECT r.*, d.relative_path FROM document_revision r
               JOIN document d ON d.id = r.document_id
               WHERE d.workspace_id = ? AND d.relative_path = ?
               ORDER BY r.revision_number DESC LIMIT 1""",
            (self.workspace_id, relative_path),
        ).fetchone()
        return None if row is None else _revision(row)

    def revisions(self, relative_path: str) -> list[RevisionRecord]:
        rows = self.connection.execute(
            """SELECT r.*, d.relative_path FROM document_revision r
               JOIN document d ON d.id = r.document_id
               WHERE d.workspace_id = ? AND d.relative_path = ?
               ORDER BY r.revision_number ASC""",
            (self.workspace_id, relative_path),
        ).fetchall()
        return [_revision(row) for row in rows]

    def stored_content(self, revision_id: str) -> str:
        row = self.connection.execute("SELECT content FROM document_revision WHERE id = ?", (revision_id,)).fetchone()
        if row is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Revision does not exist", details=(revision_id,))
        return str(row["content"])

    def sections(self, revision_id: str) -> list[SectionRecord]:
        rows = self.connection.execute(
            "SELECT * FROM document_section WHERE revision_id = ? ORDER BY ordinal ASC",
            (revision_id,),
        ).fetchall()
        return [
            SectionRecord(
                ordinal=row["ordinal"],
                level=row["level"],
                heading=row["heading"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                section_sha256=row["section_sha256"],
            )
            for row in rows
        ]

    def latest_validation(self, revision_id: str, validator: str) -> ValidationRecord | None:
        row = self.connection.execute(
            """SELECT v.*, r.revision_number FROM document_validation v
               JOIN document_revision r ON r.id = v.revision_id
               WHERE v.revision_id = ? AND v.validator = ?
               ORDER BY v.validated_at DESC, v.rowid DESC LIMIT 1""",
            (revision_id, validator),
        ).fetchone()
        return None if row is None else _validation(row)

    def validation_findings(self, validation_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT * FROM document_validation_finding WHERE validation_id = ? ORDER BY ordinal ASC",
            (validation_id,),
        ).fetchall()
        return [{key: row[key] for key in row.keys() if key != "validation_id"} for row in rows]

    def document_validations(self, relative_path: str) -> list[ValidationRecord]:
        rows = self.connection.execute(
            """SELECT v.*, r.revision_number FROM document_validation v
               JOIN document_revision r ON r.id = v.revision_id
               JOIN document d ON d.id = r.document_id
               WHERE d.workspace_id = ? AND d.relative_path = ?
               ORDER BY v.validated_at ASC, v.rowid ASC""",
            (self.workspace_id, relative_path),
        ).fetchall()
        return [_validation(row) for row in rows]

    # ------------------------------------------------------------------ writes

    def record_revision(
        self,
        *,
        relative_path: str,
        document_type: str,
        content: str,
        standards_version: str,
        source_path: str,
    ) -> tuple[RevisionRecord, bool]:
        """Persist ``content`` as the next revision if it differs from the current one.

        Returns ``(revision, created)``. Recording identical content is a no-op, so history
        never gains duplicate revisions.
        """

        self._require_transaction()
        digest = content_hash(content)
        current = self.current_revision(relative_path)
        if current is not None and current.content_sha256 == digest:
            return current, False

        now = utc_now()
        document = self._document_row(relative_path)
        if document is None:
            document_id = str(uuid.uuid4())
            self.connection.execute(
                """INSERT INTO document(id, workspace_id, relative_path, document_type, standards_version, lifecycle_state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?)""",
                (document_id, self.workspace_id, relative_path, document_type, standards_version, now, now),
            )
        else:
            document_id = str(document["id"])
            if document["document_type"] != document_type:
                raise DridhError(
                    ErrorCode.VALIDATION_FAILED,
                    f"{relative_path} is recorded as {document['document_type']}, not {document_type}",
                    recovery="Use the document's original canonical name or record it under a new path.",
                )
            self.connection.execute(
                "UPDATE document SET standards_version = ?, updated_at = ? WHERE id = ?",
                (standards_version, now, document_id),
            )

        revision_id = str(uuid.uuid4())
        number = 1 if current is None else current.revision_number + 1
        try:
            self.connection.execute(
                """INSERT INTO document_revision(id, document_id, revision_number, content_sha256, content, byte_size, line_count, standards_version, source_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    document_id,
                    number,
                    digest,
                    content,
                    len(content.encode("utf-8")),
                    len(content.splitlines()),
                    standards_version,
                    source_path,
                    now,
                ),
            )
            for section in compute_sections(content):
                self.connection.execute(
                    """INSERT INTO document_section(revision_id, ordinal, level, heading, start_line, end_line, section_sha256)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (revision_id, section.ordinal, section.level, section.heading, section.start_line, section.end_line, section.section_sha256),
                )
        except sqlite3.IntegrityError as exc:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "The revision conflicts with persisted document state and was not recorded.",
                details=(str(exc),),
            ) from None
        created = self.current_revision(relative_path)
        assert created is not None
        return created, True

    def record_validation(
        self,
        *,
        revision_id: str,
        validator: str,
        validator_version: str,
        result: ValidationResult,
    ) -> str:
        """Persist one validation outcome and its findings against an exact revision."""

        self._require_transaction()
        exists = self.connection.execute("SELECT 1 FROM document_revision WHERE id = ?", (revision_id,)).fetchone()
        if exists is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Validation refers to a revision that does not exist", details=(revision_id,))
        errors = sum(1 for finding in result.findings if finding.severity == "ERROR")
        warnings = sum(1 for finding in result.findings if finding.severity == "WARNING")
        status = "FAIL" if errors else ("WARNING" if warnings else "PASS")
        validation_id = str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO document_validation(id, revision_id, validator, validator_version, status, error_count, warning_count, validated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (validation_id, revision_id, validator, validator_version, status, errors, warnings, utc_now()),
        )
        for ordinal, finding in enumerate(result.findings, start=1):
            self.connection.execute(
                """INSERT INTO document_validation_finding(validation_id, ordinal, code, severity, line, entity_id, message, expected, actual, repair_hint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    validation_id,
                    ordinal,
                    finding.code,
                    finding.severity,
                    finding.line,
                    finding.entity_id,
                    finding.message,
                    finding.expected,
                    finding.actual,
                    finding.repair_hint,
                ),
            )
        return validation_id

    # --------------------------------------------------------- synchronisation

    def synchronization_findings(
        self,
        *,
        relative_path: str,
        source_text: str | None,
        expected_standards_version: str,
        require_validation: bool = False,
    ) -> list[Finding]:
        """Read-only comparison of a source file with its persisted history.

        Every finding is actionable. Nothing here updates the database or the file.
        """

        findings: list[Finding] = []

        def error(code: str, message: str, **kwargs: object) -> None:
            findings.append(Finding(code=code, message=message, severity="ERROR", file=relative_path, **kwargs))  # type: ignore[arg-type]

        def warning(code: str, message: str, **kwargs: object) -> None:
            findings.append(Finding(code=code, message=message, severity="WARNING", file=relative_path, **kwargs))  # type: ignore[arg-type]

        document = self._document_row(relative_path)
        if document is None:
            error(
                "DOCUMENT_NOT_PERSISTED",
                "The document has no persisted record in the workspace state",
                repair_hint=f"Record it explicitly: dridh documents record {relative_path}",
            )
            return findings
        if source_text is None:
            error(
                "SOURCE_FILE_MISSING",
                "The persisted document has no source file on disk",
                repair_hint="Restore the file, or record its removal explicitly; Dridh will not delete history",
            )
            return findings

        current = self.current_revision(relative_path)
        if current is None:
            error("REVISION_NOT_PERSISTED", "The document record exists but has no revision", repair_hint=f"Record it: dridh documents record {relative_path}")
            return findings

        stored = self.stored_content(current.id)
        stored_hash = content_hash(stored)
        if stored_hash != current.content_sha256:
            error(
                "REVISION_HASH_MISMATCH",
                "The stored text of the current revision does not match its recorded hash (state corruption)",
                expected=current.content_sha256,
                actual=stored_hash,
                repair_hint="Restore the state database from a backup. Do not re-record over corrupt history",
            )
        if [_section_key(s) for s in self.sections(current.id)] != [_section_key(s) for s in compute_sections(stored)]:
            error(
                "SECTIONS_MISMATCH",
                "Persisted sections do not match the stored revision text (state corruption)",
                repair_hint="Restore the state database from a backup",
            )

        source_hash = content_hash(source_text)
        if source_hash != current.content_sha256:
            error(
                "SOURCE_CHANGED_SINCE_REVISION",
                f"The file differs from revision {current.revision_number}; it changed without a recorded revision",
                expected=current.content_sha256,
                actual=source_hash,
                repair_hint=f"Record the change explicitly: dridh documents record {relative_path}",
            )
        if current.standards_version != expected_standards_version:
            error(
                "STANDARDS_VERSION_MISMATCH",
                "The persisted revision was recorded under a different standards version",
                expected=expected_standards_version,
                actual=current.standards_version,
                repair_hint=f"Re-record under standards {expected_standards_version}",
            )

        validations = self.document_validations(relative_path)
        current_validations = [v for v in validations if v.revision_id == current.id]
        if not validations:
            emit = error if require_validation else warning
            emit(
                "VALIDATION_NOT_RECORDED",
                "No validation result is persisted for this document",
                repair_hint=f"Record a validation: dridh documents record {relative_path} --validate",
            )
        elif not current_validations:
            emit = error if require_validation else warning
            emit(
                "VALIDATION_STALE",
                f"The latest validation refers to revision {validations[-1].revision_number}, not the current revision {current.revision_number}",
                expected=f"revision {current.revision_number}",
                actual=f"revision {validations[-1].revision_number}",
                repair_hint=f"Re-validate the current revision: dridh documents record {relative_path} --validate",
            )
        return findings


def _revision(row: sqlite3.Row) -> RevisionRecord:
    return RevisionRecord(
        id=row["id"],
        relative_path=row["relative_path"],
        revision_number=row["revision_number"],
        content_sha256=row["content_sha256"],
        byte_size=row["byte_size"],
        line_count=row["line_count"],
        standards_version=row["standards_version"],
        source_path=row["source_path"],
        created_at=row["created_at"],
    )


def _validation(row: sqlite3.Row) -> ValidationRecord:
    return ValidationRecord(
        id=row["id"],
        revision_id=row["revision_id"],
        revision_number=row["revision_number"],
        validator=row["validator"],
        validator_version=row["validator_version"],
        status=row["status"],
        error_count=row["error_count"],
        warning_count=row["warning_count"],
        validated_at=row["validated_at"],
    )


def require_relative_key(path: str) -> str:
    """Document identity is a relative POSIX path; reject anything that could escape the workspace."""

    if not path or path.startswith("/") or ".." in path.split("/"):
        raise DridhError(ErrorCode.VALIDATION_FAILED, "Document path must be relative to the project root", details=(path,))
    return path

