"""Machine-readable validation findings (TABLE-STANDARDS.md section 18.1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: str = "ERROR"
    file: str | None = None
    line: int | None = None
    entity_id: str | None = None
    expected: str | None = None
    actual: str | None = None
    repair_hint: str | None = None

    def render(self) -> str:
        location = self.file or "<document>"
        if self.line is not None:
            location += f":{self.line}"
        entity = f" [{self.entity_id}]" if self.entity_id else ""
        return f"{self.code} {location}{entity}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "ERROR" for f in self.findings)

    @property
    def errors(self) -> list[str]:
        return [f.render() for f in self.findings if f.severity == "ERROR"]

    @property
    def warnings(self) -> list[str]:
        return [f.render() for f in self.findings if f.severity == "WARNING"]

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings if f.severity == "ERROR"}

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "findings": [f.to_dict() for f in self.findings]}
