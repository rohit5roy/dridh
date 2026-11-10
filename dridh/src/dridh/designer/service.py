"""Phase 12 Technical Designer: approved-baseline inquiry, TDD generation and review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.document_contracts.identifiers import CoordinatorIdAllocator
from dridh.document_contracts.registry import load_contract_bundle
from dridh.document_contracts.validator import (
    H_TRACE,
    STANDARDS_VERSION,
    TDD_SECTIONS,
    sanitize_tdd,
    validate_tdd,
)
from dridh.ideator.service import IdeatorService, _atomic_pair, _atomic_write
from dridh.integrations.opencode import OpenCodeRunner
from dridh.orchestration.repair import call_with_repair
from dridh.state.design import DesignRepository, validated_tdd_model
from dridh.state.documents import DocumentRepository, content_hash
from dridh.state.reviews import ReviewRepository
from dridh.state.store import StateStore
from dridh.state.usecases import UseCaseRepository
from dridh.workspace.layout import WorkspaceInfo

SESSION_SCHEMA = "dridh.technical-inquiry"
SESSION_VERSION = 1
SESSION_ID = re.compile(r"^designer-[0-9a-f]{12}$")
FEATURE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TDD_VALIDATOR = "designer.tdd"
TDD_VALIDATOR_VERSION = "1"
REVIEW_CHECKLIST = (
    "Approved Use Case revision and SHA-256 match the current source.",
    "Every approved Use Case and flow is represented in Table 13.",
    "Every CMP-NN component is defined in this TDD and has an accountable boundary.",
    "DD-N decisions include context, alternatives, rationale and reversibility.",
    "Open questions, assumptions, evidence gaps and one-way-door decisions are explicit.",
    "API and event contracts are documented or explicitly marked not applicable with a reason.",
    "Data ownership, persistence, keys, indexes and retention are addressed or marked not applicable.",
    "Security, privacy, quality, migration, rollback, monitoring, failures and cost are addressed.",
    "Deterministic TDD validation passes and the SQLite design projection is current.",
    "Reviewers have checked the source revision, generated TDD and unresolved items.",
)
TOPICS: tuple[tuple[str, str, str], ...] = (
    ("existing_systems", "Existing systems and responsibilities", "List each relevant system/service, its current responsibility, owner and evidence."),
    ("repositories", "Repositories and implementation location", "Which repositories/services should contain this change? Supply Code Reacher evidence or confirm the repository and owning team."),
    ("components", "New and existing components", "Which components are new or existing, and what responsibilities do they own?"),
    ("boundaries", "Component boundaries and data ownership", "Where are component boundaries, system-of-record ownership and consistency boundaries?"),
    ("integrations", "Internal and external integrations", "Which systems integrate, who owns each dependency, and what are the failure/availability expectations?"),
    ("apis_events", "APIs, events and contracts", "Which API operations/events change? Include consumers, compatibility, auth, idempotency and contract evidence."),
    ("data_lifecycle", "Data persistence and lifecycle", "What data is stored, keys/indexes, retention, deletion, archival, backup and migration requirements?"),
    ("security_privacy", "Security, privacy and threat boundaries", "What are the trust boundaries, data classifications, authentication, authorisation, secrets and threat controls?"),
    ("scale_availability", "Scale, throughput and availability", "What volumes, peaks, latency, availability and recovery objectives are known? Mark estimates as assumptions."),
    ("failure_recovery", "Failure modes and recovery", "What can fail, how are retries/idempotency/dead letters/reconciliation handled, and what is the recovery owner?"),
    ("migration_rollback", "Migration and rollback", "What must be migrated, deployment ordering, compatibility, rollback triggers and recovery strategy?"),
    ("monitoring", "Monitoring and alerting", "Which service, business and security signals, dashboards, alerts and on-call responsibilities are required?"),
    ("cost_effort", "Cost and level of effort", "What are the material compute, storage, vendor and operating costs, and what effort ranges are supportable by evidence?"),
    ("test_strategy", "Test strategy and environments", "What unit, integration, contract, security, performance and journey tests run, with which real dependencies/environments?"),
    ("one_way_doors", "One-way-door architecture decisions", "Which decisions are costly to reverse, what alternatives were evaluated and who must approve them?"),
    ("dependencies_reviewers", "Team dependencies and reviewers", "Which teams, reviewers and external commitments are required, and who is accountable?"),
)

DEFAULT_SECTIONS = {
    "document_purpose": "This document specifies the technical design for the approved feature baseline and is intended for engineering, architecture, security, QA and operational review.",
    "blocking_reviewers": "OPEN — identify an accountable architecture/engineering reviewer and each impacted team before approval.",
    "problem_resources": "Derived from the approved Use Case source. See the traceability matrix and the source reference in Document Control.",
    "definitions": "N/A — no additional terminology was supplied beyond the approved requirements. Add definitions when a technical term is ambiguous.",
    "scope": "OPEN — the design scope must remain within the approved Use Case baseline and the component/flow traceability below.",
    "out_of_scope": "N/A — no additional technical exclusions were evidenced. Preserve the approved Use Case out-of-scope boundary.",
    "target_dates": "N/A — no committed dates were supplied. Do not infer delivery commitments from estimates.",
    "design_summary": "OPEN — the design generator did not supply an evidence-backed architecture summary.",
    "system_context": "OPEN — add a system/context diagram only when the system boundaries and dependencies are supported by evidence.",
    "internal_external_flow": "OPEN — describe request, persistence, event and recovery paths from verified contracts and evidence.",
    "data_ownership": "OPEN — record system-of-record ownership, keys, indexes, retention and lifecycle where applicable.",
    "scalability": "OPEN — quantify capacity and growth from evidence; do not present assumptions as measured facts.",
    "dependencies": "OPEN — confirm each dependency owner, required commitment and failure impact.",
    "api_design": "N/A — no API design was evidenced. Confirm this with the relevant owner before approval.",
    "events": "N/A — no event change was evidenced. Confirm this with the relevant owner before approval.",
    "data_model": "OPEN — document the persistence model, identifiers, constraints, indexes, retention and recovery.",
    "quality_approach": "OPEN — align the QA strategy to each approved success, alternate, exception and boundary flow.",
    "cost": "OPEN — estimate material infrastructure/vendor/operating cost and state all assumptions.",
    "security_privacy": "OPEN — record trust boundaries, authentication, authorisation, secret handling, encryption, privacy and threat controls.",
    "effort": "OPEN — provide evidence-based effort ranges, dependencies and confidence; do not fabricate precision.",
    "technical_alternatives": "OPEN — document credible alternatives for material technical choices and link the corresponding DD-N.",
    "migration": "N/A — no migration details were supplied. Confirm whether any state, traffic, configuration or contract migration applies.",
    "rollback": "OPEN — define rollback triggers, data consistency actions and recovery validation.",
    "feature_flags": "N/A — no feature flag was evidenced. Confirm applicability with the delivery owner.",
    "feature_logic": "OPEN — describe the implementation logic and behaviour derived from approved flows.",
    "scaling_estimate": "OPEN — quantify scale using sourced measurements or explicitly labelled assumptions.",
    "monitoring": "OPEN — identify measurable signals, alerts, dashboards and on-call ownership.",
    "failure_recovery": "OPEN — define timeouts, retries, idempotency, reconciliation, dead-letter and operator recovery behaviour.",
    "acceptance": "Every row in Table 13 must be implemented and tested; no approved flow may be silently omitted.",
    "ai_systems": "N/A — no AI system was evidenced in this design. Revisit if the design introduces model inference or automated decisions.",
    "references": "See the approved Use Case source revision and the evidence references recorded in this technical inquiry.",
}

def _now() -> str:
    return datetime.now(UTC).isoformat()

def _sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()

def _strings(value: Any, field: str, *, maximum: int = 100) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{field} must be an array of {maximum} or fewer non-empty strings.")
    return [item.strip() for item in value]

def _required_text(value: Any, field: str, *, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{field} must be a non-empty string of at most {limit} characters.")
    return value.strip()

def _cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", " ").strip()

def _table(headers: tuple[str, ...], rows: list[list[str]]) -> str:
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows),
    ])

def _approved_use_case(workspace: WorkspaceInfo, session_id: str | None) -> dict[str, Any]:
    gate = IdeatorService(workspace).gate(session_id)
    if not gate.get("unlocked"):
        raise DridhError(
            ErrorCode.DEPENDENCY_NOT_READY,
            "Technical Designer is locked because the approved Use Case baseline is not current.",
            details=(str(gate.get("reason", "Human approval and reconciliation are required.")),),
            recovery="Complete Ideator review, obtain human terminal approval and reconcile the exact Use Case revision.",
        )
    canonical = workspace.project_root / "001-USE-CASES.md"
    if not canonical.is_file() or canonical.is_symlink():
        raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "001-USE-CASES.md must be a regular file before Technical Designer can start.")
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        documents = DocumentRepository(store.connection, workspace.workspace_id)
        metadata = documents.document_metadata("001-USE-CASES.md")
        revision = documents.current_revision("001-USE-CASES.md")
        if metadata is None or revision is None:
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "The Use Case source has no persisted document revision.")
        disk = canonical.read_text(encoding="utf-8")
        if content_hash(disk) != revision.content_sha256:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The on-disk Use Case document differs from its persisted revision.")
        if gate.get("source_revision_id") != revision.id or gate.get("source_sha256") != revision.content_sha256:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The approved Use Case gate does not identify the current source revision.")
        validation = documents.latest_validation(revision.id, "ideator.use_cases")
        if validation is None or validation.status != "PASS":
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The current Use Case revision has no passing deterministic validation.")
        repository = UseCaseRepository(store.connection, workspace.workspace_id)
        findings = repository.projection_findings(
            relative_path="001-USE-CASES.md",
            document_id=str(metadata["id"]),
            current_revision_id=revision.id,
            current_content=documents.stored_content(revision.id),
        )
        if findings:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Use Case SQLite reconciliation is not clean.", details=tuple(item.message for item in findings[:10]))
        model = repository.stored_model(str(metadata["id"]))
        return {
            "document_id": str(metadata["id"]),
            "revision_id": revision.id,
            "sha256": revision.content_sha256,
            "content": documents.stored_content(revision.id),
            "model": model,
            "gate": gate,
        }

_WORKER_LABELS = {
    "dridh/workers/designer-inquirer": "Technical Inquirer",
    "dridh/workers/designer-doc-generator": "Technical Design Document Generator",
    "dridh/workers/designer-handoff": "Designer Handoff",
}


class DesignerService:
    def __init__(self, workspace: WorkspaceInfo, *, runner: Any | None = None) -> None:
        self.workspace = workspace
        self.project_root = workspace.project_root.resolve()
        self.state_root = workspace.root.resolve()
        self.runner = runner
        self._repair_log: list[dict[str, Any]] = []

    def _directory(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "designer session_id must look like designer-0123456789ab.")
        return self.state_root / "artifacts" / "designer" / session_id

    def _paths(self, session_id: str) -> tuple[Path, Path]:
        directory = self._directory(session_id)
        return directory / "technical-inquiry.json", directory / "TECHNICAL-INQUIRY-NOTES.md"

    def _load(self, session_id: str) -> dict[str, Any]:
        json_path, notes_path = self._paths(session_id)
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DridhError(ErrorCode.STATE_CORRUPT, "The Technical Designer inquiry is missing or unreadable.", details=(session_id,)) from None
        if not isinstance(payload, dict) or payload.get("schema") != SESSION_SCHEMA or payload.get("schema_version") != SESSION_VERSION:
            raise DridhError(ErrorCode.SCHEMA_UNSUPPORTED, "Unsupported Technical Designer inquiry schema.", details=(session_id,))
        if payload.get("session_id") != session_id or not notes_path.is_file():
            raise DridhError(ErrorCode.STATE_CORRUPT, "Technical Designer inquiry projections are incomplete.", details=(session_id,))
        if payload.get("status") not in {"INQUIRY", "IN_REVIEW", "APPROVED", "STALE"}:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Technical Designer status is invalid.", details=(session_id,))
        if not isinstance(payload.get("questions"), list) or not isinstance(payload.get("objective"), str):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Technical Designer inquiry data is invalid.", details=(session_id,))
        for index, item in enumerate(payload["questions"], start=1):
            if (
                not isinstance(item, dict)
                or item.get("question_id") != f"TQ-{index:03d}"
                or item.get("order") != index
                or item.get("topic") not in {topic[0] for topic in TOPICS}
                or not isinstance(item.get("question"), str)
                or not isinstance(item.get("answer"), str)
                or not isinstance(item.get("source"), str)
                or not isinstance(item.get("impact"), str)
                or not isinstance(item.get("unresolved"), bool)
                or not isinstance(item.get("related_use_case_ids"), list)
                or not isinstance(item.get("related_flow_ids"), list)
                or not isinstance(item.get("related_component_ids"), list)
            ):
                raise DridhError(ErrorCode.STATE_CORRUPT, "A persisted Technical Designer question is malformed.", details=(session_id, f"question {index}"))
        generated = payload.get("generated")
        if payload["status"] in {"IN_REVIEW", "APPROVED"} and (
            not isinstance(generated, dict)
            or not isinstance(generated.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", generated["sha256"])
            or not isinstance(generated.get("revision_id"), str)
            or not isinstance(payload.get("review_id"), str)
        ):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Generated TDD review metadata is invalid.", details=(session_id,))
        if payload["status"] == "APPROVED" and not isinstance(payload.get("approval_id"), str):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Approved TDD has no human approval record.", details=(session_id,))
        return payload

    def _render_notes(self, payload: dict[str, Any]) -> str:
        lines = [
            "# Technical Inquiry — Staging Notes",
            "",
            "> **STAGING — NOT THE CANONICAL TDD**",
            "",
            f"**Session:** {payload['session_id']}",
            f"**Approved Use Case Revision:** {payload['source']['revision_id']}",
            f"**Approved Use Case SHA-256:** {payload['source']['sha256']}",
            "",
            "## Feature and Objective",
            "",
            payload["objective"],
            "",
            "## Codebase Evidence",
            "",
            payload.get("code_reacher_evidence") or "OPEN — no Code Reacher evidence was supplied; confirm existing repository and service facts.",
            "",
            "## Inquirer Analysis",
            "",
            str(payload.get("inquirer", {}).get("summary", "OPEN")),
            "",
            "### Key Facts",
            "",
        ]
        lines.extend(f"- {item}" for item in payload.get("inquirer", {}).get("key_facts", []))
        lines.extend(["", "### Gaps", ""])
        lines.extend(f"- {item}" for item in payload.get("inquirer", {}).get("gaps", []))
        lines.extend(["", "## Technical Q&A", ""])
        lines.extend(_table(
            ("#", "Question ID", "Topic", "Question", "Answer", "Source", "Impact", "Status"),
            [[str(item["order"]), item["question_id"], item["topic"], item["question"], item["answer"] or "OPEN", item["source"], item["impact"], "OPEN" if item["unresolved"] or not item["answer"] else "ANSWERED"] for item in payload["questions"]],
        ).splitlines())
        lines.extend(["", "## Proposed Component Inventory", ""])
        components = payload.get("generated", {}).get("components", []) if isinstance(payload.get("generated"), dict) else []
        if components:
            lines.extend(_table(("Component ID", "Name", "Status", "Responsibility", "Use Cases / Flows"), [
                [item["component_id"], item["name"], item["status"], item["responsibility"], ", ".join(item.get("use_case_ids", []) + item.get("flow_ids", [])) or "N/A"]
                for item in components
            ]).splitlines())
        else:
            lines.append("OPEN — component definitions are staged by the Document Generator after inquiry completion.")
        lines.extend(["", "## UC / Flow to Component Mapping", ""])
        mappings = payload.get("generated", {}).get("traceability", []) if isinstance(payload.get("generated"), dict) else []
        if mappings:
            lines.extend(_table(("UC ID", "Flow IDs", "CMP IDs", "API / Event", "DD IDs"), [
                [item["uc_id"], ", ".join(item["flow_ids"]), ", ".join(item["component_ids"]), item["api_event"], ", ".join(item["decision_ids"]) or "N/A"]
                for item in mappings
            ]).splitlines())
        else:
            lines.append("OPEN — mapping is not generated yet.")
        lines.extend(["", "## Open Assumptions and Decision Candidates", ""])
        lines.extend(f"- {item}" for item in payload.get("inquirer", {}).get("gaps", []))
        return "\n".join(lines).rstrip() + "\n"

    def _save(self, payload: dict[str, Any]) -> None:
        json_path, notes_path = self._paths(payload["session_id"])
        payload["updated_at"] = _now()
        rendered = self._render_notes(payload)
        json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_pair(json_path, json_text, notes_path, rendered)

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
            timeout=float(os.environ.get("DRIDH_DESIGNER_WORKER_TIMEOUT", "600")),
            env=env,
        )
        return self.runner

    def _worker(
        self, agent: str, prompt: str, *, model: str | None = None,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Run a worker until its reply passes ``validate`` (at most three repairs, plan rule 9)."""

        def run(call_prompt: str, **kwargs: Any) -> Any:
            return self._runner().run_sync(
                agent=agent, prompt=call_prompt, model=model, title=f"Dridh Technical Designer: {agent}", **kwargs,
            )

        outcome = call_with_repair(run, prompt, label=_WORKER_LABELS.get(agent, agent), validate=validate)
        if outcome.attempts:
            self._repair_log.append({"agent": agent, "recorded_at": _now(), "attempts": [a.to_dict() for a in outcome.attempts]})
        return outcome.value

    @staticmethod
    def _check_inquirer(inquirer: dict[str, Any]) -> None:
        if not isinstance(inquirer.get("summary"), str):
            raise DridhError(ErrorCode.WORKER_FAILED, "Technical Inquirer output is missing its summary.",
                             details=("required keys: summary (string), key_facts, gaps (string arrays), recommended_questions (array)",))
        _strings(inquirer.get("key_facts", []), "inquirer.key_facts")
        _strings(inquirer.get("gaps", []), "inquirer.gaps")
        if not isinstance(inquirer.get("recommended_questions", []), list):
            raise DridhError(ErrorCode.WORKER_FAILED, "Technical Inquirer recommended_questions must be an array.")

    @staticmethod
    def _check_handoff(handoff: dict[str, Any]) -> None:
        if not isinstance(handoff.get("summary"), str):
            raise DridhError(ErrorCode.WORKER_FAILED, "Designer Handoff output is missing summary.",
                             details=("required keys: summary (string), concerns and open_blockers (string arrays)",))
        _strings(handoff.get("concerns", []), "handoff.concerns")
        _strings(handoff.get("open_blockers", []), "handoff.open_blockers")

    def start(
        self,
        ideator_session_id: str,
        *,
        feature: str,
        existing_context: str = "",
        code_reacher_evidence: str = "",
        model: str | None = None,
    ) -> dict[str, Any]:
        feature = _required_text(feature, "feature", limit=200)
        if not isinstance(existing_context, str) or not isinstance(code_reacher_evidence, str):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "existing_context and code_reacher_evidence must be strings.")
        existing_context = str(redact(existing_context))[:12000]
        code_reacher_evidence = str(redact(code_reacher_evidence))[:20000]
        if not code_reacher_evidence.strip() and self.workspace.mode == "existing":
            # Evidence comes from Dridh's own index, not from an agent's paraphrase (found in a real-model run: the
            # Coordinator described the code in free text and the TDD had to mark every repository fact unverified).
            from dridh.code_reacher.search import CodeSearch

            code_reacher_evidence = CodeSearch(self.workspace).evidence_summary()[:20000]
        slug = feature.lower().replace(" ", "-")
        if not FEATURE_SLUG.fullmatch(slug):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "feature must be a short lowercase slug or feature name containing letters, numbers and hyphens.")
        source = _approved_use_case(self.workspace, ideator_session_id)
        objective = f"Produce a technical design for {feature} strictly aligned to approved Use Cases."
        session_id = f"designer-{uuid.uuid4().hex[:12]}"
        questions = [
            {
                "question_id": f"TQ-{index:03d}",
                "topic": topic,
                "question": question,
                "order": index,
                "source": "Technical Designer Inquirer",
                "asked_at": _now(),
                "answer": "",
                "answered_at": None,
                "impact": impact,
                "unresolved": False,
                "related_use_case_ids": [],
                "related_flow_ids": [],
                "related_component_ids": [],
            }
            for index, (topic, question, impact) in enumerate(TOPICS, start=1)
        ]
        base = source["model"]
        baseline = {
            "actors": [{"id": item.actor_id, "name": item.actor_name, "description": item.description} for item in base.actors],
            "use_cases": [{"id": item.use_case_id, "title": item.title, "primary_actor_id": item.primary_actor_id, "trigger": item.trigger_text} for item in base.use_cases],
            "flows": [{"id": item.flow_id, "use_case_id": item.parent_use_case_id, "type": item.flow_type, "subject": item.subject, "actor_id": item.actor_id, "action": item.action_text, "result": item.result_text} for item in base.flows],
        }
        inquirer = self._worker(
            "dridh/workers/designer-inquirer",
            "You are the Phase 12 Technical Inquirer. Analyse the approved Use Case baseline, existing context and supplied Code Reacher evidence. "
            "Return JSON only with summary (string), key_facts (string array), gaps (string array), and recommended_questions "
            "(array of objects with topic and question). Do not invent repository facts. If Code Reacher evidence is absent, explicitly state that existing-codebase facts need owner confirmation. "
            "Keep all approved requirements immutable.\nAPPROVED BASELINE:\n" + json.dumps(baseline, ensure_ascii=False)
            + "\nEXISTING CONTEXT:\n" + existing_context[:12000]
            + "\nCODE REACHER EVIDENCE:\n" + (code_reacher_evidence[:20000] or "NONE — ask the owner to confirm existing systems and repository placement.")
        , model=model, validate=self._check_inquirer)
        key_facts = _strings(inquirer.get("key_facts", []), "inquirer.key_facts")
        gaps = _strings(inquirer.get("gaps", []), "inquirer.gaps")
        recommended = inquirer.get("recommended_questions", [])
        recommended_map = {
            item.get("topic"): item.get("question")
            for item in recommended
            if isinstance(item, dict) and item.get("topic") in {topic[0] for topic in TOPICS} and isinstance(item.get("question"), str) and item["question"].strip()
        }
        for item in questions:
            if item["topic"] in recommended_map:
                item["question"] = recommended_map[item["topic"]][:2000]
        payload = {
            "schema": SESSION_SCHEMA,
            "schema_version": SESSION_VERSION,
            "session_id": session_id,
            "status": "INQUIRY",
            "feature": feature,
            "feature_slug": slug,
            "objective": objective,
            "ideator_session_id": ideator_session_id,
            "source": {
                "path": "001-USE-CASES.md",
                "document_id": source["document_id"],
                "revision_id": source["revision_id"],
                "sha256": source["sha256"],
            },
            "existing_context": existing_context[:12000],
            "code_reacher_evidence": code_reacher_evidence[:20000],
            "inquirer": {"summary": inquirer["summary"][:8000], "key_facts": key_facts, "gaps": gaps},
            "questions": questions,
            "generated": None,
            "handoff": None,
            "review_id": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._save(payload)
        return self.status(session_id)

    def answer(
        self,
        session_id: str,
        *,
        topic: str,
        answer: str,
        source: str = "user",
        impact: str | None = None,
        unresolved: bool = False,
        related_use_case_ids: list[str] | None = None,
        related_flow_ids: list[str] | None = None,
        related_component_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if topic not in {item[0] for item in TOPICS}:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Unknown Technical Designer topic.", details=tuple(item[0] for item in TOPICS))
        if not isinstance(unresolved, bool) or not isinstance(answer, str) or len(answer) > 8000:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "answer must be a string of at most 8000 characters and unresolved must be boolean.")
        answer = str(redact(answer))
        if not answer.strip() and not unresolved:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "An empty answer must be explicitly marked unresolved.")
        source = _required_text(source, "source", limit=100)
        payload = self._load(session_id)
        self._validate_source_current(payload)
        if payload["status"] == "APPROVED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Approved Technical Designer inquiry is immutable; start a new session for a new baseline.")
        question = next(item for item in payload["questions"] if item["topic"] == topic)
        question["answer"] = answer.strip()
        question["answered_at"] = _now()
        question["source"] = source
        question["impact"] = (impact or question["impact"])[:1000]
        question["unresolved"] = unresolved
        question["related_use_case_ids"] = _strings(related_use_case_ids or [], "related_use_case_ids")
        question["related_flow_ids"] = _strings(related_flow_ids or [], "related_flow_ids")
        question["related_component_ids"] = _strings(related_component_ids or [], "related_component_ids")
        payload["status"] = "INQUIRY"
        payload["generated"] = None
        payload["handoff"] = None
        payload["review_id"] = None
        payload.pop("approval_id", None)
        payload.pop("approved_by", None)
        payload.pop("approved_at", None)
        self._save(payload)
        return self.status(session_id)

    def _missing_topics(self, payload: dict[str, Any]) -> list[str]:
        return [item["topic"] for item in payload["questions"] if not item.get("answered_at") or (not item.get("answer") and not item.get("unresolved"))]

    def parity(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        _, notes = self._paths(session_id)
        actual = notes.read_text(encoding="utf-8")
        expected = self._render_notes(payload)
        return {
            "ok": actual == expected,
            "session_id": session_id,
            "questions": len(payload["questions"]),
            "json_sha256": _sha(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)),
            "notes_sha256": _sha(actual),
            "finding": None if actual == expected else "TECHNICAL_INQUIRY_MARKDOWN_JSON_MISMATCH",
        }

    def status(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        parity = self.parity(session_id)
        return {
            "session_id": session_id,
            "status": payload["status"],
            "feature": payload["feature"],
            "questions_recorded": sum(1 for item in payload["questions"] if item.get("answered_at")),
            "topics_missing": self._missing_topics(payload),
            "unresolved_questions": [item["question_id"] for item in payload["questions"] if item.get("unresolved")],
            "parity": parity,
            "source": payload["source"],
            "document": payload.get("generated"),
            "handoff": payload.get("handoff"),
            "jev_checkpoint": payload.get("jev_checkpoint"),
            "review_id": payload.get("review_id"),
            "gate": self.gate(session_id),
        }

    def _validate_source_current(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = _approved_use_case(self.workspace, payload["ideator_session_id"])
        if source["revision_id"] != payload["source"]["revision_id"] or source["sha256"] != payload["source"]["sha256"]:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The approved Use Case source changed after Technical Designer discovery began.")
        return source

    def _validate_generation(self, generated: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        generated = redact(generated)
        components = generated.get("components")
        decisions = generated.get("decisions")
        risks = generated.get("risks", [])
        trace = generated.get("traceability")
        sections = generated.get("sections")
        if not isinstance(components, list) or not components or not isinstance(decisions, list) or not isinstance(risks, list) or not isinstance(trace, list) or not isinstance(sections, dict):
            raise DridhError(ErrorCode.WORKER_FAILED, "Designer Document Generator must return components, decisions, risks, traceability and sections.")
        component_names: list[str] = []
        component_keys: set[str] = set()
        for index, item in enumerate(components):
            if not isinstance(item, dict):
                raise DridhError(ErrorCode.WORKER_FAILED, "Every component definition must be an object.", details=(f"index {index}",))
            key = _required_text(item.get("key"), f"components[{index}].key", limit=80)
            name = _required_text(item.get("name"), f"components[{index}].name", limit=160)
            if "\n" in key or "\n" in name or "|" in name:
                raise DridhError(ErrorCode.WORKER_FAILED, "Component keys/names must be single-line and component names cannot contain table delimiters.", details=(name,))
            if key in component_keys or name in component_names:
                raise DridhError(ErrorCode.WORKER_FAILED, "Component keys and names must be unique.", details=(key, name))
            component_keys.add(key)
            component_names.append(name)
            _required_text(item.get("responsibility"), f"components[{index}].responsibility")
            if item.get("status") not in {"EXISTING", "NEW"}:
                raise DridhError(ErrorCode.WORKER_FAILED, "Component status must be EXISTING or NEW.", details=(name,))
            item["use_case_ids"] = _strings(item.get("use_case_ids", []), f"components[{index}].use_case_ids")
            item["flow_ids"] = _strings(item.get("flow_ids", []), f"components[{index}].flow_ids")
            for optional in ("owner", "inputs_outputs", "dependencies"):
                if item.get(optional) is not None and not isinstance(item[optional], str):
                    raise DridhError(ErrorCode.WORKER_FAILED, f"components[{index}].{optional} must be a string.")
            item.setdefault("owner", "OPEN — accountable owner confirmation required")
            item.setdefault("inputs_outputs", "OPEN — interface contracts need confirmation")
            item.setdefault("dependencies", "OPEN — dependency ownership needs confirmation")
        decision_keys: set[str] = set()
        for index, item in enumerate(decisions):
            if not isinstance(item, dict):
                raise DridhError(ErrorCode.WORKER_FAILED, "Every design decision must be an object.", details=(f"index {index}",))
            key = _required_text(item.get("key"), f"decisions[{index}].key", limit=80)
            if key in decision_keys:
                raise DridhError(ErrorCode.WORKER_FAILED, "Decision keys must be unique.", details=(key,))
            decision_keys.add(key)
            for field in ("title", "context", "decision", "alternatives", "rationale", "consequences", "reversibility", "evidence"):
                item[field] = _required_text(item.get(field), f"decisions[{index}].{field}")
            if "\n" in item["title"]:
                raise DridhError(ErrorCode.WORKER_FAILED, "Decision titles must be single-line.", details=(key,))
            references = _strings(item.get("component_keys", []), f"decisions[{index}].component_keys")
            if any(value not in component_keys for value in references):
                raise DridhError(ErrorCode.WORKER_FAILED, "Decision references an unknown component key.", details=tuple(references))
            item["component_keys"] = references
            item["related_ids"] = _strings(item.get("related_ids", []), f"decisions[{index}].related_ids")
        for index, item in enumerate(risks):
            if not isinstance(item, dict):
                raise DridhError(ErrorCode.WORKER_FAILED, "Every risk must be an object.", details=(f"index {index}",))
            for field in ("title", "description", "mitigation"):
                item[field] = _required_text(item.get(field), f"risks[{index}].{field}")
            if "\n" in item["title"]:
                raise DridhError(ErrorCode.WORKER_FAILED, "Risk titles must be single-line.", details=(str(index),))
        usecase_model = source["model"]
        expected_ucs = {item.use_case_id for item in usecase_model.use_cases}
        expected_flows = {item.flow_id for item in usecase_model.flows}
        covered_ucs: set[str] = set()
        covered_flows: set[str] = set()
        normalised_trace: list[dict[str, Any]] = []
        for index, item in enumerate(trace):
            if not isinstance(item, dict):
                raise DridhError(ErrorCode.WORKER_FAILED, "Every traceability row must be an object.", details=(f"index {index}",))
            uc_id = _required_text(item.get("uc_id"), f"traceability[{index}].uc_id", limit=20)
            if uc_id not in expected_ucs:
                raise DridhError(ErrorCode.WORKER_FAILED, "Traceability references an unknown approved Use Case.", details=(uc_id,))
            flow_ids = _strings(item.get("flow_ids", []), f"traceability[{index}].flow_ids")
            component_refs = _strings(item.get("component_keys", []), f"traceability[{index}].component_keys")
            decision_refs = _strings(item.get("decision_keys", []), f"traceability[{index}].decision_keys")
            if not flow_ids or any(flow not in expected_flows for flow in flow_ids):
                raise DridhError(ErrorCode.WORKER_FAILED, "Traceability must name actual approved flow IDs.", details=tuple(flow_ids))
            if not component_refs or any(key not in component_keys for key in component_refs):
                raise DridhError(ErrorCode.WORKER_FAILED, "Traceability must map to at least one defined component.", details=tuple(component_refs))
            if any(key not in decision_keys for key in decision_refs):
                raise DridhError(ErrorCode.WORKER_FAILED, "Traceability references an unknown design decision.", details=tuple(decision_refs))
            if any(next(f for f in usecase_model.flows if f.flow_id == flow).parent_use_case_id != uc_id for flow in flow_ids):
                raise DridhError(ErrorCode.WORKER_FAILED, "Traceability flow is not a child of the stated Use Case.", details=(uc_id, *flow_ids))
            covered_ucs.add(uc_id)
            covered_flows.update(flow_ids)
            normalised_trace.append({
                "uc_id": uc_id,
                "flow_ids": flow_ids,
                "component_keys": component_refs,
                "decision_keys": decision_refs,
                "api_event": str(item.get("api_event", "N/A")).strip() or "N/A",
            })
        missing_ucs = expected_ucs - covered_ucs
        missing_flows = expected_flows - covered_flows
        if missing_ucs or missing_flows:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "TDD traceability is incomplete; every approved Use Case and flow must be addressed.",
                details=tuple(sorted(missing_ucs | missing_flows)),
                recovery="Correct the traceability proposal. Unaddressed requirements cannot be silently omitted.",
            )
        normalised_sections = {}
        for key, value in sections.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise DridhError(ErrorCode.WORKER_FAILED, "TDD sections must map string names to string bodies.")
            if re.search(r"(?m)^##\s+", value):
                raise DridhError(ErrorCode.WORKER_FAILED, "Generated section bodies cannot introduce new level-two TDD sections.", details=(key,))
            normalised_sections[key] = str(redact(value.strip()))[:20000]
        return {
            "components": components,
            "decisions": decisions,
            "risks": risks,
            "traceability": normalised_trace,
            "sections": normalised_sections,
        }

    def _render_tdd(self, payload: dict[str, Any], generated: dict[str, Any], source: dict[str, Any], *, allocated: dict[str, Any], review_status: str = "DRAFT") -> str:
        bundle = load_contract_bundle(STANDARDS_VERSION)
        template_body, _ = sanitize_tdd(bundle.template_text)
        template_sections = tuple(
            line[3:].strip()
            for line in template_body.splitlines()
            if line.startswith("## ")
        )
        if template_sections != TDD_SECTIONS:
            raise DridhError(ErrorCode.CONFIGURATION_ERROR, "The installed TDD template headings differ from the validator's canonical section order.")
        metadata = bundle.render_metadata_block(source_revision=source["revision_id"], source_sha256=source["sha256"])
        components = generated["components"]
        decisions = generated["decisions"]
        risks = generated["risks"]
        trace = generated["traceability"]
        sections = generated["sections"]
        component_rows = allocated["components"]
        decision_rows = allocated["decisions"]
        risk_rows = allocated["risks"]
        component_ids = {item["key"]: item["component_id"] for item in component_rows}
        decision_ids = {item["key"]: item["decision_id"] for item in decision_rows}
        blocks: dict[str, str] = dict(DEFAULT_SECTIONS)
        blocks.update(sections)
        blocks["document_purpose"] = blocks.get("document_purpose") or f"Technical design for {payload['feature']}."
        blocks["design_details"] = "\n".join([
            "### System Context and Architecture",
            blocks.get("system_context", DEFAULT_SECTIONS["system_context"]),
            "",
            "### Component Responsibilities",
            *[
                "\n".join([
                    f"### {item['component_id']}: {item['name']}",
                    "",
                    f"- Status: {component['status']}.",
                    f"- Responsibility: {component['responsibility']}",
                    f"- Owner: {component.get('owner', 'OPEN — owner confirmation required')}",
                    f"- Inputs / Outputs / Contracts: {component.get('inputs_outputs', 'OPEN — contract details require confirmation')}",
                    f"- Dependencies: {component.get('dependencies', 'OPEN — dependency confirmation required')}",
                    f"- Related Use Cases / Flows: {', '.join(component.get('use_case_ids', []) + component.get('flow_ids', [])) or 'N/A'}",
                ])
                for item, component in zip(component_rows, components, strict=True)
            ],
            "",
            "### Internal and External Data Flow",
            blocks.get("internal_external_flow", DEFAULT_SECTIONS["internal_external_flow"]),
            "",
            "### Use Case-to-Design Traceability Matrix",
            "",
            _table(H_TRACE, [
                [
                    row["uc_id"],
                    ", ".join(row["flow_ids"]),
                    ", ".join(f"{component_ids[key]} — {next(c['name'] for c in components if c['key'] == key)}" for key in row["component_keys"]),
                    row["api_event"],
                    ", ".join(decision_ids[key] for key in row["decision_keys"]) or "N/A",
                ]
                for row in trace
            ]),
            "",
            "### Data Ownership and Persistence",
            blocks.get("data_ownership", DEFAULT_SECTIONS["data_ownership"]),
            "",
            "### Scalability and Future Evolution",
            blocks.get("scalability", DEFAULT_SECTIONS["scalability"]),
        ])
        decision_blocks: list[str] = []
        for id_row, decision in zip(decision_rows, decisions, strict=True):
            affected = []
            for key in decision.get("component_keys", []):
                component_name = next(component["name"] for component in components if component["key"] == key)
                affected.append(f"{component_ids[key]} — {component_name}")
            decision_blocks.append("\n".join([
                f"#### {id_row['decision_id']}: {decision['title']}",
                "",
                f"- **Context / Problem:** {decision['context']}",
                f"- **Decision:** {decision['decision']}",
                f"- **Alternatives Considered:** {decision['alternatives']}",
                f"- **Rationale:** {decision['rationale']}",
                f"- **Consequences / Trade-offs:** {decision['consequences']}",
                f"- **Affected Components:** {', '.join(affected) or 'N/A'}",
                f"- **Related Use Cases / Flows:** {', '.join(decision.get('related_ids', [])) or 'N/A'}",
                f"- **Reversibility:** {decision['reversibility']}",
                f"- **Evidence / References:** {decision['evidence']}",
            ]))
        blocks["design_decisions"] = "\n\n".join(decision_blocks) or "N/A — no material feature-wide decisions were proposed. Review this with the architecture owner."
        blocks["open_questions"] = "\n".join([
            "Open discovery questions remain visible below. An OPEN answer is not silently resolved by the generated TDD.",
            "",
            _table(("#", "Question", "Asked In", "Answer", "Source", "Impact"), [
                [str(item["order"]), item["question"], "Technical Designer inquiry", item["answer"] if item["answer"] else "OPEN", item["source"], item["impact"]]
                for item in payload["questions"]
            ] or [["—", "—", "—", "—", "—", "—"]]),
        ])
        blocks["risks"] = "\n\n".join(
            "\n".join([
                f"### {item['risk_id']}: {risk['title']}",
                "",
                risk["description"],
                "",
                f"Mitigation: {risk['mitigation']}",
            ])
            for item, risk in zip(risk_rows, risks, strict=True)
        ) or "N/A — no Stage 2 risks were identified by the current evidence; review this assertion before approval."
        ai_parts = []
        for heading, key in (
            ("### Section 1: Purpose & Accountability", "ai_purpose"),
            ("#### 1.1 AI System Type", "ai_type"),
            ("#### 1.2 Risk Tier Assessment", "ai_risk"),
            ("#### 1.3 Primary Purpose", "ai_primary_purpose"),
            ("#### 1.4 Accountable Owner", "ai_owner"),
            ("### Section 2: Risk & Impact", "ai_risk_impact"),
            ("#### 2.1 Primary Risks", "ai_primary_risks"),
            ("#### 2.2 Risk Mitigation", "ai_mitigation"),
            ("### Section 3: Data & Privacy", "ai_data"),
            ("#### 3.1 Data Sources", "ai_data_sources"),
            ("#### 3.2 PII Handling", "ai_pii"),
            ("### Section 4: Bias, Fairness & Safety", "ai_safety"),
            ("#### 4.1 Potential Issues", "ai_bias"),
            ("#### 4.2 Mitigation Steps", "ai_safety_mitigation"),
            ("### Section 5: Performance & Evaluation", "ai_performance"),
            ("#### 5.1 Pre-deployment Evaluation", "ai_eval"),
            ("#### 5.2 Production Monitoring", "ai_monitoring"),
            ("#### 5.3 Update Process", "ai_update"),
        ):
            ai_parts.extend([heading, "", blocks.get(key, "N/A — this design does not introduce an AI system; revisit if scope changes."), ""])
        blocks["ai_systems"] = "\n".join(ai_parts).strip()
        blocks["blocking_reviewers"] = blocks.get("blocking_reviewers") or "OPEN — an architecture reviewer and owners of impacted systems must be named before approval."
        blocks["scope"] = blocks.get("scope") or "Derived from the approved Use Case scope; no new functional behaviour may be introduced here."
        blocks["out_of_scope"] = blocks.get("out_of_scope") or "Preserve the approved Use Case out-of-scope requirements; no additional exclusions were approved."
        blocks["target_dates"] = blocks.get("target_dates") or "N/A — no committed dates have been supplied."
        blocks["api_design"] = blocks.get("api_design") or "N/A — no API details were supplied; confirm with the API owner before approval."
        blocks["events"] = blocks.get("events") or "N/A — no event details were supplied; confirm with the integration owner before approval."
        blocks["tdd_checklist"] = "\n".join(f"- [ ] {item}" for item in REVIEW_CHECKLIST)
        feature = payload["feature"]
        lines = [
            f"# {feature} — Technical Design Document",
            "",
            metadata,
            "",
            f"**Document Status:** {review_status}",
            f"**Last Updated:** {datetime.now(UTC).date().isoformat()}",
            f"**Feature:** {feature}",
            f"**Owner:** {blocks.get('owner', 'OPEN — owner confirmation required')}",
            "**Source Use Case Document:** 001-USE-CASES.md",
            "",
        ]
        aliases = {
            "Document Control": ["document_purpose"],
            "Blocking Reviewers": ["blocking_reviewers"],
            "Problem Statement & Resources": ["problem_resources"],
            "Definitions": ["definitions"],
            "Scope": ["scope"],
            "Out of Scope": ["out_of_scope"],
            "Target Dates and Milestones": ["target_dates"],
            "Design Summary": ["design_summary"],
            "Design Details": ["design_details"],
            "Design Decisions": ["design_decisions"],
            "Dependencies": ["dependencies"],
            "API Design": ["api_design"],
            "Events": ["events"],
            "Data Model": ["data_model"],
            "Quality Approach": ["quality_approach"],
            "Well-Architected Considerations": ["cost", "security_privacy"],
            "Expected Level of Effort": ["effort"],
            "Open Questions And Assumptions": ["open_questions"],
            "OPTIONAL — Technical Overview Supporting Sections": ["technical_alternatives"],
            "Technical Justifications / Alternative Designs": ["technical_alternatives"],
            "Integration/Migration Plan": ["migration"],
            "Rollback Plan": ["rollback"],
            "Feature Flags": ["feature_flags"],
            "Feature/Service Logic": ["feature_logic"],
            "Scaling Estimate": ["scaling_estimate"],
            "Monitoring & Alerting": ["monitoring"],
            "Failure Scenarios And Recovery": ["failure_recovery"],
            "Risks": ["risks"],
            "Acceptance Criteria": ["acceptance"],
            "AI Systems": ["ai_systems"],
            "TDD Completion Checklist": ["tdd_checklist"],
            "Document References": ["references"],
        }
        for section in TDD_SECTIONS:
            lines.extend([f"## {section}", ""])
            keys = aliases.get(section, [])
            if section == "Well-Architected Considerations":
                lines.extend(["### Cost Considerations", "", blocks["cost"], "", "### Security & Privacy Considerations", "", blocks["security_privacy"], ""])
            else:
                body = "\n\n".join(blocks.get(key, DEFAULT_SECTIONS.get(key, "OPEN — requires owner confirmation.")) for key in keys) if keys else "OPEN — requires owner confirmation."
                lines.extend([body, ""])
        document, _ = sanitize_tdd("\n".join(lines).rstrip() + "\n")
        return document

    def generate(self, session_id: str, *, model: str | None = None) -> dict[str, Any]:
        self._repair_log = []
        payload = self._load(session_id)
        if payload["status"] != "INQUIRY":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Only an active Technical Designer inquiry can be generated.")
        if not self.parity(session_id)["ok"]:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Technical inquiry JSON and Markdown are not in parity.")
        missing = self._missing_topics(payload)
        if missing:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Technical inquiry is incomplete.", details=tuple(missing))
        source = self._validate_source_current(payload)
        generated = self._worker(
            "dridh/workers/designer-doc-generator",
            "Generate a complete technical design proposal from the approved Use Case baseline and technical inquiry. "
            "Return JSON only with sections (object mapping section keys to concise Markdown text), components, decisions, risks and traceability. "
            "Never invent codebase facts: mark missing evidence OPEN and request confirmation. Preserve every approved Use Case and flow. "
            "Component items: key,name,status(EXISTING/NEW),responsibility,owner,inputs_outputs,dependencies,use_case_ids,flow_ids. "
            "Decision items: key,title,context,decision,alternatives,rationale,consequences,reversibility,evidence,component_keys,related_ids. "
            "Risk items: title,description,mitigation. Traceability items: uc_id,flow_ids,component_keys,decision_keys,api_event. "
            "Do not allocate CMP, DD or R IDs. Do not mark requirements approved.\nSOURCE:\n"
            + json.dumps({"revision_id": source["revision_id"], "sha256": source["sha256"], "model": {
                "use_cases": [{"id": item.use_case_id, "title": item.title} for item in source["model"].use_cases],
                "flows": [{"id": item.flow_id, "uc": item.parent_use_case_id} for item in source["model"].flows],
                "actors": [{"id": item.actor_id, "name": item.actor_name} for item in source["model"].actors],
            }}, ensure_ascii=False)
            + "\nINQUIRY:\n" + json.dumps(payload, ensure_ascii=False),
            model=model,
            validate=lambda reply: self._validate_generation(reply, source),
        )
        target = self.project_root / f"{payload['feature_slug']}_tdd.md"
        if target.exists() and (not target.is_file() or target.is_symlink()):
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The target TDD path is not a regular file.")
        if target.exists():
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                current = DocumentRepository(store.connection, self.workspace.workspace_id).current_revision(target.name)
                if current is None or content_hash(target.read_text(encoding="utf-8")) != current.content_sha256:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "An unmanaged or edited TDD already exists; it was not overwritten.")
        previous = target.read_bytes() if target.exists() else None
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                allocator = CoordinatorIdAllocator(connection)
                if previous is not None:
                    allocator.reserve_document_ids(previous.decode("utf-8"), source_document=target.name, document_type="tdd")
                component_rows = []
                for component in generated["components"]:
                    component_rows.append({"key": component["key"], "component_id": allocator.allocate("component", source_document=target.name), "name": component["name"]})
                decision_rows = []
                for decision in generated["decisions"]:
                    decision_rows.append({"key": decision["key"], "decision_id": allocator.allocate("design_decision", source_document=target.name)})
                risk_rows = []
                for _risk in generated["risks"]:
                    risk_rows.append({"risk_id": allocator.allocate("risk", source_document=target.name)})
                allocated = {"components": component_rows, "decisions": decision_rows, "risks": risk_rows}
                document = self._render_tdd(payload, generated, source, allocated=allocated)
                validation = validate_tdd(document, file=target.name)
                if not validation.ok:
                    raise DridhError(ErrorCode.VALIDATION_FAILED, "Generated TDD failed deterministic validation.", details=tuple(validation.errors[:20]))
                # Verify Table 13 covers every approved Use Case/flow before recording the revision.
                tdd_model = validated_tdd_model(document, target.name)
                revision, _ = DocumentRepository(connection, self.workspace.workspace_id).record_revision(
                    relative_path=target.name,
                    document_type="tdd",
                    content=document,
                    standards_version=STANDARDS_VERSION,
                    source_path=target.name,
                )
                metadata = DocumentRepository(connection, self.workspace.workspace_id).document_metadata(target.name)
                assert metadata is not None
                validation_id = DocumentRepository(connection, self.workspace.workspace_id).record_validation(
                    revision_id=revision.id,
                    validator=TDD_VALIDATOR,
                    validator_version=TDD_VALIDATOR_VERSION,
                    result=validation,
                )
                design = DesignRepository(connection, self.workspace.workspace_id)
                counts = design.replace_tdd(document_id=str(metadata["id"]), revision_id=revision.id, model=tdd_model)
                findings = design.findings(relative_path=target.name, document_id=str(metadata["id"]), kind="TDD", current_revision_id=revision.id, current_content=document)
                if findings:
                    raise DridhError(ErrorCode.VALIDATION_FAILED, "SQLite design reconciliation is not clean.", details=tuple(finding.message for finding in findings[:10]))
                reviews = ReviewRepository(connection, self.workspace.workspace_id)
                review_id = reviews.create_review(document_id=str(metadata["id"]), revision_id=revision.id, reviewer="Technical Designer Review", actor="coordinator:technical-designer")
                for item in REVIEW_CHECKLIST:
                    reviews.set_checklist(review_id, item, False, actor="coordinator:technical-designer")
                component_ids = {item["key"]: item["component_id"] for item in component_rows}
                decision_ids = {item["key"]: item["decision_id"] for item in decision_rows}
                payload["generated"] = {
                    "path": target.name,
                    "sha256": content_hash(document),
                    "revision_id": revision.id,
                    "validation_id": validation_id,
                    "component_ids": [item["component_id"] for item in component_rows],
                    "decision_ids": [item["decision_id"] for item in decision_rows],
                    "risk_ids": [item["risk_id"] for item in risk_rows],
                    "components": [
                        {
                            "component_id": row["component_id"],
                            "name": component["name"],
                            "status": component["status"],
                            "responsibility": component["responsibility"],
                            "use_case_ids": component["use_case_ids"],
                            "flow_ids": component["flow_ids"],
                        }
                        for row, component in zip(component_rows, generated["components"], strict=True)
                    ],
                    "traceability": [
                        {
                            "uc_id": row["uc_id"],
                            "flow_ids": row["flow_ids"],
                            "component_ids": [component_ids[key] for key in row["component_keys"]],
                            "decision_ids": [decision_ids[key] for key in row["decision_keys"]],
                            "api_event": row["api_event"],
                        }
                        for row in generated["traceability"]
                    ],
                    "counts": counts,
                }
                payload["review_id"] = review_id
                payload["handoff"] = None
                payload["status"] = "IN_REVIEW"
                payload["generated_at"] = _now()
        try:
            _atomic_write(target, document)  # a crash mid-write leaves the previous document, never a half-written one
            self._save(payload)
            try:
                handoff = self._worker(
                    "dridh/workers/designer-handoff",
                    "Review this TDD and its traceability against the checklist. Return JSON only with summary(string), concerns(string array), and open_blockers(string array). "
                    "Do not approve. Deterministic validation and human review are authoritative.\nCHECKLIST:\n"
                    + json.dumps(list(REVIEW_CHECKLIST))
                    + "\nTDD:\n" + document
                    + "\nINQUIRY:\n" + json.dumps(payload, ensure_ascii=False),
                    model=model,
                    validate=self._check_handoff,
                )
                payload["handoff"] = {
                    "summary": handoff["summary"][:5000],
                    "concerns": _strings(handoff.get("concerns", []), "handoff.concerns"),
                    "open_blockers": _strings(handoff.get("open_blockers", []), "handoff.open_blockers"),
                }
            except DridhError as exc:
                payload["handoff"] = {
                    "summary": "Automated Handoff review did not complete; human review remains mandatory.",
                    "concerns": [exc.message],
                    "open_blockers": ["Handoff worker evidence unavailable; reviewers must inspect all checklist items."],
                }
            if self._repair_log:
                payload["repair_history"] = list(self._repair_log)
            from dridh.jev.checkpoints import run_checkpoint

            generated = payload.get("generated") or {}
            payload["jev_checkpoint"] = run_checkpoint(
                self.workspace, stage="designer", subject_id=session_id, agent_id="dridh/technical-designer", summary=(
                    f"Technical Design Document validated. Components: {len(generated.get('component_ids', []))}; "
                    f"decisions: {len(generated.get('decision_ids', []))}; risks: {len(generated.get('risk_ids', []))}; "
                    f"every approved Use Case and flow traced: yes."),
                metadata={"source_revision_sha": generated.get("sha256")})
            self._save(payload)
        except BaseException:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
            raise
        return self.status(session_id)

    def _validate_source_current(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _approved_use_case(self.workspace, payload["ideator_session_id"])

    def approval_challenge(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        if payload["status"] != "IN_REVIEW" or not isinstance(payload.get("generated"), dict) or not payload.get("review_id"):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "This TDD is not awaiting human review.")
        return {
            "review_id": payload["review_id"],
            "revision_id": payload["generated"]["revision_id"],
            "confirmation": f"APPROVE {payload['review_id']} {payload['generated']['revision_id'][:12]}",
            "document": payload["generated"]["path"],
            "sha256": payload["generated"]["sha256"],
        }

    def approve(self, session_id: str, *, reviewer: str, confirmation: str) -> dict[str, Any]:
        payload = self._load(session_id)
        challenge = self.approval_challenge(session_id)
        if confirmation != challenge["confirmation"]:
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The TDD approval confirmation did not match.")
        reviewer = _required_text(reviewer, "reviewer", limit=100)
        source = self._validate_source_current(payload)
        if source["revision_id"] != payload["source"]["revision_id"] or source["sha256"] != payload["source"]["sha256"]:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The approved Use Case source changed; TDD approval is refused.")
        target = self.project_root / payload["generated"]["path"]
        if not target.is_file() or target.is_symlink():
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The TDD is missing or is not a regular file.")
        document = target.read_text(encoding="utf-8")
        if content_hash(document) != payload["generated"]["sha256"]:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The TDD changed after review; approval is refused.")
        validation = validate_tdd(document, file=target.name)
        if not validation.ok:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The current TDD no longer passes deterministic validation.", details=tuple(validation.errors[:10]))
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                documents = DocumentRepository(connection, self.workspace.workspace_id)
                metadata = documents.document_metadata(target.name)
                revision = documents.current_revision(target.name)
                if metadata is None or revision is None or revision.id != challenge["revision_id"]:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The TDD revision is stale; approval is refused.")
                latest = documents.latest_validation(revision.id, TDD_VALIDATOR)
                if latest is None or latest.status != "PASS":
                    raise DridhError(ErrorCode.VALIDATION_FAILED, "The TDD revision has no passing stored validation.")
                design = DesignRepository(connection, self.workspace.workspace_id)
                findings = design.findings(relative_path=target.name, document_id=str(metadata["id"]), kind="TDD", current_revision_id=revision.id, current_content=documents.stored_content(revision.id))
                if findings or not design.is_current(document_id=str(metadata["id"]), revision_id=revision.id, kind="TDD", model=validated_tdd_model(document, target.name)):
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The TDD design projection is stale or inconsistent.", details=tuple(item.message for item in findings[:10]))
                approval_id = ReviewRepository(connection, self.workspace.workspace_id).decide(
                    payload["review_id"],
                    decision="APPROVED",
                    reviewer=reviewer,
                    reason="Human approved the exact Technical Design Document revision.",
                    actor=f"human:{reviewer}",
                    revision_id=revision.id,
                )
        payload["status"] = "APPROVED"
        payload["approval_id"] = approval_id
        payload["approved_by"] = reviewer
        payload["approved_at"] = _now()
        self._save(payload)
        result = self.gate(session_id)
        if not result["unlocked"]:
            raise DridhError(ErrorCode.STATE_CORRUPT, "TDD approval was recorded but the Planner prerequisite gate remains closed.", details=(result["reason"],))
        return {"ok": True, "session_id": session_id, "status": "APPROVED", "approval_id": approval_id, "gate": result}

    def gate(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            return {"unlocked": False, "stage": "dridh/planner", "reason": "A Technical Designer session must be bound to the current approved Use Case baseline."}
        payload = self._load(session_id)
        if payload["status"] != "APPROVED":
            return {"unlocked": False, "stage": "dridh/planner", "reason": "The TDD has not received revision-bound human approval."}
        try:
            source = self._validate_source_current(payload)
        except DridhError as exc:
            return {"unlocked": False, "stage": "dridh/planner", "reason": exc.message}
        if source["revision_id"] != payload["source"]["revision_id"] or source["sha256"] != payload["source"]["sha256"]:
            return {"unlocked": False, "stage": "dridh/planner", "reason": "The approved Use Case source revision changed; TDD approval is stale."}
        generated = payload["generated"]
        target = self.project_root / generated["path"]
        if not target.is_file() or target.is_symlink():
            return {"unlocked": False, "stage": "dridh/planner", "reason": "The approved TDD file is missing."}
        document = target.read_text(encoding="utf-8")
        if content_hash(document) != generated["sha256"]:
            return {"unlocked": False, "stage": "dridh/planner", "reason": "The on-disk TDD differs from its approved SHA-256."}
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            documents = DocumentRepository(store.connection, self.workspace.workspace_id)
            metadata = documents.document_metadata(generated["path"])
            revision = documents.current_revision(generated["path"])
            if metadata is None or revision is None or revision.id != generated["revision_id"] or revision.content_sha256 != generated["sha256"]:
                return {"unlocked": False, "stage": "dridh/planner", "reason": "The stored TDD revision is not the approved revision."}
            latest = documents.latest_validation(revision.id, TDD_VALIDATOR)
            if latest is None or latest.status != "PASS":
                return {"unlocked": False, "stage": "dridh/planner", "reason": "The approved TDD revision no longer has a passing deterministic validation."}
            review = store.connection.execute(
                "SELECT id FROM human_approval WHERE id=? AND review_id=? AND revision_id=? AND decision='APPROVED'",
                (payload.get("approval_id"), payload["review_id"], revision.id),
            ).fetchone()
            if review is None:
                return {"unlocked": False, "stage": "dridh/planner", "reason": "The exact TDD revision has no matching human approval."}
            design = DesignRepository(store.connection, self.workspace.workspace_id)
            findings = design.findings(relative_path=generated["path"], document_id=str(metadata["id"]), kind="TDD", current_revision_id=revision.id, current_content=documents.stored_content(revision.id))
            if findings or not design.is_current(document_id=str(metadata["id"]), revision_id=revision.id, kind="TDD", model=validated_tdd_model(document, generated["path"])):
                return {"unlocked": False, "stage": "dridh/planner", "reason": findings[0].message if findings else "The SQLite design projection differs from the approved TDD."}
        return {
            "unlocked": True,
            "stage": "dridh/planner",
            "session_id": session_id,
            "tdd_path": generated["path"],
            "tdd_revision_id": generated["revision_id"],
            "tdd_sha256": generated["sha256"],
            "source_use_case_revision_id": source["revision_id"],
            "approved_by": payload["approved_by"],
            "approved_at": payload["approved_at"],
        }

    @staticmethod
    def require_planner_gate(workspace: WorkspaceInfo) -> dict[str, Any] | None:
        """Require an approved TDD whenever a workspace has a canonical managed Use Case baseline."""
        canonical = workspace.project_root / "001-USE-CASES.md"
        if not canonical.exists():
            return None
        use_case_gate = IdeatorService(workspace).gate()
        if not use_case_gate.get("unlocked"):
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "Planner is blocked because the approved Use Case baseline is not current.", details=(str(use_case_gate.get("reason", "")),))
        root = workspace.root / "artifacts" / "designer"
        if not root.is_dir():
            raise DridhError(ErrorCode.DEPENDENCY_NOT_READY, "Planner is blocked until Technical Designer produces and approves a TDD.")
        baseline_revision = use_case_gate.get("source_revision_id")
        for folder in root.iterdir():
            if not folder.is_dir() or not SESSION_ID.fullmatch(folder.name):
                continue
            json_path = folder / "technical-inquiry.json"
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if payload.get("status") == "APPROVED" and payload.get("source", {}).get("revision_id") == baseline_revision:
                gate = DesignerService(workspace).gate(payload.get("session_id"))
                if gate.get("unlocked"):
                    return gate
        raise DridhError(
            ErrorCode.DEPENDENCY_NOT_READY,
            "Planner is blocked until a TDD for the current approved Use Case revision receives explicit human approval.",
            details=(f"Use Case revision: {baseline_revision}",),
            recovery="Run dridh designer start, complete inquiry, generate the TDD, then approve it in an interactive terminal.",
        )
