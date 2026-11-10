"""Deterministic Phase 13 story DAG, traceability and resumability tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.integrations.opencode import AgentErrorCode, AgentRunError
from dridh.planner.service import PlannerService, _overlap, _topological


def test_topological_order_and_depth_are_deterministic() -> None:
    stories = [
        {"key": "release", "dependencies": ["api", "ui"]},
        {"key": "ui", "dependencies": []},
        {"key": "api", "dependencies": []},
    ]
    order, depth = _topological(stories)
    assert order == ["api", "ui", "release"]
    assert depth == {"api": 1, "ui": 1, "release": 2}


@pytest.mark.parametrize("stories", [
    [{"key": "a", "dependencies": ["a"]}],
    [{"key": "a", "dependencies": ["missing"]}],
    [{"key": "a", "dependencies": ["b"]}, {"key": "b", "dependencies": ["a"]}],
])
def test_topological_validation_rejects_self_unknown_and_cyclic_edges(stories: list[dict[str, object]]) -> None:
    with pytest.raises(DridhError) as error:
        _topological(stories)
    assert error.value.code is ErrorCode.VALIDATION_FAILED


def test_file_ownership_overlap_detects_parent_and_child_paths() -> None:
    assert _overlap("src/payments", "src/payments/service.py")
    assert _overlap("src/**", "src/payments/service.py")
    assert not _overlap("src/payments", "src/catalog/service.py")
    assert _overlap("src/payments/service.py", "src/payments/service.py")


def _story(key: str, *, resources: list[str] | None = None) -> dict[str, object]:
    return {
        "key": key, "component_id": "CMP-01", "title": f"{key} implementation",
        "description": f"Implement {key}.", "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"],
        "infrastructure": False, "infrastructure_rationale": "",
        "todos": ["Implement the contract."], "acceptance_criteria": ["The contract is verified."],
        "definition_of_done": ["Tests pass."], "dependencies": [], "dependency_rationale": {},
        "file_ownership": [f"src/{key}.py"], "resource_ownership": resources or [],
    }


def test_indexer_requires_exact_approved_flow_component_traceability() -> None:
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "components": [{"id": "CMP-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    result = service._validate_indexer({"stories": [_story("api")], "summary": "done", "blockers": []}, baseline)
    assert result["order"] == ["api"]
    assert result["depths"] == {"api": 1}


def test_indexer_rejects_feature_story_with_no_file_ownership() -> None:
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "components": [{"id": "CMP-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    story = _story("api")
    story["file_ownership"] = []
    with pytest.raises(DridhError):
        service._validate_indexer({"stories": [story]}, baseline)


def test_infrastructure_story_needs_a_real_component_and_error_lists_valid_ids() -> None:
    """Regression (live run): a model added a cross-cutting scaffolding story with no component_id."""
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "components": [{"id": "CMP-01"}, {"id": "CMP-02"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    feature = {**_story("orders"), "dependencies": ["scaffolding"], "dependency_rationale": {"scaffolding": "Repo and CI exist first."}}
    infra = {**_story("scaffolding"), "component_id": "CMP-01", "use_case_ids": [], "flow_ids": [],
             "infrastructure": True, "infrastructure_rationale": "Repository structure and CI pipeline.", "file_ownership": []}
    out = service._validate_indexer({"stories": [feature, infra]}, baseline)
    assert {item["key"] for item in out["stories"]} == {"orders", "scaffolding"}

    for bad in (None, "", "CMP-99"):
        broken = {**infra, "component_id": bad}
        with pytest.raises(DridhError) as exc_info:
            service._validate_indexer({"stories": [feature, broken]}, baseline)
        assert "valid component_id values: CMP-01, CMP-02" in exc_info.value.details
    missing = {k: v for k, v in infra.items() if k != "component_id"}
    with pytest.raises(DridhError):
        service._validate_indexer({"stories": [feature, missing]}, baseline)


def test_indexer_prompt_requires_component_id_on_every_story_including_infrastructure() -> None:
    service = PlannerService.__new__(PlannerService)
    prompt = service._indexer_prompt({"code_index": {"required": False}, "components": [], "flows": [], "tdd_traceability": []}, {"clarifications": []})
    assert "including infrastructure stories, MUST set component_id" in prompt


def test_parallel_pairs_exclude_shared_resources() -> None:
    service = PlannerService.__new__(PlannerService)
    stories = [
        _story("users", resources=["db:users"]),
        _story("orders", resources=["db:users"]),
        _story("catalog", resources=["db:catalog"]),
    ]
    story_ids = {"users": "ST-1.1", "orders": "ST-1.2", "catalog": "ST-1.3"}
    result = service._parallel(stories, story_ids)
    assert "ST-1.2" not in result["ST-1.1"]
    assert "ST-1.1" not in result["ST-1.2"]
    assert "ST-1.3" in result["ST-1.1"]
    assert "ST-1.1" in result["ST-1.3"]


def test_central_plan_renders_parallel_partners_from_real_parallel_output() -> None:
    """Regression: `_parallel` is keyed by story ID; the renderer once indexed it by proposal key."""
    service = PlannerService.__new__(PlannerService)
    stories = [
        {**_story("users", resources=["db:users"]), "key": "users", "component_id": "CMP-01", "title": "Users",
         "use_case_ids": ["UC-01"], "dependencies": [], "dependency_rationale": {}},
        {**_story("catalog", resources=["db:catalog"]), "key": "catalog", "component_id": "CMP-01", "title": "Catalog",
         "use_case_ids": ["UC-01"], "dependencies": [], "dependency_rationale": {}},
    ]
    story_ids = {"users": "ST-1.1", "catalog": "ST-1.2"}
    partners = service._parallel(stories, story_ids)
    payload = {
        "feature": "Demo",
        "source": {"tdd_revision_id": "rev-1", "tdd_sha256": "0" * 64},
        "indexer": {"order": ["users", "catalog"]},
        "baseline": {"components": [{"id": "CMP-01", "name": "Core"}]},
    }
    text = service._render_central(payload, stories, story_ids, {"users": 1, "catalog": 1}, partners)
    rows = {line.split("|")[2].strip(): line for line in text.splitlines() if line.startswith("| ") and "ST-1." in line}
    assert rows["ST-1.1"].split("|")[9].strip() == "ST-1.2"
    assert rows["ST-1.2"].split("|")[9].strip() == "ST-1.1"


def test_testing_planner_requires_traceability_and_unique_compose_names() -> None:
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "branch_slug": "feature-planner",
        "use_cases": [{"id": "UC-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
        "components": [{"id": "CMP-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    phase = {
        "key": "api", "test_scope": "API journey", "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"],
        "component_ids": ["CMP-01"], "dependency_mode": "DOCKER_COMPOSE",
        "environment": "branch: feature/planner; testcontainers: unsuitable; project: planner-feature-planner; network: planner-feature-planner",
        "execution_command": "pytest", "evidence": "artifacts/test.json", "service_inventory": "postgres container",
        "parallel_isolation": "unique test database and cleanup", "fallback_rationale": "",
    }
    with pytest.raises(DridhError):
        service._testing_output(
            {"phases": [phase, {**phase, "key": "second"}], "mock_policy": "Do not mock external dependencies."},
            baseline,
        )


def test_component_output_accepts_strings_or_string_arrays_and_rejects_unusable_values() -> None:
    """Regression: a live model returned multi-point fields as arrays; the validator demanded strings."""
    service = PlannerService.__new__(PlannerService)
    base = {"component_id": "CMP-01", "summary": "Orders", "responsibilities": "Own orders",
            "boundaries": "No billing", "dependencies": "None"}
    ok = service._component_output({"components": [base]}, {"CMP-01"})
    assert ok["components"][0]["responsibilities"] == "Own orders"

    listy = {**base, "responsibilities": ["Create orders", " Cancel orders ", ""], "dependencies": ["CMP-02"]}
    out = service._component_output({"components": [listy]}, {"CMP-01"})["components"][0]
    assert out["responsibilities"] == "Create orders; Cancel orders"
    assert out["dependencies"] == "CMP-02"

    for bad in ("", "  ", [], [""], ["ok", 3], {"a": "b"}, None, 7):
        with pytest.raises(DridhError) as exc_info:
            service._component_output({"components": [{**base, "responsibilities": bad}]}, {"CMP-01"})
        assert "responsibilities" in exc_info.value.message


def test_testing_output_accepts_string_arrays_for_prose_fields_but_not_commands() -> None:
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "branch_slug": "feature-planner", "use_cases": [{"id": "UC-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}], "components": [{"id": "CMP-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    phase = {
        "key": "api", "test_scope": ["API journey", "Error paths"], "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"],
        "component_ids": ["CMP-01"], "dependency_mode": "TESTCONTAINERS",
        "environment": ["postgres container", "kafka container"], "execution_command": "pytest tests/api",
        "evidence": ["artifacts/junit.xml", "artifacts/coverage.xml"],
    }
    result = {"phases": [phase], "mock_policy": "Do not mock external dependencies."}
    out = service._testing_output(result, baseline)["phases"][0]
    assert out["test_scope"] == "API journey; Error paths"
    assert out["evidence"] == "artifacts/junit.xml; artifacts/coverage.xml"
    with pytest.raises(DridhError):
        service._testing_output({**result, "phases": [{**phase, "execution_command": ["pytest", "-q"]}]}, baseline)


def test_testing_output_normalises_phase_keys_and_rejects_collisions_and_empty() -> None:
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "branch_slug": "feature-planner", "use_cases": [{"id": "UC-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}], "components": [{"id": "CMP-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    phase = {
        "key": "Happy_Path 01", "test_scope": "API", "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"],
        "component_ids": ["CMP-01"], "dependency_mode": "TESTCONTAINERS", "environment": "postgres container",
        "execution_command": "pytest", "evidence": "artifacts/junit.xml",
    }
    policy = "Do not mock external dependencies."
    out = service._testing_output({"phases": [phase], "mock_policy": policy}, baseline)
    assert out["phases"][0]["key"] == "happy-path-01"
    for bad in ("", "___", "  "):
        with pytest.raises(DridhError):
            service._testing_output({"phases": [{**phase, "key": bad}], "mock_policy": policy}, baseline)
    with pytest.raises(DridhError):  # normalisation must not let two keys collide silently
        service._testing_output({"phases": [phase, {**phase, "key": "happy path 01"}], "mock_policy": policy}, baseline)


def test_no_mock_policy_accepts_real_wording_and_rejects_non_prohibitions() -> None:
    """Regression: the live model wrote "No routine mocks ..." and was rejected by a substring check."""
    from dridh.planner.service import _states_no_mock_policy

    accepted = [
        "No routine mocks of application external dependencies. Tests use real containers via Testcontainers. "
        "WireMock is reserved only for dependencies that cannot run locally, which does not apply to any phase.",
        "Do not mock external dependencies.",
        "We never use mocks for databases.",
        "Avoid mocking the payment gateway; use a real container.",
        "Tests run without mocks.",
    ]
    rejected = [
        "", "   ", None, 7, ["Do not mock"],
        "Tests use real containers.",
        "Mocks are used for all external services.",
        "Use WireMock for every dependency. Nothing else is required.",
        "No concerns here. Mocks stand in for the database.",
    ]
    for text in accepted:
        assert _states_no_mock_policy(text), text
    for text in rejected:
        assert not _states_no_mock_policy(text), text


def test_component_prompt_exempts_infrastructure_stories_from_ownership_blockers() -> None:
    """Regression (live run): the Component Planner blocked on a scaffolding story hosted under CMP-01."""
    import inspect

    source = inspect.getsource(PlannerService)
    assert "Stories with infrastructure=true are cross-cutting setup/operations" in source
    assert "do not raise a blocker because they fall outside the" in source
    profile = (Path(__file__).resolve().parents[2] / "opencode/agents/dridh/workers/planner-component.md").read_text(encoding="utf-8")
    assert "never raise a blocker because they fall outside the component's responsibility" in profile


def test_component_prompt_limits_blockers_to_placement_and_ownership() -> None:
    """Regression (live run): the model re-reviewed the approved TDD and blocked on flow wording and a missing payment contract."""
    import inspect

    source = inspect.getsource(PlannerService)
    assert "The TDD is already approved: do not re-review it." in source
    assert "are not blockers: note them briefly in the summary field" in source
    profile = (Path(__file__).resolve().parents[2] / "opencode/agents/dridh/workers/planner-component.md").read_text(encoding="utf-8")
    assert "The approved TDD is not up for review here." in profile
    assert "are never blockers; mention them briefly in the summary" in profile


def test_testing_output_rejects_non_enum_dependency_mode_and_lists_allowed_values() -> None:
    """Regression (live run): the model used "chained"/"isolated" as dependency_mode."""
    service = PlannerService.__new__(PlannerService)
    baseline = {
        "branch_slug": "feature-planner", "use_cases": [{"id": "UC-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}], "components": [{"id": "CMP-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    phase = {
        "key": "api", "test_scope": "API", "use_case_ids": ["UC-01"], "flow_ids": ["SF-01.1"], "component_ids": ["CMP-01"],
        "dependency_mode": "chained", "environment": "postgres container", "execution_command": "pytest", "evidence": "junit.xml",
    }
    with pytest.raises(DridhError) as exc_info:
        service._testing_output({"phases": [phase], "mock_policy": "Do not mock external dependencies."}, baseline)
    assert "chained" in exc_info.value.details
    assert any("TESTCONTAINERS" in str(item) and "WIREMOCK_EXCEPTION" in str(item) for item in exc_info.value.details)
    import inspect

    assert "dependency_mode is an enum: exactly one of TESTCONTAINERS" in inspect.getsource(PlannerService)


def test_component_and_testing_prompts_state_field_types() -> None:
    """The prompt contract must tell the model the output types, not just the field names."""
    import inspect

    source = inspect.getsource(PlannerService)
    assert "single non-empty plain-text string" in source
    assert "arrays of IDs; every other phase field is a single plain-text string" in source


def test_new_planner_session_json_and_markdown_are_resumable(tmp_path) -> None:
    workspace = type("Workspace", (), {"root": tmp_path / ".opencode" / "dridh", "workspace_id": "Workspace_A"})()
    service = PlannerService.__new__(PlannerService)
    service.workspace = workspace
    service.state_root = workspace.root.resolve()
    service.project_root = tmp_path.resolve()
    payload = {
        "schema": "dridh.planning", "schema_version": 1, "session_id": "planner-0123456789ab",
        "status": "INDEXING", "designer_session_id": "designer-0123456789ab", "feature": "Fixture feature",
        "source": {"tdd_path": "TDD.md", "tdd_revision_id": "rev-1", "tdd_sha256": "abc", "use_case_revision_id": "rev-2", "use_case_sha256": "def"},
        "code_index": {"required": False, "run_id": None, "files": []}, "clarifications": [],
        "indexer": None, "components": None, "testing": None, "identifiers": None, "generated": None,
        "created_at": "2026-11-07T00:00:00+00:00", "updated_at": "2026-11-07T00:00:00+00:00", "last_error": None,
    }
    service._save(payload)
    loaded = service._load("planner-0123456789ab")
    assert loaded["status"] == "INDEXING" and loaded["indexer"] is None
    assert (workspace.root / "artifacts" / "planner" / "planner-0123456789ab" / "PLANNING-NOTES.md").is_file()


def test_branch_slug_uses_workspace_fallback_outside_git_repository(tmp_path) -> None:
    workspace = type("Workspace", (), {"root": tmp_path / ".opencode" / "dridh", "workspace_id": "Workspace_A"})()
    service = PlannerService.__new__(PlannerService)
    service.workspace = workspace
    service.project_root = tmp_path.resolve()
    assert service._branch_slug() == "workspace-workspace-a"


def test_document_target_rejects_parent_traversal(tmp_path) -> None:
    workspace = type("Workspace", (), {"root": tmp_path / ".opencode" / "dridh", "workspace_id": "workspace"})()
    service = PlannerService.__new__(PlannerService)
    service.workspace = workspace
    service.project_root = tmp_path.resolve()
    with pytest.raises(DridhError):
        service._document_target("../escape.md")



def test_provider_failure_is_persisted_and_start_retries_same_indexer_session(tmp_path) -> None:
    class RetryRunner:
        def __init__(self) -> None:
            self.attempts = 0

        def run_sync(self, *, agent: str, prompt: str, model: str | None = None, title: str | None = None):
            assert agent == "dridh/workers/planner-indexer"
            self.attempts += 1
            if self.attempts == 1:
                raise AgentRunError(
                    AgentErrorCode.PROVIDER_FAILURE,
                    "OpenRouter returned a transient upstream error.",
                    details=("retry after provider recovery",),
                    retryable=True,
                )
            return SimpleNamespace(text=json.dumps({
                "summary": "Index proposal recovered.",
                "blockers": [],
                "stories": [_story("api")],
                "project_structure": {"summary": "Existing project layout.", "recommendations": []},
            }))

    workspace = type("Workspace", (), {
        "root": tmp_path / ".opencode" / "dridh",
        "project_root": tmp_path,
        "workspace_id": "phase13-retry-fixture",
    })()
    source = {
        "tdd_path": "feature_tdd.md",
        "tdd_revision_id": "tdd-revision",
        "tdd_sha256": "a" * 64,
        "use_case_revision_id": "use-case-revision",
        "use_case_sha256": "b" * 64,
    }
    baseline = {
        "components": [{"id": "CMP-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    runner = RetryRunner()
    service = PlannerService(workspace, runner=runner)
    service._preflight = lambda _session: (source, object(), object(), {"required": False, "run_id": None, "files": []})
    service._baseline = lambda *_args: baseline

    first = service.start("designer-0123456789ab")
    session_id = first["session_id"]
    assert first["status"] == "BLOCKED"
    assert first["worker_error"]["code"] == "PROVIDER_FAILURE"
    assert first["worker_error"]["retryable"] is True
    assert "PROVIDER_FAILURE" in (workspace.root / "artifacts" / "planner" / session_id / "PLANNING-NOTES.md").read_text()

    resumed = service.start("designer-0123456789ab")
    assert resumed["session_id"] == session_id
    assert resumed["status"] == "INQUIRY"
    assert resumed["stories"] == 1
    assert resumed["worker_error"] is None
    assert runner.attempts == 2



def test_indexer_prompt_puts_canonical_baseline_last_and_explains_new_codebase_mode() -> None:
    workspace = type("Workspace", (), {"mode": "new", "workspace_id": "new-project"})()
    service = PlannerService.__new__(PlannerService)
    service.workspace = workspace
    baseline = {
        "code_index": {"required": False, "run_id": None, "files": []},
        "components": [{"id": "CMP-01"}],
        "flows": [{"id": "SF-01.1", "use_case_id": "UC-01"}],
        "tdd_traceability": [{"flow_id": "SF-01.1", "component_id": "CMP-01"}],
    }
    prompt = service._indexer_prompt(baseline, {"clarifications": []})
    assert "NEW CODEBASE MODE" in prompt
    assert "MUST NOT be reported as a blocker" in prompt
    assert prompt.endswith(json.dumps(baseline, ensure_ascii=False))



def test_indexer_prompt_contains_authoritative_component_id_and_flow_matrix() -> None:
    workspace = type("Workspace", (), {"mode": "new", "workspace_id": "matrix-test"})()
    service = PlannerService.__new__(PlannerService)
    service.workspace = workspace
    baseline = {
        "code_index": {"required": False},
        "components": [
            {"id": "CMP-01", "name": "Order Service"},
            {"id": "CMP-02", "name": "Notification Service"},
        ],
        "flows": [{"id": "SF-02.1", "use_case_id": "UC-02"}],
        "tdd_traceability": [
            {"flow_id": "SF-02.1", "component_id": "CMP-01"},
            {"flow_id": "SF-02.1", "component_id": "CMP-02"},
        ],
    }
    prompt = service._indexer_prompt(baseline, {"clarifications": []})
    assert "AUTHORITATIVE COMPONENT ID AND FLOW OWNERSHIP MATRIX" in prompt
    assert '"component_id": "CMP-01"' in prompt
    assert '"component_id": "CMP-02"' in prompt
    marker = "AUTHORITATIVE COMPONENT ID AND FLOW OWNERSHIP MATRIX (copy component_id values exactly; canonical_name is descriptive only):\n"
    matrix_text = prompt.split(marker, 1)[1].split("\n\nAPPROVED INPUT", 1)[0]
    matrix = json.loads(matrix_text)
    assert {item["component_id"]: item["approved_flows"] for item in matrix} == {
        "CMP-01": ["SF-02.1"],
        "CMP-02": ["SF-02.1"],
    }
