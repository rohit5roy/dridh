"""Phase 11 Ideator workflow: resumable discovery, deterministic documents and approval gates.

The conversation is captured as workspace-local JSON. Markdown notes are a deterministic
projection of that JSON; Python owns IDs, validation, SQLite revisions and approval gates.
LLM workers may propose content, but their output is never trusted without validation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.document_contracts.identifiers import CoordinatorIdAllocator
from dridh.document_contracts.validator import STANDARDS_VERSION, validate_use_cases
from dridh.integrations.opencode import OpenCodeRunner
from dridh.jev import JevClient
from dridh.orchestration.repair import call_with_repair
from dridh.state.documents import DocumentRepository, content_hash
from dridh.state.reviews import ReviewRepository
from dridh.state.store import StateStore
from dridh.state.usecases import UseCaseRepository, validated_model
from dridh.workspace.layout import WorkspaceInfo

SESSION_SCHEMA = "dridh.inquiry"
SESSION_VERSION = 1
SESSION_ID = re.compile(r"^inquiry-[0-9a-f]{12}$")
TOPICS = (
    "problem", "why_for_whom", "solution", "scope", "out_of_scope", "actors",
    "journey", "success_flow", "alternate_flow", "failure_handling",
    "boundary_cases", "business_rules", "priority", "assumptions",
)
PRIORITIES = {"HIGH", "MEDIUM", "LOW"}
ACTOR_TYPES = {"USER", "SYSTEM", "SERVICE"}
ACTOR_STATUSES = {"EXISTING", "NEW"}
FLOW_TYPES = {
    "SUCCESS": "success_flow",
    "ALTERNATIVE": "alternative_flow",
    "EXCEPTION": "exception_flow",
    "BOUNDARY": "boundary_flow",
    "BUSINESS_RULE": "business_rule",
}
FLOW_PREFIX = {"SUCCESS": "SF", "ALTERNATIVE": "AF", "EXCEPTION": "EF", "BOUNDARY": "BE", "BUSINESS_RULE": "BR"}
CHECKLIST = (
    "Problem statement and scope reflect the approved discovery answers",
    "Every actor is registered and has a valid ACT-NN identifier",
    "Every use case has a summary row and a matching UC-NN section",
    "Every use case has an ordered primary success flow",
    "Every branch references an exact success-flow origin in the same use case",
    "Every actor and flow reference resolves",
    "Priority, assumptions, constraints and out-of-scope behaviour are documented",
    "Q&A log represents the complete discovery session",
    "The canonical v2.1.0 Use Case validator passes",
    "The generated source revision and SQLite projection match",
)


class IdeatorError(DridhError):
    """Invalid discovery, proposal or generated document; reported as deterministic validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.VALIDATION_FAILED, message)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cell(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return text.replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_pair(json_path: Path, json_text: str, notes_path: Path, notes_text: str) -> None:
    """Stage both projections before publishing either; restore the first on second-file failure."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, str]] = []
    backups: list[tuple[Path, str | None]] = []
    try:
        for target, body in ((json_path, json_text), (notes_path, notes_text)):
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((target, temporary))
            backup = None
            if target.exists():
                fd, backup = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".bak", dir=target.parent)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(target.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
            backups.append((target, backup))
        published: list[Path] = []
        try:
            for target, temporary in staged:
                os.replace(temporary, target)
                published.append(target)
        except BaseException:
            for target in reversed(published):
                backup = next((item[1] for item in backups if item[0] == target), None)
                if backup and Path(backup).exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    finally:
        for _, temporary in staged:
            Path(temporary).unlink(missing_ok=True)
        for _, backup in backups:
            if backup:
                Path(backup).unlink(missing_ok=True)


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise IdeatorError(f"{field} must be an array of non-empty strings")
    return [item.strip() for item in value]


_WORKER_LABELS = {
    "dridh/workers/ideator-inquirer": "Ideator Inquirer",
    "dridh/workers/ideator-doc-generator": "Ideator Document Generator",
    "dridh/workers/ideator-handoff": "Ideator Handoff",
}


class IdeatorService:
    def __init__(
        self,
        workspace: WorkspaceInfo,
        *,
        runner: Any | None = None,
        jev_client: JevClient | None = None,
    ) -> None:
        self.workspace = workspace
        self.project_root = workspace.project_root.resolve()
        self.state_root = workspace.root.resolve()
        self.runner = runner
        self.jev_client = jev_client
        self._repair_log: list[dict[str, Any]] = []

    def _session_dir(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise IdeatorError("session_id must look like inquiry-0123456789ab")
        return self.state_root / "artifacts" / "ideator" / session_id

    def _paths(self, session_id: str) -> tuple[Path, Path]:
        folder = self._session_dir(session_id)
        return folder / "inquiry.json", folder / "INQUIRY-NOTES.md"

    def _load(self, session_id: str) -> dict[str, Any]:
        json_path, notes_path = self._paths(session_id)
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "The Ideator inquiry session is missing or unreadable.",
                details=(session_id,),
                recovery="Restore inquiry.json from workspace backup or start a new discovery session.",
            ) from None
        if not isinstance(payload, dict) or payload.get("schema") != SESSION_SCHEMA or payload.get("schema_version") != SESSION_VERSION:
            raise DridhError(ErrorCode.SCHEMA_UNSUPPORTED, "Unsupported Ideator inquiry session schema.", details=(session_id,))
        if payload.get("session_id") != session_id or not notes_path.is_file():
            raise DridhError(ErrorCode.STATE_CORRUPT, "The inquiry session projections are incomplete.", details=(session_id,))
        status = payload.get("status")
        objective = payload.get("objective")
        context = payload.get("existing_context")
        questions = payload.get("questions")
        if status not in {"DISCOVERY", "IN_REVIEW", "APPROVED"}:
            raise DridhError(ErrorCode.STATE_CORRUPT, "The persisted Ideator status is invalid.", details=(session_id,))
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 8000:
            raise DridhError(ErrorCode.STATE_CORRUPT, "The persisted Ideator objective is invalid.", details=(session_id,))
        if not isinstance(context, str) or len(context) > 12000 or not isinstance(questions, list):
            raise DridhError(ErrorCode.STATE_CORRUPT, "The persisted Ideator context or questions are invalid.", details=(session_id,))
        for index, item in enumerate(questions, start=1):
            if not isinstance(item, dict):
                raise DridhError(ErrorCode.STATE_CORRUPT, "An inquiry question is not an object.", details=(session_id, f"index {index}"))
            if (
                item.get("question_id") != f"Q-{index:03d}"
                or item.get("order") != index
                or item.get("topic") not in TOPICS
                or not isinstance(item.get("question"), str)
                or not item["question"].strip()
                or len(item["question"]) > 2000
                or not isinstance(item.get("answer"), str)
                or len(item["answer"]) > 8000
                or not isinstance(item.get("source"), str)
                or not item["source"].strip()
                or not isinstance(item.get("unresolved"), bool)
                or not isinstance(item.get("asked_at"), str)
                or not isinstance(item.get("answered_at"), str)
                or not isinstance(item.get("actor_refs"), list)
                or any(not isinstance(ref, str) or not ref.strip() for ref in item["actor_refs"])
                or not isinstance(item.get("flow_refs"), list)
                or any(not isinstance(ref, str) or not ref.strip() for ref in item["flow_refs"])
            ):
                raise DridhError(ErrorCode.STATE_CORRUPT, "An inquiry question does not satisfy the persisted session schema.", details=(session_id, f"question {index}"))
        generated = payload.get("generated")
        if status in {"IN_REVIEW", "APPROVED"}:
            if (
                not isinstance(generated, dict)
                or not isinstance(generated.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", generated["sha256"])
                or not isinstance(generated.get("revision_id"), str)
                or not isinstance(payload.get("review_id"), str)
                or generated.get("review_id") != payload["review_id"]
            ):
                raise DridhError(ErrorCode.STATE_CORRUPT, "The generated document review metadata is invalid.", details=(session_id,))
        if status == "APPROVED" and not isinstance(payload.get("approval_id"), str):
            raise DridhError(ErrorCode.STATE_CORRUPT, "The approved inquiry is missing its human approval record.", details=(session_id,))
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        json_path, notes_path = self._paths(str(payload["session_id"]))
        payload["updated_at"] = _now()
        rendered = self._render_notes(payload)
        json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_pair(json_path, json_text, notes_path, rendered)

    def start(self, objective: str, *, existing_context: str = "") -> dict[str, Any]:
        objective = objective.strip()
        if not objective or len(objective) > 8000:
            raise IdeatorError("objective must contain 1–8000 characters")
        session_id = f"inquiry-{uuid.uuid4().hex[:12]}"
        payload: dict[str, Any] = {
            "schema": SESSION_SCHEMA,
            "schema_version": SESSION_VERSION,
            "session_id": session_id,
            "status": "DISCOVERY",
            "objective": objective,
            "existing_context": existing_context.strip()[:12000],
            "questions": [],
            "proposal": None,
            "discovery_summary": None,
            "generated": None,
            "handoff": None,
            "jev_checkpoint": None,
            "review_id": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._save(payload)
        return self.status(session_id)

    def record_answer(
        self,
        session_id: str,
        *,
        topic: str,
        question: str,
        answer: str,
        source: str = "user",
        unresolved: bool = False,
        actor_refs: list[str] | None = None,
        flow_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        if topic not in TOPICS:
            raise IdeatorError(f"topic must be one of: {', '.join(TOPICS)}")
        question, answer = question.strip(), answer.strip()
        if not question or len(question) > 2000:
            raise IdeatorError("question must contain 1–2000 characters")
        if len(answer) > 8000:
            raise IdeatorError("answer must contain at most 8000 characters")
        if not isinstance(source, str) or not source.strip():
            raise IdeatorError("source must be a non-empty string")
        if not answer and not unresolved:
            raise IdeatorError("An empty answer must be explicitly marked unresolved")
        if not isinstance(unresolved, bool):
            raise IdeatorError("unresolved must be a boolean")
        payload = self._load(session_id)
        if payload["status"] == "APPROVED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Approved discovery is immutable; start a new Ideator session for a new scope.")
        record = next(
            (item for item in payload["questions"] if item.get("topic") == topic and item.get("question", "").casefold() == question.casefold()),
            None,
        )
        if record is None:
            record = {
                "question_id": f"Q-{len(payload['questions']) + 1:03d}",
                "topic": topic,
                "question": question,
                "order": len(payload["questions"]) + 1,
                "source": source.strip()[:80] or "user",
                "asked_at": _now(),
                "answer": "",
                "answered_at": None,
                "unresolved": False,
                "actor_refs": [],
                "flow_refs": [],
            }
            payload["questions"].append(record)
        record["answer"] = answer
        record["answered_at"] = _now()
        record["unresolved"] = unresolved
        record["source"] = source.strip()[:80] or "user"
        record["actor_refs"] = _string_list(actor_refs, "actor_refs") if actor_refs is not None else []
        record["flow_refs"] = _string_list(flow_refs, "flow_refs") if flow_refs is not None else []
        payload["status"] = "DISCOVERY"
        payload["proposal"] = None
        payload["discovery_summary"] = None
        payload["generated"] = None
        payload["handoff"] = None
        payload["jev_checkpoint"] = None
        payload.pop("jev", None)
        payload["review_id"] = None
        payload.pop("approval_id", None)
        payload.pop("approved_at", None)
        payload.pop("approved_by", None)
        payload.pop("sqlite_projection", None)
        self._save(payload)
        return self.status(session_id)

    def _missing_topics(self, payload: dict[str, Any]) -> list[str]:
        covered = {item["topic"] for item in payload["questions"] if item.get("answered_at") and (item.get("answer") or item.get("unresolved"))}
        return [topic for topic in TOPICS if topic not in covered]

    def parity(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        _, notes_path = self._paths(session_id)
        expected = self._render_notes(payload)
        actual = notes_path.read_text(encoding="utf-8")
        return {
            "ok": expected == actual,
            "session_id": session_id,
            "questions": len(payload["questions"]),
            "json_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode()).hexdigest(),
            "notes_sha256": hashlib.sha256(actual.encode()).hexdigest(),
            "finding": None if expected == actual else "INQUIRY_MARKDOWN_JSON_MISMATCH",
        }

    def status(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        parity = self.parity(session_id)
        return {
            "session_id": session_id,
            "status": payload["status"],
            "objective": payload["objective"],
            "questions_recorded": len(payload["questions"]),
            "topics_missing": self._missing_topics(payload),
            "unresolved_questions": [item["question_id"] for item in payload["questions"] if item.get("unresolved")],
            "parity": parity,
            "document": payload.get("generated"),
            "review_id": payload.get("review_id"),
            "jev_checkpoint": payload.get("jev_checkpoint"),
            "technical_designer": self.gate(session_id),
        }

    def _runner(self) -> Any:
        if self.runner is not None:
            return self.runner
        config_dir = os.environ.get("OPENCODE_CONFIG_DIR")
        env = {
            key: os.environ[key]
            for key in ("OPENCODE_CONFIG_DIR", "OPENCODE_PROVIDER_CREDENTIAL", "OPENCODE_AUTH_FILE")
            if os.environ.get(key)
        }
        self.runner = OpenCodeRunner(
            executable=os.environ.get("DRIDH_OPENCODE_BIN", "opencode"),
            workspace_root=self.project_root,
            config_dir=config_dir,
            timeout=float(os.environ.get("DRIDH_IDEATOR_WORKER_TIMEOUT", "600")),
            env=env,
        )
        return self.runner

    def _worker(
        self, agent: str, prompt: str, *, model: str | None = None,
        validate: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Run a worker until its reply passes ``validate`` (at most three repairs, plan rule 9)."""

        def run(call_prompt: str, **kwargs: Any) -> Any:
            return self._runner().run_sync(
                agent=agent, prompt=call_prompt, model=model, title=f"Dridh Ideator: {agent}", **kwargs,
            )

        outcome = call_with_repair(run, prompt, label=_WORKER_LABELS.get(agent, agent), validate=validate)
        if outcome.attempts:
            self._repair_log.append({"agent": agent, "recorded_at": _now(), "attempts": [a.to_dict() for a in outcome.attempts]})
        return outcome.value

    @staticmethod
    def _check_inquirer(inquirer: dict[str, Any]) -> None:
        required = {"summary", "key_facts", "gaps", "unresolved"}
        if not required.issubset(inquirer) or not isinstance(inquirer["summary"], str):
            raise DridhError(
                ErrorCode.WORKER_FAILED,
                "Ideator Inquirer output does not match its JSON contract.",
                details=("required keys: summary (string), key_facts, gaps, unresolved (string arrays)",),
            )
        for field in ("key_facts", "gaps", "unresolved"):
            _string_list(inquirer[field], f"inquirer.{field}")

    @staticmethod
    def _check_handoff(handoff: dict[str, Any]) -> None:
        if not isinstance(handoff.get("summary"), str) or not isinstance(handoff.get("concerns"), list):
            raise DridhError(
                ErrorCode.WORKER_FAILED,
                "Ideator Handoff output does not match its JSON contract.",
                details=("required keys: summary (string) and concerns (array of strings)",),
            )
        _string_list(handoff["concerns"], "concerns")

    def generate(
        self,
        session_id: str,
        *,
        proposal: dict[str, Any] | None = None,
        run_workers: bool = True,
        model: str | None = None,
    ) -> dict[str, Any]:
        self._repair_log = []
        payload = self._load(session_id)
        if payload.get("status") != "DISCOVERY":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Only an active discovery session can be generated; review sessions must be corrected and regenerated after a new answer.")
        if not self.parity(session_id)["ok"]:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Inquiry Markdown and JSON are out of sync.", details=("INQUIRY_MARKDOWN_JSON_MISMATCH",))
        self._refuse_other_sessions_document(session_id)
        missing = self._missing_topics(payload)
        if missing:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "Discovery is incomplete; answer or explicitly mark every required topic unresolved before generation.",
                details=tuple(missing),
                recovery="Resume the session, answer the missing topics, then generate again.",
            )
        if proposal is None:
            if not run_workers:
                raise IdeatorError("A structured proposal is required when run_workers=False")
            inquirer = self._worker(
                "dridh/workers/ideator-inquirer",
                "Analyse this complete discovery log. Return JSON only with keys: summary (string), key_facts (string array), "
                "gaps (string array), and unresolved (string array of question IDs). Do not invent facts.\n"
                + json.dumps(payload, ensure_ascii=False),
                model=model,
                validate=self._check_inquirer,
            )
            payload["discovery_summary"] = inquirer
            proposal = self._worker(
                "dridh/workers/ideator-doc-generator",
                "Create a complete structured Use Case proposal from this discovery log and analysis. Return JSON only. "
                "Required shape: {title, problem_statement, scope, out_of_scope, assumptions, constraints, priority, "
                "actors:[{name,type,status,description}], use_cases:[{title,primary_actor,trigger,category,objective,priority,"
                "flows:[{type,subject,actor,action,result,priority,origin_step}]}]}. "
                "Actor references must use actor names. Flow types: SUCCESS, ALTERNATIVE, EXCEPTION, BOUNDARY, BUSINESS_RULE. "
                "origin_step for a branch is the 1-based position of the success step it branches from. "
                "Every use case needs at least one SUCCESS step. Do not invent requirements; record uncertainty as assumptions.\n"
                "DISCOVERY:\n" + json.dumps(payload, ensure_ascii=False) + "\nANALYSIS:\n" + json.dumps(inquirer, ensure_ascii=False),
                model=model,
                validate=self._validate_proposal,
            )
        self._validate_proposal(proposal)
        canonical_path = self.project_root / "001-USE-CASES.md"
        if canonical_path.exists():
            if not canonical_path.is_file() or canonical_path.is_symlink():
                raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "001-USE-CASES.md is not a regular file; it was not overwritten.")
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                documents = DocumentRepository(store.connection, self.workspace.workspace_id)
                managed = documents.document_metadata("001-USE-CASES.md")
                current_revision = documents.current_revision("001-USE-CASES.md")
                current_hash = content_hash(canonical_path.read_text(encoding="utf-8"))
                if managed is None or current_revision is None:
                    raise DridhError(
                        ErrorCode.CONFIGURATION_CONFLICT,
                        "001-USE-CASES.md already exists and is not a Dridh-managed revision; it was not overwritten.",
                        recovery="Import or rename the existing document before generating a new canonical Use Case document.",
                    )
                if current_hash != current_revision.content_sha256:
                    raise DridhError(
                        ErrorCode.CONFIGURATION_CONFLICT,
                        "001-USE-CASES.md has unrecorded edits since its last Dridh revision; it was not overwritten.",
                        recovery="Review the edit and record/import a new validated revision before regenerating.",
                    )
        self._refuse_other_sessions_document(session_id)  # checked again: workers may have run for minutes
        document, allocated = self._render_document(proposal, questions=payload["questions"])
        validation = validate_use_cases(document, file="001-USE-CASES.md")
        if not validation.ok:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "The generated Use Case document does not satisfy Standards 2.1.0.",
                details=tuple(validation.errors[:20]),
                recovery="Correct the discovery answers and regenerate; generated Markdown is not accepted without deterministic validation.",
            )
        projection_model = validated_model(document, "001-USE-CASES.md")
        handoff: dict[str, Any] = {"summary": "Deterministic validation passed.", "items": list(CHECKLIST)}
        if run_workers:
            handoff_result = self._worker(
                "dridh/workers/ideator-handoff",
                "Review this generated Use Case document against the supplied checklist. Return JSON only with "
                "keys summary (string) and concerns (array of strings). Do not approve the document. "
                "Do not claim validation that you did not perform.\nCHECKLIST:\n"
                + json.dumps(list(CHECKLIST))
                + "\nDISCOVERY:\n"
                + json.dumps(
                    {
                        "objective": payload["objective"],
                        "existing_context": payload["existing_context"],
                        "questions": payload["questions"],
                        "discovery_summary": payload.get("discovery_summary"),
                        "proposal": proposal,
                    },
                    ensure_ascii=False,
                )
                + "\nDOCUMENT:\n" + document,
                model=model,
                validate=self._check_handoff,
            )
            handoff = {"summary": handoff_result["summary"], "concerns": _string_list(handoff_result["concerns"], "concerns"), "items": list(CHECKLIST)}
        jev = self._jev_checkpoint(session_id, payload, proposal, document)
        previous_document = canonical_path.read_bytes() if canonical_path.exists() else None
        _atomic_write(canonical_path, document)
        payload["proposal"] = proposal
        payload["handoff"] = handoff
        payload["jev_checkpoint"] = jev
        payload.pop("jev", None)
        if self._repair_log:
            payload["repair_history"] = list(self._repair_log)
        payload["generated"] = {
            "path": "001-USE-CASES.md",
            "sha256": content_hash(document),
            "actor_ids": allocated["actor_ids"],
            "use_case_ids": allocated["use_case_ids"],
            "revision_id": None,
            "review_id": None,
            "document_content": document,
        }
        try:
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                with store.transaction() as connection:
                    documents = DocumentRepository(connection, self.workspace.workspace_id)
                    revision, _ = documents.record_revision(
                        relative_path="001-USE-CASES.md",
                        document_type="use_cases",
                        content=document,
                        standards_version=STANDARDS_VERSION,
                        source_path=str(canonical_path),
                    )
                    validation_id = documents.record_validation(
                        revision_id=revision.id,
                        validator="ideator.use_cases",
                        validator_version=f"dridh-{STANDARDS_VERSION}",
                        result=validation,
                    )
                    metadata = documents.document_metadata("001-USE-CASES.md")
                    if metadata is None:
                        raise DridhError(ErrorCode.INTERNAL_ERROR, "The generated Use Case document was not registered.")
                    projection_counts = UseCaseRepository(connection, self.workspace.workspace_id).replace_projection(
                        document_id=str(metadata["id"]),
                        revision_id=revision.id,
                        model=projection_model,
                    )
                    reviews = ReviewRepository(connection, self.workspace.workspace_id)
                    review_id = reviews.create_review(
                        document_id=str(metadata["id"]),
                        revision_id=revision.id,
                        reviewer="Pending human reviewer",
                        actor="agent:dridh/coordinator",
                    )
                    for item in CHECKLIST:
                        reviews.set_checklist(review_id, item, False, actor="agent:dridh/coordinator")
                    connection.execute(
                        "UPDATE document SET lifecycle_state='READY_FOR_REVIEW', updated_at=? WHERE id=?",
                        (_now(), str(metadata["id"])),
                    )
        except BaseException:
            if previous_document is None:
                canonical_path.unlink(missing_ok=True)
            else:
                _atomic_write(canonical_path, previous_document.decode("utf-8"))
            raise
        payload["status"] = "IN_REVIEW"
        payload["review_id"] = review_id
        payload["generated"]["revision_id"] = revision.id
        payload["generated"]["review_id"] = review_id
        payload["generated"]["validation_id"] = validation_id
        payload["sqlite_projection"] = projection_counts
        checklist_path = self._session_dir(session_id) / "REVIEW-CHECKLIST.md"
        checklist_lines = [
            "# Use Case Review Checklist",
            "",
            "- **Document:** 001-USE-CASES.md",
            f"- **SHA-256:** {payload['generated']['sha256']}",
            f"- **Review ID:** {review_id}",
            "",
        ]
        checklist_lines.extend(f"- [ ] {item}" for item in CHECKLIST)
        concerns = handoff.get("concerns", [])
        if concerns:
            checklist_lines.extend(["", "## Handoff concerns", ""])
            checklist_lines.extend(f"- {item}" for item in concerns)
        checklist_lines.extend(["", "## Human decision", "", f"Run `dridh ideator approve {session_id} --reviewer <name>` in a terminal after review.", ""])
        _atomic_write(checklist_path, "\n".join(checklist_lines))
        payload["handoff"]["checklist_path"] = str(checklist_path.relative_to(self.project_root))
        self._save(payload)
        return {
            "ok": True,
            "session_id": session_id,
            "status": "IN_REVIEW",
            "document": "001-USE-CASES.md",
            "sha256": payload["generated"]["sha256"],
            "revision_id": revision.id,
            "review_id": review_id,
            "validation": {"ok": True, "errors": 0, "findings": len(validation.findings)},
            "handoff": handoff,
            "jev_checkpoint": jev,
            "technical_designer": self.gate(session_id),
        }

    def _validate_proposal(self, proposal: dict[str, Any]) -> None:
        required = {"title", "problem_statement", "scope", "out_of_scope", "assumptions", "constraints", "priority", "actors", "use_cases"}
        if not isinstance(proposal, dict):
            raise IdeatorError("proposal must be one JSON object")
        missing = required - set(proposal)
        if missing:
            raise IdeatorError(f"proposal is missing required fields: {', '.join(sorted(missing))}")
        for field in ("title", "problem_statement", "scope"):
            if not isinstance(proposal[field], str) or not proposal[field].strip():
                raise IdeatorError(f"proposal.{field} must be a non-empty string")
        if not isinstance(proposal["priority"], str) or proposal["priority"] not in PRIORITIES:
            raise IdeatorError("proposal.priority must be HIGH, MEDIUM or LOW")
        proposal["out_of_scope"] = _string_list(proposal["out_of_scope"], "out_of_scope")
        proposal["assumptions"] = _string_list(proposal["assumptions"], "assumptions")
        proposal["constraints"] = _string_list(proposal["constraints"], "constraints")
        actors = proposal["actors"]
        cases = proposal["use_cases"]
        if not isinstance(actors, list) or not actors or not isinstance(cases, list) or not cases:
            raise IdeatorError("proposal must contain at least one actor and one use case")
        names: set[str] = set()
        for actor in actors:
            if not isinstance(actor, dict) or not isinstance(actor.get("name"), str) or not actor["name"].strip():
                raise IdeatorError("each actor needs a non-empty name")
            if (
                not isinstance(actor.get("type"), str) or actor["type"] not in ACTOR_TYPES
                or not isinstance(actor.get("status"), str) or actor["status"] not in ACTOR_STATUSES
            ):
                raise IdeatorError(f"actor {actor['name']!r} has an invalid type or status")
            if actor["name"].casefold() in names:
                raise IdeatorError(f"duplicate actor name: {actor['name']}")
            names.add(actor["name"].casefold())
            actor["description"] = str(actor.get("description", "")).strip() or actor["name"]
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("title"), str) or not case["title"].strip():
                raise IdeatorError("each use case needs a title")
            primary_actor = case.get("primary_actor")
            if not isinstance(primary_actor, str) or primary_actor.casefold() not in names:
                raise IdeatorError(f"use case {case['title']!r} names an unknown primary_actor")
            if not isinstance(case.get("trigger"), str) or not case["trigger"].strip():
                raise IdeatorError(f"use case {case['title']!r} needs a trigger")
            case_priority = case.get("priority", proposal["priority"])
            if not isinstance(case_priority, str) or case_priority not in PRIORITIES:
                raise IdeatorError(f"use case {case['title']!r} has an invalid priority")
            flows = case.get("flows")
            if not isinstance(flows, list) or not any(isinstance(flow, dict) and flow.get("type") == "SUCCESS" for flow in flows):
                raise IdeatorError(f"use case {case['title']!r} needs at least one SUCCESS flow")
            for flow in flows:
                if not isinstance(flow, dict) or not isinstance(flow.get("type"), str) or flow["type"] not in FLOW_TYPES:
                    raise IdeatorError(f"use case {case['title']!r} contains an unsupported flow type")
                flow_actor = flow.get("actor")
                if not isinstance(flow_actor, str) or flow_actor.casefold() not in names:
                    raise IdeatorError(f"flow in {case['title']!r} refers to an unknown actor")
                for field in ("subject", "action", "result"):
                    if not isinstance(flow.get(field), str) or not flow[field].strip():
                        raise IdeatorError(f"flow in {case['title']!r} requires {field}")
                flow_priority = flow.get("priority", case.get("priority", proposal["priority"]))
                if not isinstance(flow_priority, str) or flow_priority not in PRIORITIES:
                    raise IdeatorError(f"flow in {case['title']!r} has an invalid priority")

    def _render_document(self, proposal: dict[str, Any], *, questions: list[dict[str, Any]] | None = None) -> tuple[str, dict[str, Any]]:
        self._validate_proposal(proposal)
        source_document = "001-USE-CASES.md"
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                allocator = CoordinatorIdAllocator(connection)
                existing_path = self.project_root / source_document
                if existing_path.is_file():
                    allocator.reserve_document_ids(
                        existing_path.read_text(encoding="utf-8"),
                        source_document=source_document,
                        document_type="use_cases",
                    )
                actor_ids: dict[str, str] = {}
                for actor in proposal["actors"]:
                    actor_ids[actor["name"].casefold()] = allocator.allocate("actor", source_document=source_document)
                use_case_ids: list[str] = []
                cases: list[dict[str, Any]] = []
                for case in proposal["use_cases"]:
                    uc_id = allocator.allocate("use_case", source_document=source_document)
                    use_case_ids.append(uc_id)
                    success_ids: list[str] = []
                    flows_out: list[dict[str, str]] = []
                    ordered_types = ("SUCCESS", "ALTERNATIVE", "EXCEPTION", "BOUNDARY", "BUSINESS_RULE")
                    for flow_type in ordered_types:
                        for flow in (item for item in case["flows"] if item["type"] == flow_type):
                            family = FLOW_TYPES[flow_type]
                            flow_id = allocator.allocate(family, parent=uc_id, source_document=source_document)
                            if flow_type == "SUCCESS":
                                success_ids.append(flow_id)
                            flows_out.append({
                                "flow_id": flow_id,
                                "type": flow_type,
                                "origin": "—" if flow_type == "SUCCESS" else "",
                                "subject": flow["subject"],
                                "actor_id": actor_ids[flow["actor"].casefold()],
                                "action": flow["action"],
                                "result": flow["result"],
                                "priority": flow.get("priority", case.get("priority", proposal["priority"])),
                                "origin_step": str(flow.get("origin_step") or 1),
                            })
                    for flow in flows_out:
                        if flow["type"] != "SUCCESS":
                            try:
                                origin_index = int(flow["origin_step"]) - 1
                                flow["origin"] = success_ids[origin_index]
                            except (ValueError, IndexError):
                                raise IdeatorError(f"Branch {flow['flow_id']} origin_step must reference a success-flow position in {uc_id}") from None
                    cases.append({**case, "use_case_id": uc_id, "primary_actor_id": actor_ids[case["primary_actor"].casefold()], "flows_out": flows_out})
        sections = [
            f"# {_cell(proposal['title'])} — Use Cases",
            "",
            f"> **Standards Version:** {STANDARDS_VERSION}  ",
            "> **Document Status:** DRAFT  ",
            f"> **Last Updated:** {datetime.now(UTC).date().isoformat()}  ",
            f"> **Feature:** {_cell(proposal['title'])}",
            "",
            "## Problem Statement and Objectives",
            "",
            _cell(proposal["problem_statement"]),
            "",
            "## Scope",
            "",
            _cell(proposal["scope"]),
            "",
            "## Actors Registry",
            "",
            "| Actor ID | Actor Name | Type | Status | Description |",
            "|---|---|---|---|---|",
        ]
        for actor in proposal["actors"]:
            actor_id = actor_ids[actor["name"].casefold()]
            sections.append(f"| {actor_id} | {_cell(actor['name'])} | {actor['type']} | {actor['status']} | {_cell(actor['description'])} |")
        sections += [
            "",
            "## Use Case Summary Matrix",
            "",
            "| ID | Use Case | Primary Actor | Trigger | Category |",
            "|---|---|---|---|---|",
        ]
        for case in cases:
            sections.append(f"| {case['use_case_id']} | {_cell(case['title'])} | {case['primary_actor_id']} | {_cell(case['trigger'])} | {_cell(case.get('category', 'Core'))} |")
        sections += ["", "## Detailed Use Cases", ""]
        for case in cases:
            sections += [
                f"### {case['use_case_id']}: {_cell(case['title'])}",
                "",
                f"- **Objective:** {_cell(case.get('objective') or case['title'])}",
                f"- **Trigger:** {_cell(case['trigger'])}",
                f"- **Priority:** {case.get('priority', proposal['priority'])}",
                "",
                "| Flow ID | Type | Origin Step | Subject | Actor ID | Action | Result | Priority |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for flow in case["flows_out"]:
                label = {"SUCCESS": "SUCCESS", "ALTERNATIVE": "ALTERNATIVE", "EXCEPTION": "EXCEPTION", "BOUNDARY": "BOUNDARY", "BUSINESS_RULE": "BUSINESS_RULE"}[flow["type"]]
                sections.append(f"| {flow['flow_id']} | {label} | {flow['origin']} | {_cell(flow['subject'])} | {flow['actor_id']} | {_cell(flow['action'])} | {_cell(flow['result'])} | {flow['priority']} |")
            sections.append("")
        sections += ["## Assumptions and Constraints", ""]
        for item in proposal["assumptions"]:
            sections.append(f"- **Assumption:** {_cell(item)}")
        for item in proposal["constraints"]:
            sections.append(f"- **Constraint:** {_cell(item)}")
        for item in proposal["out_of_scope"]:
            sections.append(f"- **Out of scope:** {_cell(item)}")
        if not proposal["assumptions"] and not proposal["constraints"] and not proposal["out_of_scope"]:
            sections.append("- N/A")
        if isinstance(proposal.get("phase2_boundary"), list) and proposal["phase2_boundary"]:
            sections += ["", "## Phase 2 Boundary (Where Applicable)", "", "| Boundary | Description | Status |", "|---|---|---|"]
            for item in proposal["phase2_boundary"]:
                sections.append(f"| {_cell(item.get('name', 'Boundary'))} | {_cell(item.get('description', ''))} | {_cell(item.get('status', 'OPEN'))} |")
        sections += [
            "",
            "## Open Questions / Q&A Log",
            "",
            "| # | Question | Asked In | Answer | Source | Impact |",
            "|---|---|---|---|---|---|",
        ]
        for item in questions or []:
            impact = ", ".join(item.get("actor_refs", []) + item.get("flow_refs", [])) or item.get("topic", "")
            answer = item.get("answer") or "UNRESOLVED"
            state = "UNRESOLVED" if item.get("unresolved") else "CAPTURED"
            sections.append(
                f"| {item['order']} | {_cell(item['question'])} | Ideator | {_cell(answer)} ({state}) | {_cell(item['source'])} | {_cell(impact)} |"
            )
        return "\n".join(sections) + "\n", {
            "actor_ids": actor_ids,
            "use_case_ids": use_case_ids,
        }

    def _render_notes(self, payload: dict[str, Any]) -> str:
        proposal = payload.get("proposal")
        if not isinstance(proposal, dict):
            proposal = {
                "title": payload.get("objective", "New Feature"),
                "problem_statement": payload.get("objective", "Discovery in progress"),
                "scope": "Scope is being discovered.",
                "out_of_scope": [], "assumptions": [], "constraints": [], "priority": "MEDIUM",
                "actors": [], "use_cases": [],
            }
        title = _cell(proposal.get("title", payload.get("objective", "New Feature")))
        lines = [
            f"# INQUIRY NOTES — {title}",
            "",
            "> **Status:** STAGING — NOT THE CANONICAL APPROVED USE-CASE DOCUMENT  ",
            f"> **Session ID:** {payload['session_id']}  ",
            f"> **Standards Version:** {STANDARDS_VERSION}",
            "",
        ]
        if payload.get("existing_context"):
            lines += ["## Existing Workspace Context", "", _cell(payload["existing_context"]), ""]
        if proposal.get("actors") and payload.get("generated"):
            doc = payload.get("generated", {}).get("document_content")
            if not isinstance(doc, str) or not doc.startswith("# "):
                raise DridhError(ErrorCode.STATE_CORRUPT, "Generated discovery is missing its canonical document snapshot.")
            body = doc[doc.find("## Problem Statement and Objectives"):]
            lines.append(body)
            return "\n".join(lines).rstrip() + "\n"
        else:
            lines += [
                "## Problem Statement and Objectives", "",
                _cell(proposal.get("problem_statement", payload.get("objective", ""))), "",
                "## Actors Registry", "",
                "| Actor ID | Actor Name | Type | Status | Description |",
                "|---|---|---|---|---|",
                "| — | — | — | — | — |", "",
                "## Use Case Summary Matrix", "",
                "| ID | Use Case | Primary Actor | Trigger | Category |",
                "|---|---|---|---|---|",
                "| — | — | — | — | — |", "",
                "## Detailed Use Cases", "",
            ]
        if payload.get("questions"):
            lines += ["", "## Open Questions / Q&A Log", "", "| # | Question | Asked In | Answer | Source | Impact |", "|---|---|---|---|---|---|"]
            for item in payload["questions"]:
                impact = ", ".join(item.get("actor_refs", []) + item.get("flow_refs", [])) or item.get("topic", "")
                status = "UNRESOLVED" if item.get("unresolved") else "CAPTURED"
                answer = item.get("answer") or "UNRESOLVED"
                lines.append(f"| {item['order']} | {_cell(item['question'])} | Ideator | {_cell(answer)} ({status}) | {_cell(item['source'])} | {_cell(impact)} |")
        else:
            lines += ["", "## Open Questions / Q&A Log", "", "| # | Question | Asked In | Answer | Source | Impact |", "|---|---|---|---|---|---|", "| — | — | — | — | — | — |"]
        return "\n".join(lines).rstrip() + "\n"

    def _jev_checkpoint(self, session_id: str, payload: dict[str, Any], proposal: dict[str, Any], document: str) -> dict[str, Any]:
        """The shared stage checkpoint (jev.checkpoints): recorded in SQLite, shown to the reviewer, advisory, never blocking.

        Only a bounded summary is sent: never the transcript or the document itself.
        """
        from dridh.jev.checkpoints import run_checkpoint
        from dridh.jev.evaluation import JevEvaluationService

        answered = sum(1 for item in payload["questions"] if item.get("answer"))
        unresolved = sum(1 for item in payload["questions"] if item.get("unresolved"))
        use_cases = proposal.get("use_cases") or []
        flows = sum(len(uc.get("flows") or []) for uc in use_cases if isinstance(uc, dict))
        summary = (
            f"Objective: {str(payload['objective'])[:600]}\n"
            f"Discovery topics answered: {answered}; explicitly unresolved: {unresolved}.\n"
            f"Use Case document validated: actors {len(proposal.get('actors') or [])}; use cases {len(use_cases)}; flows {flows}.\n"
            f"Use Case document SHA-256: {content_hash(document)}"
        )
        service = JevEvaluationService(self.workspace, client=self.jev_client) if self.jev_client is not None else None
        return run_checkpoint(self.workspace, stage="ideator", subject_id=session_id, agent_id="dridh/ideator", summary=summary,
                              service=service, metadata={"source_revision_sha": content_hash(document)})

    def _refuse_other_sessions_document(self, session_id: str) -> None:
        """Never let one session overwrite the canonical document another session put under review or got approved.

        Found in a real-model run: a Coordinator generated a throwaway session over a document that was awaiting approval.
        """
        canonical = self.project_root / "001-USE-CASES.md"
        if not canonical.is_file():
            return
        current = content_hash(canonical.read_text(encoding="utf-8"))
        root = self.state_root / "artifacts" / "ideator"
        for folder in sorted(root.glob("inquiry-*")) if root.is_dir() else ():
            if folder.name == session_id:
                continue
            try:
                other = self._load(folder.name)
            except DridhError:
                continue
            if other.get("status") in ("IN_REVIEW", "APPROVED") and (other.get("generated") or {}).get("sha256") == current:
                raise DridhError(
                    ErrorCode.CONFIGURATION_CONFLICT,
                    f"001-USE-CASES.md belongs to Ideator session {folder.name} ({other['status']}); this session may not overwrite it.",
                    details=(f"current document SHA-256: {current}",),
                    recovery=f"Continue session {folder.name} instead (answer and regenerate it). Only a human may replace its document, "
                             "by moving 001-USE-CASES.md aside first.",
                )

    def approval_challenge(self, session_id: str) -> dict[str, Any]:
        payload = self._load(session_id)
        if payload["status"] != "IN_REVIEW" or not payload.get("review_id") or not payload.get("generated"):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "This Ideator session is not awaiting human review.")
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
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "The confirmation did not match. Nothing was changed.")
        if not reviewer.strip():
            raise IdeatorError("reviewer is required")
        canonical = self.project_root / "001-USE-CASES.md"
        if not canonical.is_file():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "The canonical Use Case document is missing.")
        source_text = canonical.read_text(encoding="utf-8")
        if content_hash(source_text) != payload["generated"]["sha256"]:
            raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The Use Case document changed after review; approval is refused.", recovery="Regenerate the review against the current document revision.")
        validated_model(source_text, "001-USE-CASES.md")
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            with store.transaction() as connection:
                documents = DocumentRepository(connection, self.workspace.workspace_id)
                revision = documents.current_revision("001-USE-CASES.md")
                metadata = documents.document_metadata("001-USE-CASES.md")
                if revision is None or metadata is None or revision.id != challenge["revision_id"]:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "Approval revision is stale; no approval was recorded.")
                validation = documents.latest_validation(revision.id, "ideator.use_cases")
                if validation is None or validation.status != "PASS":
                    raise DridhError(ErrorCode.VALIDATION_FAILED, "The current revision has no passing deterministic Use Case validation.")
                projection = UseCaseRepository(connection, self.workspace.workspace_id)
                marker = projection.import_marker(str(metadata["id"]))
                if marker is None or marker["source_revision_id"] != revision.id:
                    raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The current revision has not been reconciled into SQLite; approval is refused.")
                projection_findings = projection.projection_findings(
                    relative_path="001-USE-CASES.md",
                    document_id=str(metadata["id"]),
                    current_revision_id=revision.id,
                    current_content=documents.stored_content(revision.id),
                )
                if projection_findings:
                    raise DridhError(ErrorCode.VALIDATION_FAILED, "SQLite Use Case reconciliation is not clean.", details=tuple(f.message for f in projection_findings[:10]))
                stored_model = projection.stored_model(str(metadata["id"]))
                counts = {"actors": len(stored_model.actors), "use_cases": len(stored_model.use_cases), "flows": len(stored_model.flows)}
                reviews = ReviewRepository(connection, self.workspace.workspace_id)
                approval_id = reviews.decide(
                    payload["review_id"],
                    decision="APPROVED",
                    reviewer=reviewer,
                    reason="Human approved the revision-bound Use Case document.",
                    actor=f"human:{reviewer}",
                    revision_id=revision.id,
                )
        payload["status"] = "APPROVED"
        payload["approved_at"] = _now()
        payload["approved_by"] = reviewer
        payload["approval_id"] = approval_id
        payload["sqlite_projection"] = counts
        self._save(payload)
        gate = self.gate(session_id)
        if not gate["unlocked"]:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Approval was recorded but the Technical Designer gate did not reconcile.", details=(gate["reason"],))
        return {
            "ok": True,
            "session_id": session_id,
            "status": "APPROVED",
            "approval_id": approval_id,
            "revision_id": challenge["revision_id"],
            "sqlite_projection": counts,
            "technical_designer": gate,
        }

    def gate(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is not None:
            session = self._load(session_id)
            if session.get("status") != "APPROVED" or not isinstance(session.get("generated"), dict):
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "This inquiry session has not received human approval."}
        else:
            session = None
        canonical = self.project_root / "001-USE-CASES.md"
        if not canonical.is_file():
            return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "001-USE-CASES.md does not exist."}
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            documents = DocumentRepository(store.connection, self.workspace.workspace_id)
            metadata = documents.document_metadata("001-USE-CASES.md")
            revision = documents.current_revision("001-USE-CASES.md")
            if metadata is None or revision is None:
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "The canonical Use Case document is not reconciled to SQLite."}
            if session is not None and (
                not isinstance(session.get("review_id"), str)
                or not isinstance(session.get("approval_id"), str)
                or session["generated"].get("review_id") != session["review_id"]
            ):
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "The inquiry is not bound to a valid human review and approval record."}
            query = (
                "SELECT ha.id, ha.review_id, ha.reviewer, ha.created_at "
                "FROM human_approval ha JOIN review r ON r.id=ha.review_id "
                "WHERE r.document_id=? AND r.status='APPROVED' AND ha.revision_id=? AND ha.decision='APPROVED'"
            )
            params: list[str] = [str(metadata["id"]), revision.id]
            if session is not None:
                query += " AND ha.review_id=? AND ha.id=?"
                params.extend([session["review_id"], session["approval_id"]])
            query += " ORDER BY ha.created_at DESC LIMIT 1"
            approval = store.connection.execute(query, params).fetchone()
            if approval is None:
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "The current Use Case revision has no human approval."}
            if session is not None and (
                session["generated"].get("revision_id") != revision.id
                or session["generated"].get("sha256") != revision.content_sha256
            ):
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "The approved SQLite revision does not belong to this inquiry session."}
            disk_content = canonical.read_text(encoding="utf-8")
            if content_hash(disk_content) != revision.content_sha256:
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "The on-disk Use Case document differs from the approved SQLite revision."}
            marker = UseCaseRepository(store.connection, self.workspace.workspace_id).import_marker(str(metadata["id"]))
            if marker is None or marker["source_revision_id"] != revision.id:
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": "The approved Use Case revision has not been projected into SQLite."}
            findings = UseCaseRepository(store.connection, self.workspace.workspace_id).projection_findings(
                relative_path="001-USE-CASES.md",
                document_id=str(metadata["id"]),
                current_revision_id=revision.id,
                current_content=documents.stored_content(revision.id),
            )
            if findings:
                return {"unlocked": False, "stage": "dridh/technical-designer", "reason": findings[0].message}
            return {
                "unlocked": True,
                "stage": "dridh/technical-designer",
                "source_revision_id": revision.id,
                "source_sha256": revision.content_sha256,
                "approved_by": approval["reviewer"],
                "approved_at": approval["created_at"],
            }
