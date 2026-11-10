"""Runtime contract registry and Coordinator identifier allocation tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dridh.document_contracts.identifiers import CoordinatorIdAllocator, IdentifierAllocationError
from dridh.document_contracts.registry import ContractBundleError, load_contract_bundle, supported_contract_versions
from dridh.document_contracts.validator import validate_document
from dridh.state.store import StateStore, verify_ledger
from dridh.workspace.layout import init_workspace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "document_contracts"


def _workspace(tmp_path: Path) -> tuple[object, StateStore]:
    project = tmp_path / "project"
    project.mkdir()
    info, _ = init_workspace(project)
    store = StateStore(info.state_db, info.workspace_id)
    store.open()
    return info, store


def test_packaged_contract_bundle_is_versioned_and_integrity_checked() -> None:
    bundle = load_contract_bundle()
    assert bundle.version == "2.1.0"
    assert supported_contract_versions() == ("2.1.0",)
    assert bundle.standards_sha256 == hashlib.sha256(bundle.standards_text.encode("utf-8")).hexdigest()
    assert bundle.template_sha256 == hashlib.sha256(bundle.template_text.encode("utf-8")).hexdigest()
    assert "**Standards Version:** 2.1.0" in bundle.standards_text[:1000]
    assert "Table 15" in bundle.standards_text
    assert "## Document Control" in bundle.template_text
    assert bundle.manifest["standards"]["sha256"] == bundle.standards_sha256
    assert bundle.manifest["tdd_template"]["sha256"] == bundle.template_sha256


def test_contract_bundle_renders_versions_and_source_provenance() -> None:
    bundle = load_contract_bundle()
    digest = "a" * 64
    block = bundle.render_metadata_block(source_revision="approved-use-cases-r4", source_sha256=digest)
    assert f"**Standards SHA256:** {bundle.standards_sha256}" in block
    assert f"**Template SHA256:** {bundle.template_sha256}" in block
    assert "**Source Revision:** approved-use-cases-r4" in block
    assert f"**Source SHA256:** {digest}" in block
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        bundle.render_metadata_block(source_sha256="invalid")


def test_unknown_contract_version_fails_closed() -> None:
    with pytest.raises(ContractBundleError, match="not installed or has an invalid manifest"):
        load_contract_bundle("9.9.9")


def test_tdd_validator_checks_pinned_contract_hashes() -> None:
    bundle = load_contract_bundle()
    content = (FIXTURES / "feature_tdd.md").read_text(encoding="utf-8")
    hash_metadata = "\n".join(
        [
            "> **Standards SHA256:** " + bundle.standards_sha256,
            "> **Template Version:** " + bundle.version,
            "> **Template SHA256:** " + bundle.template_sha256,
        ]
    )
    content = content.replace("> **Standards Version:** 2.1.0", "> **Standards Version:** 2.1.0\n" + hash_metadata, 1)
    assert validate_document("feature_tdd.md", content).ok
    tampered = content.replace(bundle.template_sha256, "0" * 64, 1)
    result = validate_document("feature_tdd.md", tampered)
    assert not result.ok
    assert "CONTRACT_HASH_MISMATCH" in result.codes


def test_allocator_requires_transaction_and_reserves_existing_ids(tmp_path: Path) -> None:
    _, store = _workspace(tmp_path)
    try:
        allocator = CoordinatorIdAllocator(store.connection)
        with pytest.raises(IdentifierAllocationError, match="active SQLite transaction"):
            allocator.allocate("actor")
        content = "UC-01 uses ACT-05 and SF-01.1 as its primary flow."
        with store.transaction() as connection:
            allocator = CoordinatorIdAllocator(connection)
            reserved = allocator.reserve_document_ids(
                content,
                source_document="documents/001-USE-CASES.md",
                document_type="use_cases",
            )
            assert reserved == ("ACT-05", "SF-01.1", "UC-01")
            assert allocator.allocate("actor") == "ACT-06"
            assert allocator.allocate("use_case") == "UC-02"
            assert allocator.allocate("success_flow", parent="UC-01") == "SF-01.2"
            assert allocator.allocate("success_flow", parent="UC-02") == "SF-02.1"
        with store.transaction() as connection:
            allocator = CoordinatorIdAllocator(connection)
            before = connection.execute("SELECT COUNT(*) FROM ledger WHERE event_type LIKE 'identifier.%'").fetchone()[0]
            assert allocator.reserve_document_ids(content, source_document="documents/001-USE-CASES.md", document_type="use_cases") == ()
            after = connection.execute("SELECT COUNT(*) FROM ledger WHERE event_type LIKE 'identifier.%'").fetchone()[0]
            assert before == after
            assert allocator.allocate("actor") == "ACT-07"
        assert verify_ledger(store.connection)["ok"]
    finally:
        store.close()


def test_allocator_supports_story_test_phase_and_extension_namespaces(tmp_path: Path) -> None:
    _, store = _workspace(tmp_path)
    try:
        with store.transaction() as connection:
            allocator = CoordinatorIdAllocator(connection)
            assert allocator.allocate("story", phase=3) == "ST-3.1"
            assert allocator.allocate("story", phase=3) == "ST-3.2"
            assert allocator.allocate("test_phase", prefix="X") == "X1"
            assert allocator.allocate("test_phase", prefix="D") == "D1"
            assert allocator.allocate("extension_story", prefix="QA") == "QA-1"
            # XN is a separate story namespace from TEST-PLAN's XN test phases.
            assert allocator.allocate("cross_service_story") == "X1"
    finally:
        store.close()


def test_allocator_reserves_test_phase_ids_only_for_test_plan_documents(tmp_path: Path) -> None:
    _, store = _workspace(tmp_path)
    try:
        with store.transaction() as connection:
            allocator = CoordinatorIdAllocator(connection)
            reserved = allocator.reserve_document_ids(
                "The test phases include S1, D2 and X1.",
                source_document="documents/TEST-PLAN.md",
                document_type="test_plan",
            )
            assert reserved == ("D2", "S1", "X1")
            assert allocator.allocate("test_phase", prefix="S") == "S2"
            assert allocator.allocate("test_phase", prefix="D") == "D3"
            assert allocator.allocate("test_phase", prefix="X") == "X2"
    finally:
        store.close()


def test_allocator_rejects_invalid_scope_and_reserved_prefixes(tmp_path: Path) -> None:
    _, store = _workspace(tmp_path)
    try:
        with store.transaction() as connection:
            allocator = CoordinatorIdAllocator(connection)
            with pytest.raises(IdentifierAllocationError, match="parent='UC-NN'"):
                allocator.allocate("success_flow")
            with pytest.raises(IdentifierAllocationError, match="reserved"):
                allocator.allocate("extension_story", prefix="UC")
            with pytest.raises(IdentifierAllocationError, match="prefix S, D or X"):
                allocator.allocate("test_phase", prefix="T")
            with pytest.raises(IdentifierAllocationError, match="not a valid"):
                allocator.reserve("ACT-01", family="success_flow", source_document="docs/UC.md", source_sha256="a" * 64)
    finally:
        store.close()
