"""Coordinator-owned persistent allocation of canonical document identifiers.

Identifier reservations and allocations use the workspace's existing hash-chained
SQLite audit ledger. No schema migration is required, and an identifier is never
reissued after it has been reserved or allocated.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import PurePosixPath

from dridh.state.store import append_ledger

ALLOCATED_EVENT = "identifier.allocated"
RESERVED_EVENT = "identifier.reserved"
AUDIT_ACTOR = "coordinator:id-allocator"
_EVENT_TYPES = (ALLOCATED_EVENT, RESERVED_EVENT)

_FLOW_PREFIX = {
    "success_flow": "SF",
    "alternative_flow": "AF",
    "exception_flow": "EF",
    "boundary_flow": "BE",
    "business_rule": "BR",
}
_PATTERNS = {
    "actor": re.compile(r"ACT-(?P<number>\d{2})"),
    "component": re.compile(r"CMP-(?P<number>\d{2})"),
    "use_case": re.compile(r"UC-(?P<number>\d{2})"),
    "ledger_entry": re.compile(r"LDG-(?P<number>\d{2})"),
    "design_decision": re.compile(r"DD-(?P<number>\d+)"),
    "component_decision": re.compile(r"CDD-(?P<number>\d+)"),
    "risk": re.compile(r"R(?P<number>\d+)"),
    "milestone": re.compile(r"M(?P<number>\d+)"),
    "test_phase": re.compile(r"(?P<prefix>[SDX])(?P<number>\d+)"),
    "story": re.compile(r"ST-(?P<phase>\d+)\.(?P<number>\d+)"),
    "prerequisite_story": re.compile(r"PRE-(?P<number>\d+)"),
    "cross_service_story": re.compile(r"X(?P<number>\d+)"),
    "extension_story": re.compile(r"(?P<prefix>[A-Z]{2,3})-(?P<number>\d+)"),
    **{family: re.compile(rf"{prefix}-(?P<parent>\d{{2}})\.(?P<number>\d+)") for family, prefix in _FLOW_PREFIX.items()},
}
_RESERVED_EXTENSION_PREFIXES = {"UC", "ACT", "CMP", "CDD", "DD", "LDG", "SF", "AF", "EF", "BE", "BR", "ST", "PRE"}
_TWO_DIGIT_FAMILIES = {"actor", "component", "use_case", "ledger_entry"}

_SCAN_PATTERNS = (
    ("component_decision", re.compile(r"\bCDD-\d+\b")),
    ("actor", re.compile(r"\bACT-\d{2}\b")),
    ("component", re.compile(r"\bCMP-\d{2}\b")),
    ("use_case", re.compile(r"\bUC-\d{2}\b")),
    ("ledger_entry", re.compile(r"\bLDG-\d{2}\b")),
    ("design_decision", re.compile(r"\bDD-\d+\b")),
    ("success_flow", re.compile(r"\bSF-\d{2}\.\d+\b")),
    ("alternative_flow", re.compile(r"\bAF-\d{2}\.\d+\b")),
    ("exception_flow", re.compile(r"\bEF-\d{2}\.\d+\b")),
    ("boundary_flow", re.compile(r"\bBE-\d{2}\.\d+\b")),
    ("business_rule", re.compile(r"\bBR-\d{2}\.\d+\b")),
    ("story", re.compile(r"\bST-\d+\.\d+\b")),
    ("prerequisite_story", re.compile(r"\bPRE-\d+\b")),
    ("risk", re.compile(r"\bR\d+\b")),
    ("milestone", re.compile(r"\bM\d+\b")),
    ("extension_story", re.compile(r"\b(?!UC-|ACT-|CMP-|CDD-|DD-|LDG-|SF-|AF-|EF-|BE-|BR-|ST-|PRE-)([A-Z]{2,3}-\d+)\b")),
)


class IdentifierAllocationError(ValueError):
    """Raised when a canonical identifier cannot safely be allocated or reserved."""


def _validate_source(source_document: str, source_sha256: str | None) -> None:
    if not source_document or not source_document.strip():
        raise IdentifierAllocationError("source_document must be a non-empty project-relative path")
    path = PurePosixPath(source_document)
    if path.is_absolute() or ".." in path.parts or "\\" in source_document:
        raise IdentifierAllocationError("source_document must be a safe project-relative path")
    if source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise IdentifierAllocationError("source_sha256 must be a lowercase SHA-256 digest")


def _scope(family: str, identifier: str) -> tuple[str, str | None]:
    if family in _FLOW_PREFIX:
        match = _PATTERNS[family].fullmatch(identifier)
        return "parent", f"UC-{match.group('parent')}" if match else None
    if family == "story":
        match = _PATTERNS[family].fullmatch(identifier)
        return "phase", match.group("phase") if match else None
    if family == "test_phase":
        match = _PATTERNS[family].fullmatch(identifier)
        return "prefix", match.group("prefix") if match else None
    if family == "extension_story":
        match = _PATTERNS[family].fullmatch(identifier)
        return "prefix", match.group("prefix") if match else None
    return "global", None


class CoordinatorIdAllocator:
    """Allocate and reserve IDs inside a workspace transaction.

    The actor recorded in the audit chain is fixed to the Coordinator allocator;
    callers cannot pass an LLM or human-supplied actor identity.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise IdentifierAllocationError("ID allocation and reservation require an active SQLite transaction")

    def _claims(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT event_type, payload FROM ledger WHERE event_type IN (?, ?) ORDER BY seq ASC",
            _EVENT_TYPES,
        ).fetchall()
        claims: list[dict[str, str]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise IdentifierAllocationError("An identifier audit event has an invalid payload") from exc
            if not isinstance(payload, dict):
                raise IdentifierAllocationError("An identifier audit event payload must be an object")
            identifier, family = payload.get("identifier"), payload.get("family")
            if isinstance(identifier, str) and isinstance(family, str):
                claims.append({"identifier": identifier, "family": family, "event_type": row["event_type"]})
        return claims

    def reserve(
        self,
        identifier: str,
        *,
        family: str,
        source_document: str,
        source_sha256: str,
    ) -> bool:
        """Permanently reserve an existing ID found in a canonical project document."""

        self._require_transaction()
        _validate_source(source_document, source_sha256)
        pattern = _PATTERNS.get(family)
        if pattern is None or not pattern.fullmatch(identifier):
            raise IdentifierAllocationError(f"{identifier!r} is not a valid {family!r} identifier")
        if family == "extension_story" and identifier.split("-", 1)[0] in _RESERVED_EXTENSION_PREFIXES:
            raise IdentifierAllocationError("The extension story prefix is reserved by a canonical identifier family")
        for claim in self._claims():
            if claim["identifier"] == identifier and claim["family"] == family:
                return False
        append_ledger(
            self.connection,
            None,
            RESERVED_EVENT,
            AUDIT_ACTOR,
            {
                "identifier": identifier,
                "family": family,
                "source_document": source_document,
                "source_sha256": source_sha256,
                "action": "reserved_existing",
            },
        )
        return True

    def reserve_document_ids(
        self,
        content: str,
        *,
        source_document: str,
        document_type: str,
    ) -> tuple[str, ...]:
        """Reserve recognized identifiers from a document, idempotently.

        Test-phase identifiers are interpreted only in TEST-PLAN documents.
        Outside TEST-PLAN, XN is recognized only when used as a story heading.
        """

        self._require_transaction()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        _validate_source(source_document, digest)
        found: dict[str, str] = {}
        for family, pattern in _SCAN_PATTERNS:
            for match in pattern.finditer(content):
                found.setdefault(match.group(0), family)
        if document_type == "test_plan":
            for match in re.finditer(r"\b[SDX]\d+\b", content):
                found.setdefault(match.group(0), "test_phase")
        else:
            for match in re.finditer(r"(?m)^\s*#{1,6}\s+(X\d+)\s*:", content):
                found.setdefault(match.group(1), "cross_service_story")
        reserved: list[str] = []
        for identifier, family in sorted(found.items()):
            if self.reserve(identifier, family=family, source_document=source_document, source_sha256=digest):
                reserved.append(identifier)
        return tuple(reserved)

    def allocate(
        self,
        family: str,
        *,
        parent: str | None = None,
        phase: int | None = None,
        prefix: str | None = None,
        existing_ids: Iterable[str] = (),
        source_document: str | None = None,
    ) -> str:
        """Allocate the next unused canonical ID in the requested family/scope."""

        self._require_transaction()
        if family not in _PATTERNS:
            raise IdentifierAllocationError(f"Unsupported identifier family: {family}")
        if family in _FLOW_PREFIX:
            if parent is None or not re.fullmatch(r"UC-\d{2}", parent):
                raise IdentifierAllocationError(f"{family} allocation requires parent='UC-NN'")
        elif family == "story":
            if phase is None or phase < 1:
                raise IdentifierAllocationError("story allocation requires a positive phase number")
        elif family == "test_phase":
            if prefix not in {"S", "D", "X"}:
                raise IdentifierAllocationError("test_phase allocation requires prefix S, D or X")
        elif family == "extension_story":
            if prefix is None or not re.fullmatch(r"[A-Z]{2,3}", prefix) or prefix in _RESERVED_EXTENSION_PREFIXES:
                raise IdentifierAllocationError("extension_story allocation requires a non-reserved 2–3 letter uppercase prefix")
        if source_document is not None:
            _validate_source(source_document, None)

        requested_scope = (
            ("parent", parent) if family in _FLOW_PREFIX else
            ("phase", str(phase)) if family == "story" else
            ("prefix", prefix) if family in {"test_phase", "extension_story"} else
            ("global", None)
        )
        used: set[str] = set()
        largest = 0
        for claim in self._claims():
            if claim["family"] != family:
                continue
            identifier = claim["identifier"]
            if _scope(family, identifier) != requested_scope:
                continue
            match = _PATTERNS[family].fullmatch(identifier)
            if match:
                used.add(identifier)
                largest = max(largest, int(match.group("number")))
        for identifier in existing_ids:
            if not isinstance(identifier, str) or not _PATTERNS[family].fullmatch(identifier):
                raise IdentifierAllocationError(f"{identifier!r} is not a valid {family!r} identifier in existing_ids")
            if _scope(family, identifier) == requested_scope:
                match = _PATTERNS[family].fullmatch(identifier)
                if match:
                    used.add(identifier)
                    largest = max(largest, int(match.group("number")))
        number = largest + 1
        if family in _TWO_DIGIT_FAMILIES and number > 99:
            raise IdentifierAllocationError(f"The two-digit {family} identifier range is exhausted")
        if family in _FLOW_PREFIX:
            candidate = f"{_FLOW_PREFIX[family]}-{parent[3:]}.{number}"
        elif family == "story":
            candidate = f"ST-{phase}.{number}"
        elif family == "test_phase":
            candidate = f"{prefix}{number}"
        elif family == "extension_story":
            candidate = f"{prefix}-{number}"
        else:
            stem = {
                "actor": "ACT",
                "component": "CMP",
                "use_case": "UC",
                "ledger_entry": "LDG",
                "design_decision": "DD",
                "component_decision": "CDD",
                "risk": "R",
                "milestone": "M",
                "prerequisite_story": "PRE",
                "cross_service_story": "X",
            }[family]
            candidate = f"{stem}-{number:02d}" if family in _TWO_DIGIT_FAMILIES else f"{stem}{number}" if stem in {"R", "M", "X"} else f"{stem}-{number}"
        if candidate in used:
            raise IdentifierAllocationError(f"Internal allocator collision while generating {candidate}")
        append_ledger(
            self.connection,
            None,
            ALLOCATED_EVENT,
            AUDIT_ACTOR,
            {
                "identifier": candidate,
                "family": family,
                "parent": parent,
                "phase": phase,
                "prefix": prefix,
                "source_document": source_document,
                "action": "allocated",
            },
        )
        return candidate
