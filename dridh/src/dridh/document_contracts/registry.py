"""Versioned, integrity-checked document contract bundles shipped with Dridh."""

# ruff: noqa: I001
from __future__ import annotations

import dataclasses
import hashlib
import importlib.resources
import json
import re
import typing


DEFAULT_CONTRACT_VERSION = "2.1.0"
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class ContractBundleError(RuntimeError):
    """A requested contract bundle is missing or fails its integrity checks."""


@dataclasses.dataclass(frozen=True)
class ContractBundle:
    version: str
    standards_text: str
    template_text: str
    standards_sha256: str
    template_sha256: str
    manifest: dict[str, typing.Any]

    def metadata_fields(
        self,
        *,
        source_revision: str | None = None,
        source_sha256: str | None = None,
    ) -> dict[str, str]:
        """Metadata every document generator should carry into its output."""

        fields = {
            "Standards Version": self.version,
            "Standards SHA256": self.standards_sha256,
            "Template Version": self.version,
            "Template SHA256": self.template_sha256,
        }
        if source_revision:
            fields["Source Revision"] = source_revision
        if source_sha256:
            if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
            fields["Source SHA256"] = source_sha256
        return fields

    def render_metadata_block(
        self,
        *,
        source_revision: str | None = None,
        source_sha256: str | None = None,
    ) -> str:
        """Render stable Markdown blockquote metadata for a generated document."""

        fields = self.metadata_fields(source_revision=source_revision, source_sha256=source_sha256)
        return "\n".join(f"> **{key}:** {value}  " for key, value in fields.items()).rstrip()


def _package_for(version: str) -> str:
    if not _VERSION.fullmatch(version):
        raise ContractBundleError(f"Invalid contract version: {version!r}")
    return f"dridh.document_contracts.contracts.v{version.replace('.', '_')}"


def _read_contract(root: typing.Any, descriptor: dict[str, typing.Any], label: str) -> tuple[str, str]:
    filename = descriptor.get("file")
    if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise ContractBundleError(f"{label} has an unsafe or missing packaged filename")
    try:
        raw = root.joinpath(filename).read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise ContractBundleError(f"Packaged {label} is unavailable: {filename}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    expected = descriptor.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ContractBundleError(f"{label} manifest entry has an invalid SHA-256")
    if actual != expected:
        raise ContractBundleError(f"{label} integrity check failed: expected {expected}, found {actual}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractBundleError(f"Packaged {label} is not valid UTF-8") from exc
    return text, actual


def load_contract_bundle(version: str = DEFAULT_CONTRACT_VERSION) -> ContractBundle:
    """Load a packaged contract version and verify its files against the manifest."""

    package = _package_for(version)
    try:
        root = importlib.resources.files(package)
        manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
    except (ModuleNotFoundError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ContractBundleError(f"Contract bundle {version} is not installed or has an invalid manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "dridh.document-contracts/1":
        raise ContractBundleError(f"Contract bundle {version} has an unsupported manifest schema")
    if manifest.get("bundle_version") != version:
        raise ContractBundleError(f"Manifest version does not match requested contract version {version}")
    standards_descriptor = manifest.get("standards")
    template_descriptor = manifest.get("tdd_template")
    if not isinstance(standards_descriptor, dict) or not isinstance(template_descriptor, dict):
        raise ContractBundleError(f"Contract bundle {version} must declare standards and TDD template files")
    if standards_descriptor.get("version") != version or template_descriptor.get("version") != version:
        raise ContractBundleError("Standards and TDD template versions must match the requested bundle version")
    standards_text, standards_hash = _read_contract(root, standards_descriptor, "TABLE-STANDARDS.md")
    template_text, template_hash = _read_contract(root, template_descriptor, "Tech-Design-Template.md")
    marker = f"**Standards Version:** {version}"
    if marker not in standards_text[:1000] or marker not in template_text[:1000]:
        raise ContractBundleError(f"Packaged documents do not declare Standards Version {version}")
    return ContractBundle(
        version=version,
        standards_text=standards_text,
        template_text=template_text,
        standards_sha256=standards_hash,
        template_sha256=template_hash,
        manifest=manifest,
    )


def supported_contract_versions() -> tuple[str, ...]:
    """Return versions installed in this Python package."""

    available: list[str] = []
    root = importlib.resources.files("dridh.document_contracts.contracts")
    for item in root.iterdir():
        if item.is_dir() and item.name.startswith("v"):
            version = item.name[1:].replace("_", ".")
            if _VERSION.fullmatch(version):
                available.append(version)
    return tuple(sorted(available))
