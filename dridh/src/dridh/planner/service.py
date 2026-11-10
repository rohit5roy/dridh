"""Phase 13 Planner: approved-design decomposition, document generation and reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.designer.service import DesignerService
from dridh.document_contracts.identifiers import CoordinatorIdAllocator
from dridh.document_contracts.registry import load_contract_bundle
from dridh.document_contracts.validator import (
    H_CENTRAL,
    H_DASHBOARD,
    H_MILESTONES,
    H_RISKS,
    H_STORY_INDEX,
    H_TEST,
    STANDARDS_VERSION,
    validate_central_plan,
    validate_component_master,
    validate_phase_file,
    validate_test_plan,
)
from dridh.integrations.opencode import AgentRunError, OpenCodeRunner
from dridh.orchestration.repair import RepairOutcome, call_with_repair
from dridh.state.design import DesignRepository, validated_master_model, validated_tdd_model
from dridh.state.documents import DocumentRepository, content_hash
from dridh.state.planning import (
    PlanningRepository,
    central_signature,
    phase_identity,
    phase_signature,
    test_plan_signature,
    validated_central,
    validated_phase,
    validated_test_plan,
)
from dridh.state.reviews import ReviewRepository
from dridh.state.store import StateStore, canonical_json
from dridh.state.usecases import UseCaseRepository
from dridh.workspace.layout import WorkspaceInfo

SESSION_SCHEMA = "dridh.planning"
SESSION_VERSION = 1
SESSION_ID = re.compile(r"^planner-[0-9a-f]{12}$")
STORY_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_WORKER_LABELS = {
    "dridh/workers/planner-indexer": "Central Plan Indexer",
    "dridh/workers/planner-component": "Component Planner",
    "dridh/workers/planner-testing": "Testing Planner",
}
FLOW_ID = re.compile(r"(?:SF|AF|EF|BE|BR)-\d{2}\.\d+")
UC_ID = re.compile(r"UC-\d{2}")
CMP_ID = re.compile(r"CMP-\d{2}")
TEST_MODES = frozenset({"TESTCONTAINERS", "DOCKER_COMPOSE", "REAL_QA_ENDPOINT", "WIREMOCK_EXCEPTION"})
STORY_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "IN_REVIEW", "COMPLETED", "BLOCKED")
TEST_STATUS = "NOT_STARTED"
PLANNER_VALIDATOR_VERSION = "1"
REVIEW_CHECKLIST = (
    "The approved Use Case revision and TDD revision are still current.",
    "Every approved Use Case and flow maps to one or more implementation stories.",
    "Every story maps to a canonical TDD component or is explicitly classified as infrastructure.",
    "Story cards have bounded ownership, actionable TODOs, acceptance criteria and a Definition of Done.",
    "Dependencies have rationale, resolve to known stories and form an acyclic topological order.",
    "Unlocks are derived from reverse dependency edges; parallel pairs have no dependency or ownership conflict.",
    "Component masters and phase files agree with the Central Plan Index.",
    "Table 15 covers the planned user journeys and uses an approved real-dependency mode.",
    "Testcontainers is preferred; Compose networks are isolated; WireMock exceptions are justified and time-bounded.",
    "Project structure recommendations respect the existing Code Reacher evidence.",
    "All planning documents pass deterministic validation and match their SQLite projections.",
)

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cell(value: object) -> str:
    text = str(value).strip().replace("|", r"\|")
    return re.sub(r"\s*\n\s*", " ", text) or "—"


def _prose(value: object) -> str:
    """Normalise a model-supplied prose field to one string, or "" when unusable.

    Models naturally return multi-point fields (responsibilities, evidence) as a JSON array of
    strings. Accept that shape and join it; reject empty values, objects and nested structures so
    malformed output still fails validation instead of being silently stringified.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list | tuple) and value and all(isinstance(part, str) for part in value):
        parts = [part.strip() for part in value if part.strip()]
        return "; ".join(parts)
    return ""


_NO_MOCK = re.compile(
    r"\b(?:do not|does not|don't|never|no|not|avoid|without|prohibit\w*|forbid\w*|disallow\w*)\b[^.\n]{0,80}?\bmock",
    re.IGNORECASE,
)


def _states_no_mock_policy(value: object) -> bool:
    """True when the text prohibits mocks (a negation shortly before "mock" in the same sentence).

    Wording varies ("Do not mock...", "No routine mocks...", "Never use mocks..."), so match the
    prohibition itself rather than fixed phrases; text that only mentions mocks or endorses them fails.
    """
    return isinstance(value, str) and bool(_NO_MOCK.search(value))


def _items(values: list[str]) -> str:
    return "<br>".join(f"{index}. {_cell(value)}" for index, value in enumerate(values, start=1))


def _table(headers: tuple[str, ...], rows: list[list[object]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Planner refuses to replace a symbolic link.", details=(str(path),))
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:70] or "component"


def _overlap(left: str, right: str) -> bool:
    a = left.replace("\\", "/").strip("/")
    b = right.replace("\\", "/").strip("/")
    if a.endswith("/**"):
        a = a[:-3]
    if b.endswith("/**"):
        b = b[:-3]
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _topological(stories: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    by_key = {item["key"]: item for item in stories}
    dependencies = {key: list(item["dependencies"]) for key, item in by_key.items()}
    for key, deps in dependencies.items():
        unknown = sorted(set(deps) - set(by_key))
        if unknown:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Planner dependency refers to an unknown story key.", details=(key, *unknown))
        if key in deps or len(deps) != len(set(deps)):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Planner dependency list contains a self-edge or duplicate.", details=(key,))
    order: list[str] = []
    depth: dict[str, int] = {}
    remaining = {key: set(value) for key, value in dependencies.items()}
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if not deps)
        if not ready:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Planner story graph contains a dependency cycle.", details=tuple(sorted(remaining)))
        for key in ready:
            depth[key] = 1 + max((depth[dep] for dep in dependencies[key]), default=0)
            order.append(key)
            del remaining[key]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order, depth


class PlannerService:
    """Owns the Phase 13 lifecycle; model proposals are never authoritative until validated."""

    def __init__(self, workspace: WorkspaceInfo, *, runner: Any | None = None) -> None:
        self.workspace = workspace
        self.project_root = workspace.project_root.resolve()
        self.state_root = workspace.root.resolve()
        self.runner = runner

    def _directory(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "planner session_id must look like planner-0123456789ab.")
        return self.state_root / "artifacts" / "planner" / session_id

    def _paths(self, session_id: str) -> tuple[Path, Path]:
        folder = self._directory(session_id)
        return folder / "planning.json", folder / "PLANNING-NOTES.md"

    def _notes(self, payload: dict[str, Any]) -> str:
        counts = {
            "stories": len((payload.get("indexer") or {}).get("stories", [])),
            "components": len((payload.get("components") or {}).get("components", [])),
            "test_phases": len((payload.get("testing") or {}).get("phases", [])),
        }
        clarifications = [
            f"- **Q:** {item['question']}; **A:** {item['answer']} (source: {item['source']}; {item['recorded_at']})"
            for item in payload.get("clarifications", [])
        ]
        if not clarifications:
            clarifications = ["- None."]
        lines = [
            "# Planner — Staging Notes",
            "",
            "> **STAGING — NOT THE CANONICAL PLAN**",
            "",
            f"**Session:** {payload['session_id']}",
            f"**Status:** {payload['status']}",
            f"**Technical Designer Session:** {payload['designer_session_id']}",
            f"**Updated:** {payload['updated_at']}",
            "",
            "## Clarifications",
            *clarifications,
            "",
            "## Approved baseline",
            f"- TDD: {payload['source']['tdd_path']}",
            f"- TDD revision: {payload['source']['tdd_revision_id']}",
            f"- TDD SHA-256: {payload['source']['tdd_sha256']}",
            f"- Use Case revision: {payload['source']['use_case_revision_id']}",
            f"- Use Case SHA-256: {payload['source']['use_case_sha256']}",
            "",
            "## Persisted discovery",
            f"- Story proposals: {counts['stories']}",
            f"- Component plans: {counts['components']}",
            f"- Test phases: {counts['test_phases']}",
            f"- Code index run: {payload.get('code_index', {}).get('run_id', 'not required for a new codebase')}",
        ]
        for name, key in (("Indexer", "indexer"), ("Component Planner", "components"), ("Testing Planner", "testing")):
            item = payload.get(key)
            if isinstance(item, dict):
                lines.extend(["", f"## {name}", _cell(item.get("summary", "Proposal persisted."))])
                blockers = item.get("blockers", [])
                if blockers:
                    lines.extend(["", "Blockers:", *[f"- {value}" for value in blockers]])
        error = payload.get("last_error")
        if error:
            lines.extend(["", "## Last error", _cell(error)])
        for key, heading in (("indexer", "Central Plan Indexer"), ("components", "Component Planner"), ("testing", "Testing Planner")):
            proposal = payload.get(key)
            blockers = proposal.get("blockers", []) if isinstance(proposal, dict) else []
            if blockers:
                lines.extend(["", f"## {heading} blockers", *[f"- {_cell(value)}" for value in blockers]])
        worker_error = payload.get("worker_error")
        if isinstance(worker_error, dict):
            lines.extend([
                "", "## Last worker failure",
                f"- Agent: {worker_error.get('agent', 'unknown')}",
                f"- Code: {worker_error.get('code', 'unknown')}",
                f"- Retryable: {worker_error.get('retryable', False)}",
                f"- Diagnostic: {_cell(worker_error.get('message', ''))}",
                f"- Recovery: {_cell(worker_error.get('recovery', 'Retry after correcting the provider/configuration issue.'))}",
                *[f"- Detail: {_cell(value)}" for value in worker_error.get("details", [])],
            ])
        lines.extend(["", "## Resume", f"Run planner status/generate with session {payload['session_id']}.", ""])
        return "\n".join(lines)

    def _save(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = _now()
        json_path, notes_path = self._paths(payload["session_id"])
        _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _atomic_write(notes_path, self._notes(payload))

    def _load(self, session_id: str) -> dict[str, Any]:
        json_path, notes_path = self._paths(session_id)
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Planner session is missing or unreadable.", details=(session_id,)) from None
        if not isinstance(payload, dict) or payload.get("schema") != SESSION_SCHEMA or payload.get("schema_version") != SESSION_VERSION:
            raise DridhError(ErrorCode.SCHEMA_UNSUPPORTED, "Unsupported Planner session schema.", details=(session_id,))
        if payload.get("session_id") != session_id or not notes_path.is_file():
            raise DridhError(ErrorCode.STATE_CORRUPT, "Planner session projections are incomplete.", details=(session_id,))
        if payload.get("status") not in {"INDEXING", "INQUIRY", "COMPONENT_PLANNING", "TEST_PLANNING", "GENERATED", "BLOCKED", "STALE"}:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Planner session status is invalid.", details=(session_id,))
        if not isinstance(payload.get("designer_session_id"), str) or not isinstance(payload.get("source"), dict):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Planner session baseline is malformed.", details=(session_id,))
        if notes_path.read_text(encoding="utf-8") != self._notes(payload):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Planner JSON and Markdown staging notes are out of sync.", details=(session_id,))
        return payload

    def _runner(self) -> Any:
        if self.runner is not None:
            return self.runner
        env = {
            key: os.environ[key]
            for key in ("OPENCODE_CONFIG_DIR", "OPENCODE_PROVIDER_CREDENTIAL", "OPENCODE_AUTH_FILE", "OPENROUTER_API_KEY")
            if os.environ.get(key)
        }
        self.runner = OpenCodeRunner(
            executable=os.environ.get("DRIDH_OPENCODE_BIN", "opencode"),
            workspace_root=self.project_root,
            config_dir=os.environ.get("OPENCODE_CONFIG_DIR"),
            timeout=float(os.environ.get("DRIDH_PLANNER_WORKER_TIMEOUT", "600")),
            env=env,
        )
        return self.runner

    def _worker(
        self, agent: str, prompt: str, *, model: str | None = None,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> RepairOutcome:
        """Run a worker until its reply passes ``validate`` (at most three repairs, plan rule 9)."""

        def run(call_prompt: str, **kwargs: Any) -> Any:
            return self._runner().run_sync(
                agent=agent, prompt=call_prompt, model=model, title=f"Dridh Planner: {agent}", **kwargs,
            )

        return call_with_repair(run, prompt, label=_WORKER_LABELS.get(agent, agent), validate=validate)

    def _preflight(self, designer_session_id: str) -> tuple[dict[str, Any], Any, Any, dict[str, Any]]:
        gate = DesignerService(self.workspace).gate(designer_session_id)
        if not gate.get("unlocked"):
            raise DridhError(
                ErrorCode.DEPENDENCY_NOT_READY,
                "Planner is blocked because the current TDD has not passed the revision-bound human approval gate.",
                details=(str(gate.get("reason", "")),),
                recovery="Complete Technical Designer inquiry, generate the TDD, validate it and approve its exact revision in a terminal.",
            )
        tdd_path = (self.project_root / gate["tdd_path"]).resolve()
        if not tdd_path.is_relative_to(self.project_root) or not tdd_path.is_file() or tdd_path.is_symlink():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The approved TDD path is not a regular file inside the project.")
        tdd_text = tdd_path.read_text(encoding="utf-8")
        if content_hash(tdd_text) != gate["tdd_sha256"]:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The approved TDD changed before Planner preflight.")
        tdd = validated_tdd_model(tdd_text, tdd_path.name)
        use_case_path = self.project_root / "001-USE-CASES.md"
        if not use_case_path.is_file() or use_case_path.is_symlink():
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The approved canonical 001-USE-CASES.md file is missing.")
        use_case_text = use_case_path.read_text(encoding="utf-8")
        use_case_sha256 = content_hash(use_case_text)
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            documents = DocumentRepository(store.connection, self.workspace.workspace_id)
            use_meta = documents.document_metadata("001-USE-CASES.md")
            use_revision = documents.current_revision("001-USE-CASES.md")
            tdd_meta = documents.document_metadata(gate["tdd_path"])
            tdd_revision = documents.current_revision(gate["tdd_path"])
            if use_meta is None or use_revision is None or use_revision.id != gate["source_use_case_revision_id"] or use_revision.content_sha256 != use_case_sha256:
                raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The approved Use Case revision is not current in SQLite.")
            if tdd_meta is None or tdd_revision is None or tdd_revision.id != gate["tdd_revision_id"]:
                raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The approved TDD revision is not current in SQLite.")
            use_repo = UseCaseRepository(store.connection, self.workspace.workspace_id)
            use_findings = use_repo.projection_findings(
                relative_path="001-USE-CASES.md",
                document_id=str(use_meta["id"]),
                current_revision_id=use_revision.id,
                current_content=documents.stored_content(use_revision.id),
            )
            if use_findings:
                raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The approved Use Case projection is stale.", details=tuple(item.message for item in use_findings[:5]))
            use_cases = use_repo.stored_model(str(use_meta["id"]))
            code_index: dict[str, Any] = {"required": self.workspace.mode == "existing", "run_id": None, "source_sha": None, "files": []}
            if self.workspace.mode == "existing":
                run = store.connection.execute(
                    """SELECT r.id, r.source_sha, r.repository_path, r.files_seen, r.files_indexed
                       FROM code_index_run r
                       WHERE r.workspace_id=? AND r.status='SUCCEEDED'
                       ORDER BY r.completed_at DESC, r.started_at DESC LIMIT 1""",
                    (self.workspace.workspace_id,),
                ).fetchone()
                if run is None or int(run["files_indexed"]) < 1:
                    raise DridhError(
                        ErrorCode.DEPENDENCY_NOT_READY,
                        "Planner requires a successful Code Reacher index for an existing repository.",
                        recovery="Run the Code Reacher indexing workflow and retry Planner. Do not substitute guessed repository structure.",
                    )
                code_index.update({"run_id": run["id"], "source_sha": run["source_sha"], "repository_path": run["repository_path"],
                                   "files_seen": run["files_seen"], "files_indexed": run["files_indexed"]})
                rows = store.connection.execute(
                    """SELECT f.path, f.language, COUNT(e.id) AS symbols
                       FROM code_file f LEFT JOIN code_entity e ON e.file_id=f.id
                       WHERE f.index_run_id=? GROUP BY f.id ORDER BY f.path LIMIT 250""",
                    (run["id"],),
                ).fetchall()
                code_index["files"] = [dict(row) for row in rows]
            source = {
                "tdd_path": gate["tdd_path"],
                "tdd_revision_id": gate["tdd_revision_id"],
                "tdd_sha256": gate["tdd_sha256"],
                "use_case_revision_id": gate["source_use_case_revision_id"],
                "use_case_sha256": use_case_sha256,
                "approved_by": gate["approved_by"],
                "approved_at": gate["approved_at"],
            }
            return source, tdd, use_cases, code_index

    def _branch_slug(self) -> str:
        """Return a deterministic, branch-scoped identifier for Compose isolation."""
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.project_root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            branch = completed.stdout.strip() if completed.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            branch = ""
        raw = branch if branch and branch != "HEAD" else f"workspace-{self.workspace.workspace_id}"
        slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:32]
        return slug or "workspace"

    def _baseline(self, source: dict[str, Any], tdd: Any, use_cases: Any, code_index: dict[str, Any]) -> dict[str, Any]:
        return {
            "branch_slug": self._branch_slug(),
            "source": source,
            "components": [{"id": c.component_id, "name": c.component_name, "definition": c.definition_text} for c in tdd.components],
            "use_cases": [{"id": u.use_case_id, "title": u.title, "actor": u.primary_actor_id} for u in use_cases.use_cases],
            "flows": [{"id": f.flow_id, "use_case_id": f.parent_use_case_id, "type": f.flow_type, "subject": f.subject,
                       "actor": f.actor_id, "action": f.action_text, "result": f.result_text, "origin": f.origin_step_flow_id}
                      for f in use_cases.flows],
            "tdd_traceability": [{"use_case_id": m.use_case_id, "flow_id": m.flow_id, "component_id": m.component_id,
                                  "decision_id": m.design_decision_id} for m in tdd.mappings],
            "code_index": code_index,
        }

    def _validate_indexer(self, result: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        stories = result.get("stories")
        if not isinstance(stories, list) or not stories:
            raise DridhError(ErrorCode.WORKER_FAILED, "Central Plan Indexer must return a non-empty stories array.")
        components = {item["id"] for item in baseline["components"]}
        flows = {item["id"]: item["use_case_id"] for item in baseline["flows"]}
        mapped_components: dict[str, set[str]] = {}
        for item in baseline["tdd_traceability"]:
            if item["flow_id"]:
                mapped_components.setdefault(item["flow_id"], set()).add(item["component_id"])
        keys: set[str] = set()
        normalised: list[dict[str, Any]] = []
        covered: set[str] = set()
        for index, raw in enumerate(stories):
            if not isinstance(raw, dict):
                raise DridhError(ErrorCode.WORKER_FAILED, "Each story proposal must be an object.", details=(str(index),))
            key = raw.get("key")
            if not isinstance(key, str) or not STORY_KEY.fullmatch(key) or key in keys:
                raise DridhError(ErrorCode.WORKER_FAILED, "Story proposal keys must be unique lowercase slugs.", details=(str(key),))
            keys.add(key)
            component = raw.get("component_id")
            if component not in components:
                raise DridhError(
                    ErrorCode.WORKER_FAILED,
                    "Story proposal references a component not defined in the approved TDD.",
                    details=(key, str(component), "valid component_id values: " + ", ".join(sorted(components))),
                )
            title = raw.get("title")
            description = raw.get("description")
            if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not description.strip():
                raise DridhError(ErrorCode.WORKER_FAILED, "Every story needs a title and description.", details=(key,))
            use_case_ids = raw.get("use_case_ids")
            flow_ids = raw.get("flow_ids")
            if not isinstance(use_case_ids, list) or not all(isinstance(v, str) and UC_ID.fullmatch(v) for v in use_case_ids):
                raise DridhError(ErrorCode.WORKER_FAILED, "Story Use Case IDs must be an array of UC-NN identifiers.", details=(key,))
            if not isinstance(flow_ids, list) or not all(isinstance(v, str) and FLOW_ID.fullmatch(v) for v in flow_ids):
                raise DridhError(ErrorCode.WORKER_FAILED, "Story Flow IDs must be an array of canonical flow identifiers.", details=(key,))
            infrastructure = raw.get("infrastructure", False)
            if not isinstance(infrastructure, bool):
                raise DridhError(ErrorCode.WORKER_FAILED, "Story infrastructure classification must be boolean.", details=(key,))
            rationale = raw.get("infrastructure_rationale", "")
            if infrastructure:
                if flow_ids or use_case_ids or not isinstance(rationale, str) or not rationale.strip():
                    raise DridhError(ErrorCode.WORKER_FAILED, "Infrastructure stories must use N/A traceability and explain why.", details=(key,))
            else:
                if not flow_ids or not use_case_ids:
                    raise DridhError(ErrorCode.WORKER_FAILED, "A feature story must map to at least one approved Use Case and flow.", details=(key,))
                if set(use_case_ids) != {flows.get(flow) for flow in flow_ids}:
                    raise DridhError(ErrorCode.WORKER_FAILED, "Story Use Case IDs must exactly match the parent Use Cases of its flows.", details=(key,))
                if any(component not in mapped_components.get(flow, set()) for flow in flow_ids):
                    raise DridhError(ErrorCode.WORKER_FAILED, "Story component does not match the approved TDD flow-to-component traceability.", details=(key,))
                covered.update(flow_ids)
            for field in ("todos", "acceptance_criteria", "definition_of_done", "dependencies", "file_ownership", "resource_ownership"):
                if not isinstance(raw.get(field), list) or not all(isinstance(v, str) and v.strip() for v in raw[field]):
                    raise DridhError(ErrorCode.WORKER_FAILED, f"Story field {field} must be a string array.", details=(key,))
            if not infrastructure and not raw["file_ownership"]:
                raise DridhError(ErrorCode.WORKER_FAILED, "Feature stories must declare concrete file ownership.", details=(key,))
            for owned in raw["file_ownership"]:
                normalized_path = owned.strip().replace("\\", "/")
                if normalized_path.startswith("/") or re.match(r"^[A-Za-z]:", normalized_path) or ".." in Path(normalized_path).parts:
                    raise DridhError(ErrorCode.WORKER_FAILED, "Story file ownership must use safe project-relative paths.", details=(key, normalized_path))
            rationale_map = raw.get("dependency_rationale", {})
            if not isinstance(rationale_map, dict):
                raise DridhError(ErrorCode.WORKER_FAILED, "dependency_rationale must be an object keyed by dependency story key.", details=(key,))
            if set(raw["dependencies"]) != set(rationale_map):
                raise DridhError(ErrorCode.WORKER_FAILED, "Every dependency needs a specific rationale and no extra rationale keys.", details=(key,))
            if any(not isinstance(value, str) or not value.strip() for value in rationale_map.values()):
                raise DridhError(ErrorCode.WORKER_FAILED, "Dependency rationale values must be non-empty.", details=(key,))
            normalised.append({
                **raw,
                "key": key,
                "component_id": component,
                "title": title.strip(),
                "description": description.strip(),
                "use_case_ids": sorted(set(use_case_ids)),
                "flow_ids": sorted(set(flow_ids)),
                "todos": [value.strip() for value in raw["todos"]],
                "acceptance_criteria": [value.strip() for value in raw["acceptance_criteria"]],
                "definition_of_done": [value.strip() for value in raw["definition_of_done"]],
                "dependencies": list(raw["dependencies"]),
                "dependency_rationale": rationale_map,
                "file_ownership": [value.strip().replace("\\", "/") for value in raw["file_ownership"]],
                "resource_ownership": [value.strip() for value in raw["resource_ownership"]],
                "infrastructure": infrastructure,
                "infrastructure_rationale": rationale.strip() if isinstance(rationale, str) else "",
            })
        if len(keys) != len(stories):
            raise DridhError(ErrorCode.WORKER_FAILED, "Story proposal keys are not unique.")
        missing = sorted(set(flows) - covered)
        if missing:
            raise DridhError(ErrorCode.WORKER_FAILED, "Approved Use Case flows are not covered by implementation stories.", details=tuple(missing))
        for key, story in ((item["key"], item) for item in normalised):
            unknown = sorted(set(story["dependencies"]) - keys)
            if unknown:
                raise DridhError(ErrorCode.WORKER_FAILED, "Story references an unknown dependency key.", details=(key, *unknown))
        order, depths = _topological(normalised)
        return {
            "summary": str(result.get("summary", "Central story map validated."))[:2000],
            "blockers": result.get("blockers", []) if isinstance(result.get("blockers", []), list) else [],
            "stories": normalised,
            "order": order,
            "depths": depths,
            "project_structure": result.get("project_structure", {}),
        }

    def _record_worker_failure(self, payload: dict[str, Any], agent: str, error: Exception) -> None:
        """Persist a redacted provider/worker failure without discarding completed proposals."""
        raw_code = getattr(error, "code", ErrorCode.WORKER_FAILED)
        code = getattr(raw_code, "value", str(raw_code))
        message = str(redact(getattr(error, "message", str(error))))[:2000]
        details = [str(redact(value))[:1000] for value in getattr(error, "details", ())]
        payload["worker_error"] = {
            "agent": agent,
            "code": code,
            "message": message,
            "details": details,
            "retryable": bool(getattr(error, "retryable", False)),
            "recovery": str(redact(getattr(error, "recovery", "Correct the provider/configuration issue and retry the same Planner session.")))[:1000],
            "recorded_at": _now(),
        }
        payload["status"] = "BLOCKED"
        payload["last_error"] = f"{agent} failed ({code}): {message}"[:2000]
        self._save(payload)

    def _invoke_worker(
        self, payload: dict[str, Any], agent: str, prompt: str, *, model: str | None = None,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any | None:
        """Invoke a worker; return its validated reply, or None after recording a resumable failure.

        Content problems are repaired inside the worker loop. Provider failures and an exhausted repair
        budget are persisted as ``worker_error`` so the session stays BLOCKED and resumable.
        """
        try:
            outcome = self._worker(agent, prompt, model=model, validate=validate)
        except (AgentRunError, DridhError) as exc:
            self._record_worker_failure(payload, agent, exc)
            return None
        payload.pop("worker_error", None)
        if outcome.attempts:
            payload.setdefault("repair_history", []).append({
                "agent": agent,
                "recorded_at": _now(),
                "attempts": [attempt.to_dict() for attempt in outcome.attempts],
            })
        return outcome.value

    def _indexer_prompt(self, baseline: dict[str, Any], payload: dict[str, Any]) -> str:
        """Build a prompt with instructions first and the canonical JSON input last."""
        parts = [
            "You are the Phase 13 Central Plan Indexer. Return JSON only. Decompose the approved TDD into independently executable, "
            "small stories. Preserve all approved Use Cases and their flows; every supplied flow must be covered. Components must match "
            "the supplied canonical CMP IDs and flow-to-component matrix. Do not invent codebase facts. For each story return: key "
            "(unique lowercase slug), component_id, title, description, use_case_ids, flow_ids, todos (ordered strings), acceptance_criteria "
            "(ordered strings), definition_of_done (ordered strings), dependencies (other story keys), dependency_rationale (object keyed "
            "by every dependency key), file_ownership (explicit relative paths/globs), resource_ownership (shared contracts, schemas, migrations, DBs or environments), infrastructure (boolean) and infrastructure_rationale. "
            "Every story, including infrastructure stories, MUST set component_id to exactly one supplied CMP ID; it is never null or omitted. For cross-cutting infrastructure work choose the supplied component that owns or is most affected by it. "
            "Use infrastructure=true only for genuine cross-cutting setup/operations with no UC/flow mapping. Also return summary, blockers "
            "and project_structure {summary, recommendations}. Do not silently resolve ambiguities; list genuine blockers explicitly."
        ]
        code_index = baseline.get("code_index", {})
        if not code_index.get("required", False):
            parts.append(
                "NEW CODEBASE MODE: The approved baseline intentionally has no existing repository index. This absence is expected "
                "and MUST NOT be reported as a blocker. Do not invent files, frameworks, languages or existing services. Plan an "
                "expected project structure from the approved TDD. Put implementation choices that need later confirmation into "
                "assumptions/recommendations, not blockers. Report blockers only for contradictions or missing approved Use Case, flow "
                "or component traceability. Every flow in the supplied baseline is approved and must be covered."
            )
        else:
            parts.append(
                "EXISTING CODEBASE MODE: Use the supplied successful Code Reacher index as the only repository evidence. Do not "
                "request or invent repository facts outside that index."
            )
        clarifications = payload.get("clarifications", [])
        if clarifications:
            parts.append(
                "HUMAN CLARIFICATIONS (apply without changing approved requirements): "
                + json.dumps(clarifications, ensure_ascii=False)
            )
        # Repeat the canonical ownership matrix outside the full JSON block so the
        # model has an unambiguous, concise authority for CMP IDs and flow mapping.
        component_matrix = [
            {
                "component_id": item["id"],
                "canonical_name": item.get("name", item["id"]),
                "approved_flows": sorted({
                    mapping["flow_id"]
                    for mapping in baseline.get("tdd_traceability", [])
                    if mapping.get("component_id") == item["id"] and mapping.get("flow_id")
                }),
            }
            for item in baseline.get("components", [])
        ]
        parts.append(
            "AUTHORITATIVE COMPONENT ID AND FLOW OWNERSHIP MATRIX (copy component_id values exactly; "
            "canonical_name is descriptive only):\n" + json.dumps(component_matrix, ensure_ascii=False)
        )
        parts.append("APPROVED INPUT (canonical JSON):\n" + json.dumps(baseline, ensure_ascii=False))
        return "\n\n".join(parts)

    def _run_indexer(
        self, payload: dict[str, Any], source: dict[str, Any], tdd: Any, use_cases: Any,
        code_index: dict[str, Any], *, model: str | None = None,
    ) -> dict[str, Any]:
        baseline = self._baseline(source, tdd, use_cases, code_index)
        payload["status"] = "INDEXING"
        payload["last_error"] = None
        self._save(payload)
        indexer = self._invoke_worker(
            payload, "dridh/workers/planner-indexer", self._indexer_prompt(baseline, payload), model=model,
            validate=lambda reply: self._validate_indexer(reply, baseline),
        )
        if indexer is None:
            return self.status(payload["session_id"])
        payload["indexer"] = indexer
        blockers = payload["indexer"].get("blockers", [])
        payload["status"] = "BLOCKED" if blockers else "INQUIRY"
        payload["last_error"] = "Central Plan Indexer reported unresolved blockers." if blockers else None
        payload.pop("worker_error", None)
        self._save(payload)
        return self.status(payload["session_id"])

    def start(self, designer_session_id: str, *, model: str | None = None) -> dict[str, Any]:
        if not isinstance(designer_session_id, str) or not designer_session_id:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "designer_session_id is required.")
        for folder in (self.state_root / "artifacts" / "planner").glob("planner-*"):
            try:
                existing = self._load(folder.name)
            except DridhError:
                continue
            if existing.get("designer_session_id") == designer_session_id:
                if existing.get("indexer") is None and existing.get("status") in {"INDEXING", "BLOCKED"}:
                    source, tdd, use_cases, code_index = self._preflight(designer_session_id)
                    source_pairs = (
                        ("tdd_revision_id", "tdd_revision_id"),
                        ("tdd_sha256", "tdd_sha256"),
                        ("use_case_revision_id", "use_case_revision_id"),
                        ("use_case_sha256", "use_case_sha256"),
                    )
                    if any(source[current] != existing["source"][saved] for current, saved in source_pairs):
                        existing["status"] = "STALE"
                        existing["last_error"] = "The approved source revision changed while this Planner session was interrupted."
                        self._save(existing)
                        return self.status(folder.name)
                    return self._run_indexer(existing, source, tdd, use_cases, code_index, model=model)
                return self.status(folder.name)
        source, tdd, use_cases, code_index = self._preflight(designer_session_id)
        session_id = f"planner-{uuid.uuid4().hex[:12]}"
        payload: dict[str, Any] = {
            "schema": SESSION_SCHEMA,
            "schema_version": SESSION_VERSION,
            "session_id": session_id,
            "status": "INDEXING",
            "designer_session_id": designer_session_id,
            "feature": Path(source["tdd_path"]).stem.replace("_tdd", "").replace("_", " "),
            "source": source,
            "code_index": code_index,
            "clarifications": [],
            "indexer": None,
            "components": None,
            "testing": None,
            "identifiers": None,
            "generated": None,
            "created_at": _now(),
            "updated_at": _now(),
            "last_error": None,
            "worker_error": None,
        }
        self._save(payload)
        return self._run_indexer(payload, source, tdd, use_cases, code_index, model=model)

    def status(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        return {
            "session_id": session_id,
            "designer_session_id": payload["designer_session_id"],
            "status": payload["status"],
            "source": payload["source"],
            "code_index": payload["code_index"],
            "stories": len((payload.get("indexer") or {}).get("stories", [])),
            "components": len((payload.get("components") or {}).get("components", [])),
            "test_phases": len((payload.get("testing") or {}).get("phases", [])),
            "indexer_summary": (payload.get("indexer") or {}).get("summary"),
            "indexer_blockers": (payload.get("indexer") or {}).get("blockers", []),
            "component_blockers": (payload.get("components") or {}).get("blockers", []),
            "testing_blockers": (payload.get("testing") or {}).get("blockers", []),
            "generated": payload.get("generated"),
            "jev_checkpoint": payload.get("jev_checkpoint"),
            "last_error": payload.get("last_error"),
            "worker_error": payload.get("worker_error"),
            "clarifications": payload.get("clarifications", []),
            "resumable": payload["status"] not in {"GENERATED", "STALE"},
        }

    def _component_output(self, result: dict[str, Any], component_ids: set[str]) -> dict[str, Any]:
        rows = result.get("components")
        if not isinstance(rows, list):
            raise DridhError(ErrorCode.WORKER_FAILED, "Component Planner must return a components array.")
        by_id: dict[str, dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict) or item.get("component_id") not in component_ids:
                raise DridhError(ErrorCode.WORKER_FAILED, "Component Planner returned an unknown component.")
            cid = item["component_id"]
            if cid in by_id:
                raise DridhError(ErrorCode.WORKER_FAILED, "Component Planner returned a duplicate component.", details=(cid,))
            normalised = {"component_id": cid}
            for field in ("summary", "responsibilities", "boundaries", "dependencies"):
                text = _prose(item.get(field))
                if not text:
                    raise DridhError(
                        ErrorCode.WORKER_FAILED,
                        f"Component {cid} needs {field} as a non-empty string (or an array of strings).",
                    )
                normalised[field] = text
            by_id[cid] = normalised
        missing = sorted(component_ids - set(by_id))
        if missing:
            raise DridhError(ErrorCode.WORKER_FAILED, "Component Planner did not cover every actionable TDD component.", details=tuple(missing))
        return {"summary": str(result.get("summary", "Component plans validated."))[:2000], "blockers": result.get("blockers", []), "components": [by_id[cid] for cid in sorted(by_id)]}

    def _testing_output(self, result: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        phases = result.get("phases")
        if not isinstance(phases, list) or not phases:
            raise DridhError(ErrorCode.WORKER_FAILED, "Testing Planner must return at least one Table 15 test phase.")
        known_uc = {item["id"] for item in baseline["use_cases"]}
        known_flow = {item["id"]: item["use_case_id"] for item in baseline["flows"]}
        known_cmp = {item["id"] for item in baseline["components"]}
        mapped_components: dict[str, set[str]] = {}
        for mapping in baseline.get("tdd_traceability", []):
            if mapping.get("flow_id"):
                mapped_components.setdefault(mapping["flow_id"], set()).add(mapping["component_id"])
        covered: set[str] = set()
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        self._compose_names: set[str] = set()
        for item in phases:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase proposals need unique slug keys.")
            # Test-phase keys are internal handles (Coordinator allocates the real Table 15 IDs), so
            # normalise harmless variants such as "Happy_Path" or "TP 01" to the lowercase slug form.
            item = {**item, "key": re.sub(r"[^a-z0-9]+", "-", item["key"].lower()).strip("-")[:80].strip("-")}
            if not STORY_KEY.fullmatch(item["key"]):
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase proposals need unique slug keys.")
            if item["key"] in seen:
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase keys must be unique.", details=(item["key"],))
            seen.add(item["key"])
            mode = item.get("dependency_mode")
            if mode not in TEST_MODES:
                raise DridhError(
                    ErrorCode.WORKER_FAILED,
                    "Testing Planner proposed an unsupported Dependency Mode.",
                    details=(str(mode), "allowed dependency_mode values: " + ", ".join(sorted(TEST_MODES))),
                )
            ucs, flows, comps = item.get("use_case_ids"), item.get("flow_ids"), item.get("component_ids")
            if not isinstance(ucs, list) or not isinstance(flows, list) or not isinstance(comps, list):
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phases must map Use Cases, flows and components.", details=(item["key"],))
            if any(value not in known_uc for value in ucs) or any(value not in known_flow for value in flows) or any(value not in known_cmp for value in comps):
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase references an unknown Use Case, flow or component.", details=(item["key"],))
            if flows and set(ucs) != {known_flow[flow] for flow in flows}:
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase Use Cases must match the parent of each supplied flow.", details=(item["key"],))
            if not flows or not comps:
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase needs at least one approved flow and component.", details=(item["key"],))
            expected_components = set().union(*(mapped_components.get(flow, set()) for flow in flows))
            if set(comps) != expected_components:
                raise DridhError(ErrorCode.WORKER_FAILED, "Test phase component IDs must exactly match the TDD component mappings for its flows.", details=(item["key"],))
            covered.update(flows)
            item = dict(item)
            for field in ("test_scope", "environment", "evidence"):
                item[field] = _prose(item.get(field))
            if not isinstance(item.get("execution_command"), str):
                item["execution_command"] = ""
            for field in ("test_scope", "environment", "execution_command", "evidence"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise DridhError(ErrorCode.WORKER_FAILED, f"Test phase {item['key']} needs {field} as a non-empty string.")
            if "\n" in item["execution_command"] or len(item["execution_command"]) > 1000:
                raise DridhError(ErrorCode.WORKER_FAILED, "Test execution commands must be one line and bounded.", details=(item["key"],))
            if mode == "WIREMOCK_EXCEPTION":
                env = item["environment"].lower()
                required = ("dependency:", "contract:", "owner:", "expires:", "container: unavailable", "qa: unavailable")
                if not all(token in env for token in required):
                    raise DridhError(ErrorCode.WORKER_FAILED, "WireMock fallback must document the dependency, contract, owner, expiry and why container/QA alternatives are unavailable.", details=(item["key"],))
                if not isinstance(item.get("fallback_rationale"), str) or not item["fallback_rationale"].strip():
                    raise DridhError(ErrorCode.WORKER_FAILED, "WireMock fallback needs an explicit reason.", details=(item["key"],))
            if mode == "DOCKER_COMPOSE":
                environment = item["environment"].lower()
                compose_names = re.findall(r"(?:project|network)\s*:\s*([a-z0-9_-]+)", environment)
                if "branch:" not in environment or not re.search(r"testcontainers:\s*(?:unsuitable|not available)", environment):
                    raise DridhError(ErrorCode.WORKER_FAILED, "Compose fallback must record why Testcontainers is unsuitable and identify the branch scope.", details=(item["key"],))
                branch_slug = str(baseline.get("branch_slug", ""))
                if not branch_slug or not compose_names or not any(branch_slug in name for name in compose_names):
                    raise DridhError(ErrorCode.WORKER_FAILED, "Compose project/network names must include the current Git branch slug.", details=(item["key"],))
                if len(set(compose_names)) != len(compose_names) or any(name in self._compose_names for name in compose_names):
                    raise DridhError(ErrorCode.WORKER_FAILED, "Compose project/network names must be unique across test phases.", details=(item["key"],))
                self._compose_names.update(compose_names)
            output.append({
                "key": item["key"], "test_scope": item["test_scope"].strip(),
                "use_case_ids": sorted(set(ucs)), "flow_ids": sorted(set(flows)),
                "component_ids": sorted(set(comps)), "dependency_mode": mode,
                "environment": item["environment"].strip(), "execution_command": item["execution_command"].strip(),
                "evidence": item["evidence"].strip(), "fallback_rationale": str(item.get("fallback_rationale", "")).strip(),
                "service_inventory": str(item.get("service_inventory", "Use actual service containers or a named QA endpoint." )).strip(),
                "parallel_isolation": str(item.get("parallel_isolation", "Use isolated test data and unique container/network resources." )).strip(),
            })
        missing = sorted(set(known_flow) - covered)
        if missing:
            raise DridhError(ErrorCode.WORKER_FAILED, "Table 15 does not cover every approved Use Case flow.", details=tuple(missing))
        mock_policy = result.get("mock_policy", "Do not mock application external dependencies as the normal strategy.")
        if not _states_no_mock_policy(mock_policy):
            raise DridhError(
                ErrorCode.WORKER_FAILED,
                "Testing Planner must state the no-mock policy explicitly.",
                details=(f"mock_policy received: {str(mock_policy)[:300]}",),
            )
        blockers = result.get("blockers", [])
        if not isinstance(blockers, list) or not all(isinstance(value, str) for value in blockers):
            raise DridhError(ErrorCode.WORKER_FAILED, "Testing Planner blockers must be an array of strings.")
        return {"summary": str(result.get("summary", "Test plan validated."))[:2000], "blockers": blockers, "phases": output,
                "mock_policy": mock_policy.strip()}

    def _parallel(self, stories: list[dict[str, Any]], story_ids: dict[str, str]) -> dict[str, list[str]]:
        dependencies = {item["key"]: set(item["dependencies"]) for item in stories}
        ancestors: dict[str, set[str]] = {}
        def collect(key: str) -> set[str]:
            if key not in ancestors:
                found: set[str] = set()
                for dep in dependencies[key]:
                    found.add(dep)
                    found.update(collect(dep))
                ancestors[key] = found
            return ancestors[key]
        for key in dependencies:
            collect(key)
        partners: dict[str, list[str]] = {item["key"]: [] for item in stories}
        for index, left in enumerate(stories):
            left_key = left["key"]
            left_paths = left["file_ownership"] + left["resource_ownership"]
            if not left_paths:
                continue
            for right in stories[index + 1:]:
                right_key = right["key"]
                right_paths = right["file_ownership"] + right["resource_ownership"]
                if not right_paths or ancestors[left_key] & {right_key} or ancestors[right_key] & {left_key}:
                    continue
                if any(_overlap(a, b) for a in left_paths for b in right_paths):
                    continue
                partners[left_key].append(right_key)
                partners[right_key].append(left_key)
        return {story_ids[key]: sorted(story_ids[item] for item in values) for key, values in partners.items()}

    def _render_central(self, payload: dict[str, Any], stories: list[dict[str, Any]], story_ids: dict[str, str],
                        depths: dict[str, int], partners: dict[str, list[str]],
                        statuses: dict[str, str] | None = None) -> str:
        statuses = statuses or {}
        tdd = payload["source"]
        bundle = load_contract_bundle(STANDARDS_VERSION)
        order_keys = payload["indexer"]["order"]
        by_key = {item["key"]: item for item in stories}
        unlocks: dict[str, list[str]] = {key: [] for key in by_key}
        for key, item in by_key.items():
            for dep in item["dependencies"]:
                unlocks[dep].append(key)
        rows: list[list[object]] = []
        for ordinal, key in enumerate(order_keys, start=1):
            item = by_key[key]
            cid = item["component_id"]
            cname = next(c["name"] for c in payload["baseline"]["components"] if c["id"] == cid)
            rows.append([
                ordinal, story_ids[key], f"{cid} — {cname}", depths[key],
                ", ".join(item["use_case_ids"]) if item["use_case_ids"] else "Ops",
                item["title"],
                ", ".join(story_ids[dep] for dep in item["dependencies"]) if item["dependencies"] else "None",
                ", ".join(story_ids[dep] for dep in sorted(unlocks[key])) if unlocks[key] else "—",
                # `partners` is keyed by canonical story ID (see `_parallel`), not by the proposal slug.
                ", ".join(partners[story_ids[key]]) or "—",
                statuses.get(key, "NOT_STARTED"),
            ])
        return "\n".join([
            f"# {payload['feature']} — Central Plan Index",
            "",
            bundle.render_metadata_block(source_revision=tdd["tdd_revision_id"], source_sha256=tdd["tdd_sha256"]),
            "> **Document Status:** DRAFT  ",
            f"> **Last Updated:** {_now()[:10]}",
            "",
            "## Table 2 — Execution Order",
            "",
            _table(H_CENTRAL, rows),
            "",
            "## Dependency Rationale",
            "",
            *[
                f"- **{story_ids[item['key']]}:** " + "; ".join(
                    f"{story_ids[dep]} — {item['dependency_rationale'][dep]}" for dep in item["dependencies"]
                ) if item["dependencies"] else f"- **{story_ids[item['key']]}:** No predecessor dependency."
                for item in (by_key[key] for key in order_keys)
            ],
            "",
            "## Table 9 — Risk Register",
            "",
            _table(H_RISKS, []),
            "",
            "## Table 10 — Milestones",
            "",
            _table(H_MILESTONES, []),
            "",
        ])

    def _render_component(self, component: dict[str, Any], tdd_path: str, stories: list[dict[str, Any]],
                          story_ids: dict[str, str], depths: dict[str, int], by_key: dict[str, dict[str, Any]],
                          statuses: dict[str, str] | None = None) -> str:
        statuses = statuses or {}
        selected = [item for item in stories if item["component_id"] == component["component_id"]]
        rows = [[story_ids[item["key"]], depths[item["key"]], item["title"], ", ".join(item["use_case_ids"]) or "Ops", statuses.get(item["key"], "NOT_STARTED")] for item in selected]
        phases = sorted({depths[item["key"]] for item in selected})
        dashboard = []
        for phase in phases:
            phase_rows = [item for item in selected if depths[item["key"]] == phase]
            counts = {status: sum(1 for item in phase_rows if statuses.get(item["key"], "NOT_STARTED") == status) for status in STORY_STATUSES}
            dashboard.append([phase, len(phase_rows), *[counts[status] for status in STORY_STATUSES]])
        totals = [sum(row[1] for row in dashboard), *[sum(row[index] for row in dashboard) for index in range(2, 7)]]
        dashboard.append(["**Total**", *totals])
        anchor = f"#{component['component_id'].lower()}-{_slug(component['name'])}"
        master_dir = self.project_root / ".opencode" / "dridh" / "documents" / "components" / component["component_id"]
        relative_tdd = Path(os.path.relpath(self.project_root / tdd_path, master_dir)).as_posix()
        return "\n".join([
            f"# {component['name']} — Component Master",
            "",
            f"> **Standards Version:** {STANDARDS_VERSION}  ",
            "> **Document Status:** DRAFT  ",
            f"> **Last Updated:** {_now()[:10]}  ",
            f"> **Component:** {component['component_id']}",
            "",
            "## Component Overview",
            "",
            f"- **Component ID:** {component['component_id']}",
            f"- **Canonical TDD Definition:** [{component['component_id']} — {component['name']}]({relative_tdd}{anchor})",
            f"- **Component Name:** {component['name']}",
            "",
            "## Component Responsibilities",
            "",
            component["summary"],
            "",
            "### Ownership and Boundaries",
            "",
            component["responsibilities"],
            "",
            component["boundaries"],
            "",
            "### Dependencies",
            "",
            component["dependencies"],
            "",
            "## Table 4 — Story Index",
            "",
            _table(H_STORY_INDEX, rows),
            "",
            "## Table 5 — Phase Status Dashboard",
            "",
            _table(H_DASHBOARD, dashboard),
            "",
            "## Phase Files",
            "",
            *[f"- PHASE-{phase}-{_slug(component['name'])}.md" for phase in phases],
            "",
        ])

    def _render_phase(self, component: dict[str, Any], phase: int, stories: list[dict[str, Any]],
                      story_ids: dict[str, str], unlocks: dict[str, list[str]],
                      statuses: dict[str, str] | None = None) -> str:
        statuses = statuses or {}
        selected = [item for item in stories if item["component_id"] == component["component_id"] and item["phase"] == phase]
        lines = [
            f"# Phase {phase} — {component['name']}",
            "",
            f"> **Standards Version:** {STANDARDS_VERSION}  ",
            "> **Document Status:** DRAFT  ",
            f"> **Last Updated:** {_now()[:10]}  ",
            f"> **Component:** {component['component_id']}",
            "",
        ]
        for item in selected:
            sid = story_ids[item["key"]]
            deps = [story_ids[key] for key in item["dependencies"]]
            lines.extend([
                f"## {sid}: {item['title']}",
                "",
                _table(("Field", "Value"), [
                    ["Use Case ID", ", ".join(item["use_case_ids"]) or "N/A"],
                    ["Flow IDs", ", ".join(item["flow_ids"]) or "N/A"],
                    ["Title", item["title"]],
                    ["Description", item["description"] + ("<br><br>Dependency rationale: " + "; ".join(item["dependency_rationale"][key] for key in item["dependencies"]) if deps else "")],
                    ["Status", statuses.get(item["key"], "NOT_STARTED")],
                    ["Dependencies", ", ".join(deps) if deps else "None"],
                    ["Unlocks", ", ".join(sorted(story_ids[key] for key in unlocks[item["key"]])) or "—"],  # IDs, never model keys
                    ["What Will Be Done", _items(item["todos"])],
                    ["Acceptance Criteria", _items(item["acceptance_criteria"])],
                    ["Definition of Done", _items(item["definition_of_done"])],
                ]),
                "",
                "### File Ownership",
                "",
                ", ".join(item["file_ownership"]) or item.get("infrastructure_rationale", "Infrastructure work; no feature file ownership."),
                "",
                "### Shared Resource Ownership",
                "",
                ", ".join(item["resource_ownership"]) or "None.",
                "",
            ])
        return "\n".join(lines)

    def _render_test_plan(self, payload: dict[str, Any], phases: list[dict[str, Any]], phase_ids: dict[str, str],
                          statuses: dict[str, str] | None = None) -> str:
        statuses = statuses or {}
        rows = []
        inventory_lines = []
        isolation_lines = []
        wiremock_lines = []
        for item in phases:
            flows = ", ".join(item["flow_ids"])
            ucs = ", ".join(item["use_case_ids"])
            comps = ", ".join(item["component_ids"])
            refs = f"{ucs} ({flows})"
            rows.append([
                phase_ids[item["key"]], item["test_scope"], refs, comps, item["dependency_mode"],
                item["environment"], item["execution_command"], item["evidence"],
                statuses.get(item["key"], TEST_STATUS),
            ])
            inventory_lines.append(f"- **{phase_ids[item['key']]}:** {item['service_inventory']}")
            isolation_lines.append(f"- **{phase_ids[item['key']]}:** {item['parallel_isolation']}")
            if item["dependency_mode"] == "WIREMOCK_EXCEPTION":
                wiremock_lines.append(f"- **{phase_ids[item['key']]}:** {item['fallback_rationale']}")
        if not inventory_lines:
            inventory_lines = ["- No external services are required."]
        if not isolation_lines:
            isolation_lines = ["- No parallel test phases are defined."]
        if not wiremock_lines:
            wiremock_lines = ["- None."]
        return "\n".join([
            f"# {payload['feature']} — Test Plan",
            "",
            f"> **Standards Version:** {STANDARDS_VERSION}  ",
            "> **Document Status:** DRAFT  ",
            f"> **Last Updated:** {_now()[:10]}",
            "",
            "## Table 15 — Test Phase Plan",
            "",
            _table(H_TEST, rows),
            "",
            "## Dependency Inventory and Execution Strategy",
            "",
            *inventory_lines,
            "",
            "## Isolation and Data Lifecycle",
            "",
            *isolation_lines,
            "",
            "## No-Mock Policy",
            "",
            payload["testing"]["mock_policy"] + " Testcontainers is preferred; use branch-specific Compose with a unique project/network when containers are unsuitable; use a real QA endpoint when the dependency cannot run locally. WireMock is allowed only as a documented contract fallback when neither real dependency option is available, with owner and expiry recorded.",
            "",
            "## WireMock Exceptions",
            "",
            *wiremock_lines,
            "",
        ])

    def _render_structure(self, payload: dict[str, Any]) -> str:
        structure = payload["indexer"].get("project_structure")
        if not isinstance(structure, dict):
            structure = {}
        lines = [
            "# Project Structure Plan",
            "",
            "> **Document Status:** DRAFT",
            f"> **Repository Mode:** {self.workspace.mode}",
            "",
            "## Existing Architecture Evidence",
            "",
        ]
        if self.workspace.mode == "existing":
            lines.extend([
                f"- Code index run: {payload['code_index']['run_id']}",
                f"- Indexed source SHA: {payload['code_index']['source_sha']}",
                f"- Indexed files: {payload['code_index']['files_indexed']}",
                "",
                "## Existing Indexed Paths",
                "",
                *[f"- {item['path']} ({item['language']}, {item['symbols']} indexed symbols)" for item in payload["code_index"]["files"]],
                "",
            ])
        else:
            lines.extend(["New codebase: no existing repository structure is assumed.", ""])
        lines.extend(["## Planner Recommendations", "", str(structure.get("summary", "No separate structural recommendation was supplied.")), ""])
        recommendations = structure.get("recommendations", [])
        if isinstance(recommendations, list):
            lines.extend([f"- {item}" for item in recommendations if isinstance(item, str) and item.strip()])
        lines.append("")
        return "\n".join(lines)

    def _render_checklist(self, payload: dict[str, Any]) -> str:
        return "\n".join([
            f"# {payload['feature']} — Planning Review Checklist",
            "",
            "> **Document Status:** READY_FOR_HUMAN_REVIEW",
            f"> **Planner Session:** {payload['session_id']}",
            "",
            *[f"- [ ] {item}" for item in REVIEW_CHECKLIST],
            "",
            "Review the Central Plan Index, all component masters and phase files, TEST-PLAN.md and the project structure recommendations. This checklist is a handoff aid, not an approval.",
            "",
        ])

    def _document_target(self, relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Generated planning path must be project-relative and may not contain parent traversal.", details=(relative,))
        candidate = self.project_root
        for part in relative_path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Planner refuses symbolic-link traversal in generated output paths.", details=(relative,))
        target = candidate.resolve()
        if not target.is_relative_to(self.project_root):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Generated planning path escapes the project.", details=(relative,))
        return target

    def _persist(self, payload: dict[str, Any], documents: list[dict[str, Any]], *, refresh_markers_only: bool = False) -> dict[str, Any]:
        targets: list[tuple[dict[str, Any], Path]] = []
        previous: dict[str, str | None] = {}
        for item in documents:
            target = self._document_target(item["path"])
            prior = None
            if target.exists():
                if not target.is_file():
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Planner will not replace a non-file output path.", details=(item["path"],))
                prior = target.read_text(encoding="utf-8")
                with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                    current = DocumentRepository(store.connection, self.workspace.workspace_id).current_revision(item["path"])
                    if current is None or content_hash(prior) != current.content_sha256:
                        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Planner will not overwrite an unmanaged or edited planning document.", details=(item["path"],))
            previous[item["path"]] = prior
            targets.append((item, target))
        try:
            for item, target in targets:
                _atomic_write(target, item["content"])
            revisions: dict[str, Any] = {}
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                with store.transaction() as connection:
                    docs = DocumentRepository(connection, self.workspace.workspace_id)
                    for item, target in targets:
                        revision, _created = docs.record_revision(
                            relative_path=item["path"], document_type=item["document_type"], content=item["content"],
                            standards_version=STANDARDS_VERSION, source_path=str(target),
                        )
                        validation = item.get("validation")
                        if validation is not None:
                            docs.record_validation(revision_id=revision.id, validator=item["validator"],
                                                   validator_version=PLANNER_VALIDATOR_VERSION, result=validation)
                            if not validation.ok:
                                raise DridhError(ErrorCode.VALIDATION_FAILED, "A planning artifact did not pass deterministic validation.",
                                                 details=tuple(f"{item['path']}: {finding}" for finding in validation.errors[:5]))
                        revisions[item["path"]] = {"revision_id": revision.id, "sha256": revision.content_sha256}
                    planning = PlanningRepository(connection, self.workspace.workspace_id)
                    design = DesignRepository(connection, self.workspace.workspace_id)
                    if refresh_markers_only:
                        for item, _target in targets:
                            meta = docs.document_metadata(item["path"])
                            revision = revisions[item["path"]]
                            if item["kind"] in {"central_plan", "phase_file", "test_plan"}:
                                kind = {"central_plan": "CENTRAL_PLAN", "phase_file": "PHASE_FILE", "test_plan": "TEST_PLAN"}[item["kind"]]
                                cursor = connection.execute(
                                    "UPDATE planning_import SET source_revision_id=?, imported_at=? WHERE workspace_id=? AND document_id=? AND document_kind=?",
                                    (revision["revision_id"], _now(), self.workspace.workspace_id, str(meta["id"]), kind),
                                )
                                if cursor.rowcount != 1:
                                    raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "A planning projection is missing; status-only synchronization cannot recreate planning rows.", details=(item["path"],))
                            elif item["kind"] == "component_master":
                                cursor = connection.execute(
                                    "UPDATE design_import SET source_revision_id=?, imported_at=? WHERE workspace_id=? AND document_id=? AND document_kind='COMPONENT_MASTER'",
                                    (revision["revision_id"], _now(), self.workspace.workspace_id, str(meta["id"])),
                                )
                                if cursor.rowcount != 1:
                                    raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "A component-master design projection is missing; status-only synchronization cannot recreate decisions.", details=(item["path"],))
                    else:
                        central_item = next(item for item, _target in targets if item["kind"] == "central_plan")
                        central_rev = revisions[central_item["path"]]
                        central_doc = docs.document_metadata(central_item["path"])
                        planning.import_central(document_id=str(central_doc["id"]), revision_id=central_rev["revision_id"],
                                                model=validated_central(central_item["content"], central_item["path"]))
                        for item, _target in targets:
                            if item["kind"] != "phase_file":
                                continue
                            meta = docs.document_metadata(item["path"])
                            revision = revisions[item["path"]]
                            planning.import_phase(document_id=str(meta["id"]), revision_id=revision["revision_id"],
                                                  model=validated_phase(item["content"], item["path"]))
                        test_item = next(item for item, _target in targets if item["kind"] == "test_plan")
                        test_meta = docs.document_metadata(test_item["path"])
                        test_revision = revisions[test_item["path"]]
                        planning.import_test_plan(document_id=str(test_meta["id"]), revision_id=test_revision["revision_id"],
                                                  model=validated_test_plan(test_item["content"], test_item["path"]))
                        for item, _target in targets:
                            if item["kind"] != "component_master":
                                continue
                            meta = docs.document_metadata(item["path"])
                            revision = revisions[item["path"]]
                            design.replace_component_master(document_id=str(meta["id"]), revision_id=revision["revision_id"],
                                                            model=validated_master_model(item["content"], item["path"]))
            return revisions
        except BaseException:
            # The source files and SQLite are one logical projection. Restore every
            # before-image if the transaction or projection import fails.
            for item, target in targets:
                prior = previous[item["path"]]
                try:
                    if prior is None:
                        target.unlink(missing_ok=True)
                    else:
                        _atomic_write(target, prior)
                except OSError:
                    pass
            raise

    def clarify(self, session_id: str, *, question: str, answer: str, source: str = "user", model: str | None = None) -> dict[str, Any]:
        """Record a human clarification and re-run the indexer in the same resumable session."""
        if not isinstance(question, str) or not question.strip() or not isinstance(answer, str) or not answer.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Planner clarification requires non-empty question and answer strings.")
        if not isinstance(source, str) or not source.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Planner clarification source must be a non-empty string.")
        question = str(redact(question.strip()))[:2000]
        answer = str(redact(answer.strip()))[:4000]
        source = source.strip()[:80]
        payload = self._load(session_id)
        if payload["status"] in {"GENERATED", "STALE"}:
            raise DridhError(ErrorCode.INVALID_TRANSITION, "A generated or stale Planner session cannot accept clarifications.")
        source_info, tdd, use_cases, code_index = self._preflight(payload["designer_session_id"])
        source_pairs = (
            ("tdd_revision_id", "tdd_revision_id"),
            ("tdd_sha256", "tdd_sha256"),
            ("use_case_revision_id", "use_case_revision_id"),
            ("use_case_sha256", "use_case_sha256"),
        )
        if any(source_info[current] != payload["source"][saved] for current, saved in source_pairs):
            payload["status"] = "STALE"
            payload["last_error"] = "The approved TDD or Use Case baseline changed."
            self._save(payload)
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The approved source changed; this Planner session is stale.")
        payload.setdefault("clarifications", []).append({"question": question, "answer": answer, "source": source, "recorded_at": _now()})
        payload["indexer"] = None
        payload["components"] = None
        payload["testing"] = None
        payload["identifiers"] = None
        payload["generated"] = None
        payload["status"] = "INDEXING"
        payload["last_error"] = None
        payload.pop("worker_error", None)
        self._save(payload)
        return self._run_indexer(payload, source_info, tdd, use_cases, code_index, model=model)

    def generate(self, session_id: str, *, model: str | None = None) -> dict[str, Any]:
        payload = self._load(session_id)
        if payload["status"] == "GENERATED":
            return self.status(session_id)
        if payload["status"] == "STALE":
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "This Planner session is stale; start a new session from the current approved TDD.")
        source, tdd, use_cases, code_index = self._preflight(payload["designer_session_id"])
        for key, value in (("tdd_revision_id", "tdd_revision_id"), ("tdd_sha256", "tdd_sha256"),
                           ("use_case_revision_id", "use_case_revision_id"), ("use_case_sha256", "use_case_sha256")):
            if source[value] != payload["source"][key]:
                payload["status"] = "STALE"
                payload["last_error"] = "The approved source revision changed during planning."
                self._save(payload)
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Planner source baseline changed; TDD/use-case approval is no longer current.")
        if payload.get("indexer") is None:
            self._run_indexer(payload, source, tdd, use_cases, code_index, model=model)
            if payload.get("indexer") is None:
                return self.status(session_id)
        indexer = payload["indexer"]
        if indexer.get("blockers"):
            payload["status"] = "BLOCKED"
            payload["last_error"] = "Central Plan Indexer reported unresolved blockers."
            self._save(payload)
            return self.status(session_id)
        component_ids = {item["id"] for item in self._baseline(source, tdd, use_cases, code_index)["components"]}
        if payload.get("components") is None:
            payload["status"] = "COMPONENT_PLANNING"
            payload["last_error"] = None
            self._save(payload)
            prompt = (
                "You are the Phase 13 Component Planner. Return JSON only with components, summary and blockers. "
                "Return exactly one item for every supplied CMP ID with component_id, summary, responsibilities, boundaries and dependencies. "
                "Every one of those fields is a single non-empty plain-text string (use '; ' to separate several points); "
                "blockers is an array of strings. Example: {\"summary\": \"...\", \"blockers\": [], \"components\": [{\"component_id\": \"CMP-01\", "
                "\"summary\": \"...\", \"responsibilities\": \"...\", \"boundaries\": \"...\", \"dependencies\": \"...\"}]}. "
                "Do not redefine CMP IDs or invent technical facts. Component masters will link to the canonical TDD CMP definition. "
                "Story cards and dependencies are supplied; ensure each component plan makes their boundaries and ownership explicit. "
                "Stories with infrastructure=true are cross-cutting setup/operations (for example repository scaffolding or CI) that are hosted under a "
                "component for placement only: they have no Use Case or flow mapping by design, so do not raise a blocker because they fall outside the "
                "component's responsibility; mention them as hosted infrastructure. "
                "The TDD is already approved: do not re-review it. Raise a blocker only when a supplied story cannot be placed, or is placed under a "
                "component that does not own it, according to the TDD component definitions. Requirement or design observations about the approved TDD "
                "(ambiguous flow outcomes, unspecified third-party contracts) are not blockers: note them briefly in the summary field.\n"
                + json.dumps({"components": [{"id": c.component_id, "name": c.component_name, "definition": c.definition_text} for c in tdd.components],
                              "stories": indexer["stories"], "clarifications": payload.get("clarifications", [])}, ensure_ascii=False)
            )
            components = self._invoke_worker(
                payload, "dridh/workers/planner-component", prompt, model=model,
                validate=lambda reply: self._component_output(reply, component_ids),
            )
            if components is None:
                return self.status(session_id)
            payload["components"] = components
            payload.pop("worker_error", None)
            self._save(payload)
        if payload["components"].get("blockers"):
            payload["status"] = "BLOCKED"
            payload["last_error"] = "Component Planner reported unresolved blockers."
            self._save(payload)
            return self.status(session_id)
        if payload.get("testing") is None:
            payload["status"] = "TEST_PLANNING"
            payload["last_error"] = None
            self._save(payload)
            baseline = self._baseline(source, tdd, use_cases, code_index)
            prompt = (
                "You are the Phase 13 Testing Planner. Return JSON only with phases, summary, blockers and mock_policy. "
                "Create Table 15 test phases with key (unique lowercase slug: letters, digits and hyphens only, e.g. \"order-happy-path\"), test_scope, use_case_ids, flow_ids, component_ids, dependency_mode, environment, "
                "execution_command, evidence, service_inventory, parallel_isolation, fallback_rationale. Cover every supplied approved flow. "
                "use_case_ids, flow_ids and component_ids are arrays of IDs; every other phase field is a single plain-text string "
                "(execution_command is one line). "
                "dependency_mode is an enum: exactly one of TESTCONTAINERS, DOCKER_COMPOSE, REAL_QA_ENDPOINT or WIREMOCK_EXCEPTION, "
                "naming how the phase obtains its real dependencies; it is NOT about how test phases depend on each other. "
                "Use TESTCONTAINERS first; DOCKER_COMPOSE only when Testcontainers is unsuitable and environment includes branch, testcontainers: unsuitable, and project/network names containing the supplied branch_slug; REAL_QA_ENDPOINT when local services cannot run, "
                "and WIREMOCK_EXCEPTION only when neither container nor real QA endpoint is possible and dependency, contract, owner, rationale, expiry, container: unavailable and qa: unavailable are recorded. "
                "Do not propose routine mocks. mock_policy is a string that explicitly prohibits mocks, e.g. \"Do not mock application external dependencies; use real containers.\" Commands must be reproducible.\n"
                + json.dumps({"baseline": baseline, "clarifications": payload.get("clarifications", [])}, ensure_ascii=False)
            )
            testing = self._invoke_worker(
                payload, "dridh/workers/planner-testing", prompt, model=model,
                validate=lambda reply: self._testing_output(reply, baseline),
            )
            if testing is None:
                return self.status(session_id)
            payload["testing"] = testing
            payload.pop("worker_error", None)
            self._save(payload)
        if payload["testing"].get("blockers"):
            payload["status"] = "BLOCKED"
            payload["last_error"] = "Testing Planner reported unresolved blockers."
            self._save(payload)
            return self.status(session_id)

        stories = indexer["stories"]
        order_keys, depths = _topological(stories)
        for item in stories:
            item["phase"] = depths[item["key"]]
        identifiers = payload.get("identifiers")
        expected_story_keys = {item["key"] for item in stories}
        expected_test_keys = {item["key"] for item in payload["testing"]["phases"]}
        if isinstance(identifiers, dict):
            story_ids = identifiers.get("story_ids")
            test_ids = identifiers.get("test_phase_ids")
            if (not isinstance(story_ids, dict) or not isinstance(test_ids, dict)
                    or set(story_ids) != expected_story_keys or set(test_ids) != expected_test_keys
                    or not all(isinstance(value, str) for value in (*story_ids.values(), *test_ids.values()))):
                raise DridhError(ErrorCode.STATE_CORRUPT, "Persisted Planner identifier allocation does not match the current proposals.")
        else:
            story_ids: dict[str, str] = {}
            test_ids: dict[str, str] = {}
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                with store.transaction() as connection:
                    allocator = CoordinatorIdAllocator(connection)
                    for key in order_keys:
                        story_ids[key] = allocator.allocate("story", phase=depths[key], source_document=".opencode/dridh/documents/CENTRAL-PLAN-INDEX.md")
                    for item in payload["testing"]["phases"]:
                        mode = item["dependency_mode"]
                        prefix = "D" if mode == "DOCKER_COMPOSE" else ("X" if mode in {"REAL_QA_ENDPOINT", "WIREMOCK_EXCEPTION"} else "S")
                        test_ids[item["key"]] = allocator.allocate("test_phase", prefix=prefix, source_document=".opencode/dridh/documents/TEST-PLAN.md")
            payload["identifiers"] = {"story_ids": story_ids, "test_phase_ids": test_ids}
            self._save(payload)
        unlock_keys: dict[str, list[str]] = {key: [] for key in order_keys}
        for item in stories:
            for dependency in item["dependencies"]:
                unlock_keys[dependency].append(item["key"])
        partners = self._parallel(stories, story_ids)
        components_by_id = {item["component_id"]: {**item, "name": next(c.component_name for c in tdd.components if c.component_id == item["component_id"])} for item in payload["components"]["components"]}
        tdd_by_id = {item.component_id: item for item in tdd.components}
        base_dir = ".opencode/dridh/documents"
        central_path = f"{base_dir}/CENTRAL-PLAN-INDEX.md"
        test_path = f"{base_dir}/TEST-PLAN.md"
        central_text = self._render_central({**payload, "baseline": self._baseline(source, tdd, use_cases, code_index)}, stories, story_ids, depths, partners)
        docs: list[dict[str, Any]] = [{
            "path": central_path, "content": central_text, "kind": "central_plan", "document_type": "central_plan",
            "validator": "planner.central", "validation": validate_central_plan(central_text, central_path),
        }]
        for cid in sorted(components_by_id):
            component = components_by_id[cid]
            master_path = f"{base_dir}/components/{cid}/COMPONENT-MASTER.md"
            master_text = self._render_component(component, source["tdd_path"], stories, story_ids, depths, {item["key"]: item for item in stories})
            docs.append({"path": master_path, "content": master_text, "kind": "component_master", "document_type": "component_master",
                         "validator": "planner.component-master", "validation": validate_component_master(master_text, master_path)})
            for phase in sorted({depths[item["key"]] for item in stories if item["component_id"] == cid}):
                phase_path = f"{base_dir}/components/{cid}/PHASE-{phase}-{_slug(tdd_by_id[cid].component_name)}.md"
                phase_text = self._render_phase(component, phase, stories, story_ids, unlock_keys)
                docs.append({"path": phase_path, "content": phase_text, "kind": "phase_file", "document_type": "phase_file",
                             "validator": "planner.phase", "validation": validate_phase_file(phase_text, phase_path)})
        test_text = self._render_test_plan(payload, payload["testing"]["phases"], test_ids)
        docs.append({"path": test_path, "content": test_text, "kind": "test_plan", "document_type": "test_plan",
                     "validator": "planner.test-plan", "validation": validate_test_plan(test_text, test_path)})
        structure_path = f"{base_dir}/PROJECT-STRUCTURE-PLAN.md"
        structure_text = self._render_structure({**payload, "indexer": indexer})
        docs.append({"path": structure_path, "content": structure_text, "kind": "artifact", "document_type": "planning_artifact"})
        checklist_path = f"{base_dir}/PLANNING-REVIEW-CHECKLIST.md"
        checklist_text = self._render_checklist(payload)
        docs.append({"path": checklist_path, "content": checklist_text, "kind": "artifact", "document_type": "planning_artifact"})
        try:
            revisions = self._persist(payload, docs)
        except DridhError as exc:
            payload["last_error"] = exc.message
            payload["status"] = "BLOCKED"
            self._save(payload)
            raise
        generated_files = [{"path": item["path"], "sha256": content_hash(item["content"]), "kind": item["kind"]} for item in docs]
        payload["generated"] = {
            "files": generated_files,
            "revisions": revisions,
            "story_ids": story_ids,
            "test_phase_ids": test_ids,
            "story_count": len(stories),
            "component_count": len(components_by_id),
            "test_phase_count": len(payload["testing"]["phases"]),
            "review_checklist": checklist_path,
            "central_plan": central_path,
            "test_plan": test_path,
        }
        payload["status"] = "GENERATED"
        payload["last_error"] = None
        self._save(payload)
        gate = self._gate_checks(session_id)
        if not gate.get("ok"):
            payload["status"] = "BLOCKED"
            payload["last_error"] = str(gate.get("reason", "Generated planning artifacts failed reconciliation."))
            self._save(payload)
        else:
            from dridh.jev.checkpoints import run_checkpoint

            payload["jev_checkpoint"] = run_checkpoint(
                self.workspace, stage="planner", subject_id=session_id, agent_id="dridh/planner", summary=(
                    f"Plan validated and reconciled with SQLite. Stories: {len(payload['indexer']['stories'])}; "
                    f"components: {len(payload['components']['components'])}; test phases: {len(payload['testing']['phases'])}; "
                    "dependency graph acyclic: yes; every story traces to a use case and has acceptance criteria."),
                metadata={"source_revision_sha": self.plan_definition_digest(session_id)})
            self._save(payload)
        return self.status(session_id)

    def sync(self, session_id: str) -> dict[str, Any]:
        """Refresh Markdown status projections from the authoritative SQLite planning graph."""
        payload = self._load(session_id)
        if payload["status"] != "GENERATED" or not isinstance(payload.get("generated"), dict):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Only a fully generated Planner session can synchronize status projections.")
        source, tdd, use_cases, code_index = self._preflight(payload["designer_session_id"])
        for key in ("tdd_revision_id", "tdd_sha256", "use_case_revision_id", "use_case_sha256"):
            if source[key] != payload["source"][key]:
                payload["status"] = "STALE"
                payload["last_error"] = "The approved source revision changed before projection synchronization."
                self._save(payload)
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The approved TDD or Use Case source changed; the Planner baseline is stale.")
        generated = payload["generated"]
        for item in generated["files"]:
            target = self._document_target(item["path"])
            if not target.is_file() or content_hash(target.read_text(encoding="utf-8")) != item["sha256"]:
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Planner status sync will not overwrite a planning document edited since the previous projection.", details=(item["path"],))
        stories = payload["indexer"]["stories"]
        order_keys, depths = _topological(stories)
        for item in stories:
            item["phase"] = depths[item["key"]]
        story_ids = generated["story_ids"]
        test_ids = generated["test_phase_ids"]
        if set(story_ids) != {item["key"] for item in stories} or set(test_ids) != {item["key"] for item in payload["testing"]["phases"]}:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Planner generated identifier maps do not match the persisted proposals.")
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            docs = DocumentRepository(store.connection, self.workspace.workspace_id)
            planning = PlanningRepository(store.connection, self.workspace.workspace_id)
            graph = planning.graph()
            if set(story_ids.values()) - set(graph):
                raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "One or more generated stories are missing from the SQLite planning graph.")
            story_statuses: dict[str, str] = {}
            expected_dependencies = {story_ids[item["key"]]: tuple(sorted(story_ids[value] for value in item["dependencies"])) for item in stories}
            expected_unlocks: dict[str, set[str]] = {story_ids[key]: set() for key in story_ids}
            for key, item in ((item["key"], item) for item in stories):
                for dependency in item["dependencies"]:
                    expected_unlocks[story_ids[dependency]].add(story_ids[key])
            expected_parallel = self._parallel(stories, story_ids)
            for item in stories:
                key = item["key"]
                story_id = story_ids[key]
                view = graph[story_id]
                if (view.dependencies != expected_dependencies[story_id]
                        or view.unlocks != tuple(sorted(expected_unlocks[story_id]))
                        or view.parallel_with != tuple(expected_parallel[story_id])
                        or view.title != item["title"]
                        or view.component_id != item["component_id"]
                        or view.phase != depths[key]
                        or view.use_cases != tuple(sorted(item["use_case_ids"]))):
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The SQLite story definition or graph changed independently of the approved Planner proposal.", details=(story_id,))
                story_statuses[key] = view.status
            test_meta = docs.document_metadata(generated["test_plan"])
            if test_meta is None:
                raise DridhError(ErrorCode.STATE_CORRUPT, "The canonical TEST-PLAN.md document is missing from SQLite.")
            test_rows = store.connection.execute(
                "SELECT test_phase_id, status FROM test_phase WHERE workspace_id=? AND document_id=?",
                (self.workspace.workspace_id, str(test_meta["id"])),
            ).fetchall()
            test_status_by_id = {row["test_phase_id"]: row["status"] for row in test_rows}
            if set(test_ids.values()) != set(test_status_by_id):
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The SQLite test-phase set differs from the generated Table 15 plan.")
            actual_test_signature = planning.test_plan_signature_from_rows(str(test_meta["id"]))
            expected_test_signature = []
            for phase in payload["testing"]["phases"]:
                expected_test_signature.append((
                    test_ids[phase["key"]], phase["test_scope"], tuple(sorted(phase["use_case_ids"])),
                    tuple(sorted(phase["flow_ids"])), tuple(sorted(phase["component_ids"])),
                    phase["dependency_mode"], phase["environment"], phase["execution_command"], phase["evidence"],
                ))
            if sorted(row[:9] for row in actual_test_signature) != sorted(expected_test_signature):
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The SQLite test-plan definitions changed independently of the approved Planner proposal.")
            test_statuses = {key: test_status_by_id[test_id] for key, test_id in test_ids.items()}
        unlock_keys: dict[str, list[str]] = {key: [] for key in order_keys}
        for item in stories:
            for dependency in item["dependencies"]:
                unlock_keys[dependency].append(item["key"])
        partners = self._parallel(stories, story_ids)
        components_by_id = {item["component_id"]: {**item, "name": next(c.component_name for c in tdd.components if c.component_id == item["component_id"])}
                            for item in payload["components"]["components"]}
        tdd_by_id = {item.component_id: item for item in tdd.components}
        baseline = self._baseline(source, tdd, use_cases, code_index)
        base_dir = ".opencode/dridh/documents"
        central_path = generated["central_plan"]
        test_path = generated["test_plan"]
        central_text = self._render_central({**payload, "baseline": baseline}, stories, story_ids, depths, partners, story_statuses)
        docs_to_write: list[dict[str, Any]] = [{
            "path": central_path, "content": central_text, "kind": "central_plan", "document_type": "central_plan",
            "validator": "planner.central", "validation": validate_central_plan(central_text, central_path),
        }]
        for cid in sorted(components_by_id):
            component = components_by_id[cid]
            master_path = f"{base_dir}/components/{cid}/COMPONENT-MASTER.md"
            master_text = self._render_component(component, source["tdd_path"], stories, story_ids, depths,
                                                 {item["key"]: item for item in stories}, story_statuses)
            docs_to_write.append({"path": master_path, "content": master_text, "kind": "component_master", "document_type": "component_master",
                                  "validator": "planner.component-master", "validation": validate_component_master(master_text, master_path)})
            for phase in sorted({depths[item["key"]] for item in stories if item["component_id"] == cid}):
                phase_path = f"{base_dir}/components/{cid}/PHASE-{phase}-{_slug(tdd_by_id[cid].component_name)}.md"
                phase_text = self._render_phase(component, phase, stories, story_ids, unlock_keys, story_statuses)
                docs_to_write.append({"path": phase_path, "content": phase_text, "kind": "phase_file", "document_type": "phase_file",
                                      "validator": "planner.phase", "validation": validate_phase_file(phase_text, phase_path)})
        test_text = self._render_test_plan(payload, payload["testing"]["phases"], test_ids, test_statuses)
        docs_to_write.append({"path": test_path, "content": test_text, "kind": "test_plan", "document_type": "test_plan",
                              "validator": "planner.test-plan", "validation": validate_test_plan(test_text, test_path)})
        structure_path = f"{base_dir}/PROJECT-STRUCTURE-PLAN.md"
        structure_text = self._render_structure({**payload, "indexer": payload["indexer"]})
        docs_to_write.append({"path": structure_path, "content": structure_text, "kind": "artifact", "document_type": "planning_artifact"})
        checklist_path = generated["review_checklist"]
        checklist_text = self._render_checklist(payload)
        docs_to_write.append({"path": checklist_path, "content": checklist_text, "kind": "artifact", "document_type": "planning_artifact"})
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            docs = DocumentRepository(store.connection, self.workspace.workspace_id)
            planning = PlanningRepository(store.connection, self.workspace.workspace_id)
            design = DesignRepository(store.connection, self.workspace.workspace_id)
            for item in docs_to_write:
                meta = docs.document_metadata(item["path"])
                if meta is None:
                    raise DridhError(ErrorCode.STATE_CORRUPT, "A planning projection is missing from SQLite before status synchronization.", details=(item["path"],))
                document_id = str(meta["id"])
                if item["kind"] == "central_plan":
                    model = validated_central(item["content"], item["path"])
                    if central_signature(model) != planning.central_signature_from_rows(document_id):
                        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Central Plan definitions or relationships changed in SQLite; status-only synchronization refused.", details=(item["path"],))
                elif item["kind"] == "phase_file":
                    model = validated_phase(item["content"], item["path"])
                    component_id, phase_number = phase_identity(item["content"])
                    if phase_signature(model) != planning.phase_signature_from_rows(document_id, component_id=component_id, phase_number=phase_number):
                        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Story-card definitions changed in SQLite; status-only synchronization refused.", details=(item["path"],))
                elif item["kind"] == "test_plan":
                    model = validated_test_plan(item["content"], item["path"])
                    if test_plan_signature(model) != planning.test_plan_signature_from_rows(document_id):
                        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Test-plan definitions changed in SQLite; status-only synchronization refused.", details=(item["path"],))
                elif item["kind"] == "component_master":
                    model = validated_master_model(item["content"], item["path"])
                    if design.stored_master(document_id) != model:
                        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Component design decisions changed in SQLite; status-only synchronization refused.", details=(item["path"],))
        revisions = self._persist(payload, docs_to_write, refresh_markers_only=True)
        generated["files"] = [{"path": item["path"], "sha256": content_hash(item["content"]), "kind": item["kind"]} for item in docs_to_write]
        generated["revisions"] = revisions
        payload["generated"] = generated
        payload["last_error"] = None
        self._save(payload)
        result = self._gate_checks(session_id)
        if not result.get("ok"):
            payload["status"] = "BLOCKED"
            payload["last_error"] = str(result.get("reason", "The synchronized plan failed reconciliation."))
            self._save(payload)
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Status projection synchronization did not pass reconciliation.", details=(payload["last_error"],))
        return self.status(session_id)
    def plan_definition_digest(self, session_id: str) -> str:
        """Digest of the plan *definitions* (stories, order, components, test phases and source baseline).

        Status columns in the Markdown projections are deliberately excluded so ``planner sync`` during
        execution does not invalidate a human approval; any change to what will be built does.
        """
        payload = self._load(session_id)
        body = {
            "source": {key: payload["source"][key] for key in ("tdd_revision_id", "tdd_sha256", "use_case_revision_id", "use_case_sha256")},
            "stories": (payload.get("indexer") or {}).get("stories", []),
            "order": (payload.get("indexer") or {}).get("order", []),
            "components": (payload.get("components") or {}).get("components", []),
            "testing": (payload.get("testing") or {}).get("phases", []),
        }
        return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()

    def open_review(self, session_id: str) -> dict[str, Any]:
        """Open (or return) the human review of the current Central Plan revision, with every checklist item unchecked."""
        payload = self._load(session_id)
        if payload["status"] != "GENERATED" or not isinstance(payload.get("generated"), dict):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The plan is not generated and awaiting human approval.")
        if payload.get("approval"):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "This plan is already approved.")
        central_path = payload["generated"]["central_plan"]
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                docs = DocumentRepository(connection, self.workspace.workspace_id)
                revision, meta = docs.current_revision(central_path), docs.document_metadata(central_path)
                if revision is None or meta is None:
                    raise DridhError(ErrorCode.STATE_CORRUPT, "The Central Plan is missing from SQLite.")
                existing = payload.get("review")
                if isinstance(existing, dict) and existing.get("revision_id") == revision.id:
                    review = ReviewRepository(connection, self.workspace.workspace_id).get_review(existing["review_id"])
                    if review["status"] in ("OPEN", "IN_REVIEW"):
                        return {"review_id": existing["review_id"], "revision_id": revision.id, "checklist": review["checklist"]}
                reviews = ReviewRepository(connection, self.workspace.workspace_id)
                review_id = reviews.create_review(document_id=str(meta["id"]), revision_id=revision.id, reviewer="Planner Review", actor="coordinator:planner")
                for item in REVIEW_CHECKLIST:
                    reviews.set_checklist(review_id, item, False, actor="coordinator:planner")
                created = reviews.get_review(review_id)
        payload["review"] = {"review_id": review_id, "revision_id": revision.id}
        self._save(payload)
        return {"review_id": review_id, "revision_id": revision.id, "checklist": created["checklist"]}

    def approval_challenge(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        if payload["status"] != "GENERATED" or not isinstance(payload.get("generated"), dict):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The plan is not generated and awaiting human approval.")
        if payload.get("approval"):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "This plan is already approved.")
        review = payload.get("review")
        if not isinstance(review, dict):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Open the plan review first.", recovery=f"Run `dridh planner review {session_id}`, tick the checklist, then approve.")
        digest = self.plan_definition_digest(session_id)
        return {
            "session_id": session_id, "plan_digest": digest, "review_id": review["review_id"],
            "confirmation": f"APPROVE PLAN {session_id} {digest[:12]}",
            "central_plan": payload["generated"]["central_plan"], "story_count": payload["generated"]["story_count"],
        }

    def approve(self, session_id: str, *, reviewer: str, confirmation: str) -> dict[str, Any]:
        """Record the human, revision-bound approval that enables execution (D-P15-01). The checklist must be ticked first."""
        challenge = self.approval_challenge(session_id)
        if confirmation != challenge["confirmation"]:
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The plan approval confirmation did not match.")
        reviewer = str(reviewer).strip()[:100]
        if not reviewer:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "reviewer is required.")
        pre = self._gate_checks(session_id)
        if not pre.get("ok"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The plan does not pass reconciliation; approval refused.", details=(str(pre.get("reason")),))
        payload = self._load(session_id)
        central_path = payload["generated"]["central_plan"]
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                revision = DocumentRepository(connection, self.workspace.workspace_id).current_revision(central_path)
                if revision is None or revision.id != payload["review"]["revision_id"]:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The Central Plan changed since the review opened; open a new review.",
                                     recovery=f"Run `dridh planner review {session_id}` again.")
                approval_id = ReviewRepository(connection, self.workspace.workspace_id).decide(
                    challenge["review_id"], decision="APPROVED", reviewer=reviewer,
                    reason=f"Human approved the generated plan (definition digest {challenge['plan_digest'][:12]}).",
                    actor=f"human:{reviewer}", revision_id=revision.id,
                )
        payload["approval"] = {
            "approval_id": approval_id, "review_id": challenge["review_id"], "reviewer": reviewer, "approved_at": _now(),
            "plan_digest": challenge["plan_digest"], "central_plan_revision_id": revision.id,
        }
        self._save(payload)
        result = self.gate(session_id)
        if not result.get("unlocked"):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Plan approval was recorded but the gate remains closed.", details=(str(result.get("reason")),))
        return {"ok": True, "session_id": session_id, "approval_id": approval_id, "plan_digest": challenge["plan_digest"], "gate": result}

    def gate(self, session_id: str) -> dict[str, Any]:
        checked = self._gate_checks(session_id)
        if not checked.pop("ok"):
            return checked
        try:
            payload = self._load(session_id)
            approval = payload.get("approval")
            if not isinstance(approval, dict):
                return {"unlocked": False, "stage": "dridh/executor", "human_review_required": True,
                        "plan_valid": True, "reason": "The generated plan has not received revision-bound human approval."}
            if approval.get("plan_digest") != self.plan_definition_digest(session_id):
                return {"unlocked": False, "stage": "dridh/executor", "human_review_required": True,
                        "plan_valid": True, "reason": "The plan definitions changed after human approval; approve the new plan."}
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                row = store.connection.execute(
                    "SELECT decision FROM human_approval WHERE id=? AND workspace_id=?", (approval.get("approval_id"), self.workspace.workspace_id)
                ).fetchone()
            if row is None or row["decision"] != "APPROVED":
                return {"unlocked": False, "stage": "dridh/executor", "human_review_required": True,
                        "plan_valid": True, "reason": "The recorded plan approval is missing from SQLite."}
        except DridhError as exc:
            return {"unlocked": False, "stage": "dridh/executor", "reason": exc.message}
        return {**checked, "approval_id": approval["approval_id"], "approved_by": approval["reviewer"],
                "plan_digest": approval["plan_digest"], "human_review_required": False}

    def _gate_checks(self, session_id: str) -> dict[str, Any]:
        try:
            payload = self._load(session_id)
            if payload["status"] != "GENERATED" or not isinstance(payload.get("generated"), dict):
                return {"ok": False, "unlocked": False, "stage": "dridh/executor", "reason": "The Planner has not generated a complete plan for human review."}
            current_source, _tdd, _use_cases, _code_index = self._preflight(payload["designer_session_id"])
            for source_key in ("tdd_revision_id", "tdd_sha256", "use_case_revision_id", "use_case_sha256"):
                if current_source[source_key] != payload["source"][source_key]:
                    return {"ok": False, "unlocked": False, "stage": "dridh/executor", "reason": f"The approved {source_key} changed after planning."}
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                docs = DocumentRepository(store.connection, self.workspace.workspace_id)
                planning = PlanningRepository(store.connection, self.workspace.workspace_id)
                design = DesignRepository(store.connection, self.workspace.workspace_id)
                findings: list[str] = []
                for item in payload["generated"]["files"]:
                    target = self._document_target(item["path"])
                    if not target.is_file() or content_hash(target.read_text(encoding="utf-8")) != item["sha256"]:
                        findings.append(f"{item['path']} is missing or differs from the generated revision.")
                        continue
                    revision = docs.current_revision(item["path"])
                    meta = docs.document_metadata(item["path"])
                    expected = payload["generated"]["revisions"].get(item["path"], {})
                    if revision is None or meta is None or revision.id != expected.get("revision_id") or revision.content_sha256 != item["sha256"]:
                        findings.append(f"{item['path']} does not match its immutable SQLite revision.")
                        continue
                    if item["kind"] in {"central_plan", "phase_file", "test_plan"}:
                        kind = {"central_plan": "CENTRAL_PLAN", "phase_file": "PHASE_FILE", "test_plan": "TEST_PLAN"}[item["kind"]]
                        findings.extend(f"{item['path']}: {finding.message}" for finding in planning.findings(
                            relative_path=item["path"], document_id=str(meta["id"]), kind=kind,
                            current_revision_id=revision.id, current_content=docs.stored_content(revision.id),
                        ))
                    elif item["kind"] == "component_master":
                        findings.extend(f"{item['path']}: {finding.message}" for finding in design.findings(
                            relative_path=item["path"], document_id=str(meta["id"]), kind="COMPONENT_MASTER",
                            current_revision_id=revision.id, current_content=docs.stored_content(revision.id),
                        ))
                    validation = docs.latest_validation(revision.id, {
                        "central_plan": "planner.central", "phase_file": "planner.phase",
                        "test_plan": "planner.test-plan", "component_master": "planner.component-master",
                    }.get(item["kind"], ""))
                    if item["kind"] != "artifact" and (validation is None or validation.status != "PASS"):
                        findings.append(f"{item['path']} has no passing current validation.")
                if findings:
                    return {"ok": False, "unlocked": False, "stage": "dridh/executor", "reason": findings[0], "findings": findings[:20]}
            return {
                "ok": True, "unlocked": True, "stage": "dridh/executor", "session_id": session_id,
                "central_plan": payload["generated"]["central_plan"], "test_plan": payload["generated"]["test_plan"],
                "story_count": payload["generated"]["story_count"], "component_count": payload["generated"]["component_count"],
                "test_phase_count": payload["generated"]["test_phase_count"],
                "tdd_revision_id": payload["source"]["tdd_revision_id"],
                "tdd_sha256": payload["source"]["tdd_sha256"],
                "human_review_required": True,
                "review_checklist": payload["generated"]["review_checklist"],
            }
        except DridhError as exc:
            return {"ok": False, "unlocked": False, "stage": "dridh/executor", "reason": exc.message}

    def execution_stories(self, session_id: str) -> list[dict[str, Any]]:
        """The approved plan's stories with their SQLite ids and current status, in dependency order."""
        gate = self.gate(session_id)
        if not gate.get("unlocked"):
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The plan is not approved for execution.", details=(str(gate.get("reason", "")),))
        return self.planned_stories(session_id)

    def planned_stories(self, session_id: str) -> list[dict[str, Any]]:
        """The generated plan's stories (ids, ownership, status) whether or not the plan is approved. Read-only."""
        payload = self._load(session_id)
        if not (payload.get("generated") or {}).get("story_ids"):
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The plan has not been generated yet.", details=(session_id,))
        ids = payload["generated"]["story_ids"]
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            rows = {r["story_id"]: r["status"] for r in store.connection.execute(
                "SELECT story_id, status FROM story WHERE workspace_id=?", (self.workspace.workspace_id,))}
        by_key = {item["key"]: item for item in payload["indexer"]["stories"]}
        result = []
        for key in payload["indexer"]["order"]:
            item = by_key[key]
            result.append({
                "story_id": ids[key], "key": key, "title": item["title"], "description": item.get("description", ""),
                "component_id": item["component_id"], "use_case_ids": item["use_case_ids"],
                "todos": item["todos"], "acceptance_criteria": item["acceptance_criteria"],
                "definition_of_done": item["definition_of_done"], "file_ownership": item["file_ownership"],
                "infrastructure": bool(item.get("infrastructure")),
                "dependencies": [ids[dep] for dep in item["dependencies"]], "status": rows.get(ids[key], "NOT_STARTED"),
            })
        return result

    def update_story_statuses(self, session_id: str, statuses: dict[str, str], *, actor: str) -> None:
        """Set story statuses in SQLite (the authority), then refresh the Markdown projections."""
        if not statuses:
            return
        bad = {value for value in statuses.values() if value not in STORY_STATUSES}
        if bad:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Invalid story status.", details=tuple(sorted(bad)))
        from dridh.state.reviews import _audit
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                for story_id, status in sorted(statuses.items()):
                    updated = connection.execute(
                        "UPDATE story SET status=? WHERE workspace_id=? AND story_id=?", (status, self.workspace.workspace_id, story_id))
                    if updated.rowcount != 1:
                        raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown story '{story_id}'.")
                    _audit(connection, self.workspace.workspace_id, actor, "story.status", "story", story_id, {"status": status})
        self.sync(session_id)

    def gate_required(self) -> dict[str, Any]:
        """Find a current generated planning baseline for this workspace."""
        root = self.state_root / "artifacts" / "planner"
        if not root.is_dir():
            return {"unlocked": False, "stage": "dridh/executor", "reason": "No Planner session exists."}
        for folder in sorted(root.glob("planner-*"), reverse=True):
            try:
                self._load(folder.name)
            except DridhError:
                continue
            result = self.gate(folder.name)
            if result.get("unlocked"):
                return result
        return {"unlocked": False, "stage": "dridh/executor", "reason": "No current, reconciled Planner baseline is available."}
