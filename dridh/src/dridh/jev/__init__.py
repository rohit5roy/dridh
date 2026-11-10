"""Optional Jev client. Never on the critical path; bounded; deterministic fallback."""

from dridh.jev.client import JevClient, JevOutcome, JevQuestion, JevStatus

__all__ = ["JevClient", "JevOutcome", "JevQuestion", "JevStatus"]
