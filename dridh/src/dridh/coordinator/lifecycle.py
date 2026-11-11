"""The feature lifecycle: one answer to "where is this feature, and what may happen next?".

Stages (single forward steps, ``CODEBASE_INDEXING`` only for existing codebases)::

    INITIALISED -> [CODEBASE_INDEXING] -> IDEATION -> USE_CASE_REVIEW -> TECHNICAL_DISCOVERY
        -> TDD_REVIEW -> PLANNING -> PLAN_VALIDATION -> EXECUTION -> DELIVERY_REVIEW -> COMPLETED

Design rules
------------
* The lifecycle never re-implements a stage's rules. A stage is *left* only when that stage's own
  evidence says so, and the evidence comes from the existing services: the Ideator and Designer
  ``gate()`` (revision-bound human approval, on-disk hash, SQLite projection), the Planner ``gate()``
  (validated, reconciled plan), the story table and the code index. Agents cannot declare a stage done.
* Moves are single steps, recorded with their evidence in an append-only history and the hash-chained
  audit log; writes use an optimistic revision check so a stale caller cannot overwrite newer state.
* Approvals are tied to document revisions. If a document changes after approval, the stage service's
  gate closes and ``sync`` moves the feature back to the matching review stage (``REOPEN``).
* Completing a feature is a human decision (``complete``); no agent-reachable path does it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.lifecycle import STAGES, LifecycleRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import WorkspaceInfo

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Where a feature goes back to when the human sends the stage's work back for changes, or its approval goes stale.
REOPEN_TARGET = {
    "USE_CASE_REVIEW": "IDEATION",
    "TDD_REVIEW": "TECHNICAL_DISCOVERY",
    "PLAN_VALIDATION": "PLANNING",
    "DELIVERY_REVIEW": "EXECUTION",
}

NEXT_ACTIONS: dict[str, tuple[str, ...]] = {
    "INITIALISED": ("coordinator.sync",),
    "CODEBASE_INDEXING": ("code_reacher.index (Phase 14)", "coordinator.sync"),
    "IDEATION": ("ideator.start", "ideator.answer", "ideator.generate", "coordinator.sync"),
    "USE_CASE_REVIEW": ("human: `dridh ideator approve` or reject", "coordinator.sync"),
    "TECHNICAL_DISCOVERY": ("designer.start", "designer.answer", "designer.generate", "coordinator.sync"),
    "TDD_REVIEW": ("human: `dridh designer approve` or reject", "coordinator.sync"),
    "PLANNING": ("planner.start", "planner.clarify", "planner.generate", "coordinator.sync"),
    "PLAN_VALIDATION": ("planner.gate", "coordinator.sync", "human: dridh planner approve"),
    "EXECUTION": ("Executor (Phase 15)", "coordinator.sync"),
    "DELIVERY_REVIEW": ("human: `dridh feature complete`",),
    "COMPLETED": (),
}


@dataclass
class Readiness:
    """Whether a feature may leave its current stage, and the evidence either way."""

    ready: bool
    blockers: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def successor(mode: str, stage: str) -> str | None:
    """The next stage on this feature's path, or None at the end."""
    index = STAGES.index(stage)
    for candidate in STAGES[index + 1:]:
        if candidate == "CODEBASE_INDEXING" and mode == "new":
            continue
        return candidate
    return None


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].strip("-")
    if not SLUG.fullmatch(slug):
        raise DridhError(ErrorCode.VALIDATION_FAILED, "A feature needs a name with letters or digits.", details=(name[:80],))
    return slug


class StageEvidence:
    """Reads the existing stage services and the SQLite projection; never writes."""

    def __init__(self, workspace: WorkspaceInfo) -> None:
        self.workspace = workspace

    # ---------------------------------------------------------------- services

    def _ideator(self) -> Any:
        from dridh.ideator.service import IdeatorService

        return IdeatorService(self.workspace)

    def _designer(self) -> Any:
        from dridh.designer.service import DesignerService

        return DesignerService(self.workspace)

    def _planner(self) -> Any:
        from dridh.planner.service import PlannerService

        return PlannerService(self.workspace)

    @staticmethod
    def _session_status(service: Any, session_id: str | None) -> str | None:
        if not session_id:
            return None
        try:
            return str(service.status(session_id).get("status"))
        except DridhError:
            return None

    @staticmethod
    def _gate(service: Any, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return {"unlocked": False, "reason": "No session is bound to this feature yet."}
        try:
            result = service.gate(session_id)
        except DridhError as exc:
            return {"unlocked": False, "reason": exc.message}
        return {"unlocked": bool(result.get("unlocked")), "reason": result.get("reason", "")}

    # ------------------------------------------------------------- discovery

    def discover_downstream(self, feature: dict[str, Any]) -> dict[str, str]:
        """Sessions that follow from the bound ones: designer from ideator, planner from designer."""
        found: dict[str, str] = {}
        root = self.workspace.root.resolve() / "artifacts"
        if feature.get("ideator_session_id") and not feature.get("designer_session_id"):
            found_id = self._latest(root / "designer", "designer-*", self._designer(), "ideator_session_id", feature["ideator_session_id"])
            if found_id:
                found["designer_session_id"] = found_id
        designer_id = feature.get("designer_session_id") or found.get("designer_session_id")
        if designer_id and not feature.get("planner_session_id"):
            found_id = self._latest(root / "planner", "planner-*", self._planner(), "designer_session_id", designer_id)
            if found_id:
                found["planner_session_id"] = found_id
        return found

    @staticmethod
    def _latest(folder: Any, pattern: str, service: Any, key: str, value: str) -> str | None:
        best: tuple[str, str] | None = None
        if not folder.is_dir():
            return None
        for candidate in sorted(folder.glob(pattern)):
            try:
                payload = service._load(candidate.name)
            except DridhError:
                continue
            if payload.get(key) == value:
                stamp = str(payload.get("updated_at") or payload.get("created_at") or "")
                if best is None or stamp >= best[0]:
                    best = (stamp, candidate.name)
        return best[1] if best else None

    # ------------------------------------------------------------ exit conditions

    def can_leave(self, feature: dict[str, Any], stage: str) -> Readiness:
        """May the feature leave ``stage``? Evidence comes from the stage's own service."""
        if stage == "INITIALISED":
            return Readiness(True, evidence={"event": "feature created"})
        if stage == "CODEBASE_INDEXING":
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                row = store.read().execute(
                    "SELECT id, status, source_sha FROM code_index_run WHERE workspace_id=? ORDER BY started_at DESC LIMIT 1",
                    (self.workspace.workspace_id,),
                ).fetchone()
            if row is None or row["status"] != "SUCCEEDED":
                return Readiness(False, ["No successful Code Reacher index exists yet for this codebase."],
                                 {"latest_index_status": row["status"] if row else None})
            return Readiness(True, evidence={"index_run": row["id"], "source_sha": row["source_sha"]})
        if stage == "IDEATION":
            session = feature.get("ideator_session_id")
            status = self._session_status(self._ideator(), session)
            if status in ("IN_REVIEW", "APPROVED"):
                return Readiness(True, evidence={"ideator_session_id": session, "status": status})
            return Readiness(False, [f"The Use Case document is not generated yet (Ideator status: {status or 'no session bound'})."],
                             {"ideator_session_id": session, "status": status})
        if stage == "USE_CASE_REVIEW":
            return self._from_gate(self._gate(self._ideator(), feature.get("ideator_session_id")), "Use Case approval")
        if stage == "TECHNICAL_DISCOVERY":
            session = feature.get("designer_session_id")
            status = self._session_status(self._designer(), session)
            if status in ("IN_REVIEW", "APPROVED"):
                return Readiness(True, evidence={"designer_session_id": session, "status": status})
            return Readiness(False, [f"The TDD is not generated yet (Designer status: {status or 'no session bound'})."],
                             {"designer_session_id": session, "status": status})
        if stage == "TDD_REVIEW":
            return self._from_gate(self._gate(self._designer(), feature.get("designer_session_id")), "TDD approval")
        if stage == "PLANNING":
            session = feature.get("planner_session_id")
            status = self._session_status(self._planner(), session)
            if status == "GENERATED":
                return Readiness(True, evidence={"planner_session_id": session, "status": status})
            return Readiness(False, [f"The plan is not generated yet (Planner status: {status or 'no session bound'})."],
                             {"planner_session_id": session, "status": status})
        if stage == "PLAN_VALIDATION":
            return self._from_gate(self._gate(self._planner(), feature.get("planner_session_id")), "plan validation")
        if stage == "EXECUTION":
            with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
                rows = store.read().execute(
                    "SELECT status, COUNT(*) AS n FROM story WHERE workspace_id=? GROUP BY status", (self.workspace.workspace_id,)
                ).fetchall()
            counts = {row["status"]: row["n"] for row in rows}
            total, done = sum(counts.values()), counts.get("COMPLETED", 0)
            if total == 0 or done != total:
                return Readiness(False, [f"{done} of {total} stories are COMPLETED; all must be before delivery review."], {"stories": counts})
            return Readiness(True, evidence={"stories": counts})
        if stage == "DELIVERY_REVIEW":
            return Readiness(False, ["Completion is an explicit human decision (`dridh feature complete`)."])
        return Readiness(False, ["The feature is complete."])

    @staticmethod
    def _from_gate(gate: dict[str, Any], label: str) -> Readiness:
        if gate["unlocked"]:
            return Readiness(True, evidence={"gate": "unlocked"})
        return Readiness(False, [gate.get("reason") or f"{label} is not satisfied."], {"gate": "locked", "reason": gate.get("reason", "")})

    # --------------------------------------------------------------- invariants

    def violated_invariant(self, feature: dict[str, Any]) -> tuple[str, str] | None:
        """(stage to go back to, reason) when an approval the current stage relies on is no longer valid."""
        stage = feature["stage"]
        index = STAGES.index(stage)
        checks = (
            ("TECHNICAL_DISCOVERY", self._ideator, "ideator_session_id", "USE_CASE_REVIEW", "Use Case approval"),
            ("PLANNING", self._designer, "designer_session_id", "TDD_REVIEW", "TDD approval"),
            ("EXECUTION", self._planner, "planner_session_id", "PLAN_VALIDATION", "plan validation"),
        )
        for first_stage, service_factory, column, back_to, label in checks:
            if index >= STAGES.index(first_stage) and feature.get(column):
                gate = self._gate(service_factory(), feature[column])
                if not gate["unlocked"]:
                    return back_to, f"The {label} is no longer valid: {gate.get('reason') or 'its gate is closed'}"
        # The human sent the stage's work back (rejected a review).
        sent_back = (
            ("USE_CASE_REVIEW", self._ideator, "ideator_session_id", ("DISCOVERY",)),
            ("TDD_REVIEW", self._designer, "designer_session_id", ("INQUIRY",)),
            ("PLAN_VALIDATION", self._planner, "planner_session_id", ("STALE", "INQUIRY", "INDEXING")),
        )
        for review_stage, service_factory, column, statuses in sent_back:
            if stage == review_stage and feature.get(column):
                status = self._session_status(service_factory(), feature[column])
                if status in statuses:
                    return REOPEN_TARGET[review_stage], f"The review sent the work back (session status: {status})."
        return None


class FeatureLifecycle:
    """Dispatcher for the feature lifecycle: create, status, advance, sync, reopen, block, complete."""

    MAX_STEPS = len(STAGES) + 4

    def __init__(self, workspace: WorkspaceInfo, *, evidence: StageEvidence | None = None) -> None:
        self.workspace = workspace
        self.evidence = evidence or StageEvidence(workspace)

    # ------------------------------------------------------------------ helpers

    def _store(self) -> StateStore:
        return StateStore(self.workspace.state_db, self.workspace.workspace_id)

    def _get(self, reference: str) -> dict[str, Any]:
        with self._store() as store:
            return LifecycleRepository(store.read(), self.workspace.workspace_id).get(reference)

    def _bind_discovered(self, feature: dict[str, Any], actor: str) -> dict[str, Any]:
        for column, session_id in self.evidence.discover_downstream(feature).items():
            with self._store() as store, store.transaction() as connection:
                feature = LifecycleRepository(connection, self.workspace.workspace_id).bind_session(feature, column, session_id, actor=actor)
        return feature

    # ------------------------------------------------------------------- public

    def create_feature(self, name: str, *, actor: str, mode: str | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A feature name is required.")
        slug = slugify(name)
        with self._store() as store, store.transaction() as connection:
            created = LifecycleRepository(connection, self.workspace.workspace_id).create(
                slug=slug, name=name.strip()[:200], mode=mode or self.workspace.mode, actor=actor
            )
        return created

    def list_features(self) -> list[dict[str, Any]]:
        with self._store() as store:
            return LifecycleRepository(store.read(), self.workspace.workspace_id).list()

    def bind_session(self, reference: str, column: str, session_id: str, *, actor: str) -> dict[str, Any]:
        feature = self._get(reference)
        with self._store() as store, store.transaction() as connection:
            return LifecycleRepository(connection, self.workspace.workspace_id).bind_session(feature, column, session_id, actor=actor)

    def status(self, reference: str) -> dict[str, Any]:
        feature = self._bind_discovered(self._get(reference), actor="lifecycle:discovery")
        target = successor(feature["mode"], feature["stage"])
        readiness = self.evidence.can_leave(feature, feature["stage"]) if target else Readiness(False, ["The feature is complete."])
        invariant = self.evidence.violated_invariant(feature)
        with self._store() as store:
            history = LifecycleRepository(store.read(), self.workspace.workspace_id).transitions(feature["id"])
        return {
            "feature": {key: feature[key] for key in ("id", "slug", "name", "mode", "stage", "status", "blocked_reason", "revision")},
            "sessions": {key: feature[key] for key in ("ideator_session_id", "designer_session_id", "planner_session_id")},
            "next_stage": target,
            "ready_to_advance": bool(target) and readiness.ready and feature["status"] == "ACTIVE" and invariant is None,
            "blockers": ([f"The feature is blocked: {feature['blocked_reason']}"] if feature["status"] == "BLOCKED" else []) + readiness.blockers,
            "stale": None if invariant is None else {"reopen_to": invariant[0], "reason": invariant[1]},
            "next_actions": list(NEXT_ACTIONS[feature["stage"]]),
            "history_length": len(history),
        }

    def advance(self, reference: str, *, actor: str, idempotency_key: str | None = None) -> dict[str, Any]:
        """Move one step forward if (and only if) the current stage's exit evidence is satisfied."""
        feature = self._bind_discovered(self._get(reference), actor=actor)
        if idempotency_key:
            with self._store() as store:
                replay = LifecycleRepository(store.read(), self.workspace.workspace_id).transition_by_key(feature["id"], idempotency_key)
            if replay is not None:
                return {"replayed": True, "transition": replay, "feature": self._get(feature["id"])}
        if feature["status"] == "BLOCKED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"The feature is blocked: {feature['blocked_reason']}",
                             recovery="Resolve the blocker, then `dridh feature unblock`.")
        target = successor(feature["mode"], feature["stage"])
        if target is None:
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The feature is already COMPLETED.")
        if feature["stage"] == "DELIVERY_REVIEW":
            raise DridhError(ErrorCode.APPROVAL_REQUIRED, "Completing a feature is a human decision.",
                             recovery="Run `dridh feature complete` in a terminal.")
        invariant = self.evidence.violated_invariant(feature)
        if invariant is not None:
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"An earlier approval is no longer valid: {invariant[1]}",
                             details=(f"reopen to {invariant[0]}",), recovery="Run `dridh feature sync` to reopen the review.")
        readiness = self.evidence.can_leave(feature, feature["stage"])
        if not readiness.ready:
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"Cannot leave {feature['stage']}: {readiness.blockers[0]}",
                             details=tuple(readiness.blockers), recovery="Complete the work for this stage, then retry.")
        with self._store() as store, store.transaction() as connection:
            updated = LifecycleRepository(connection, self.workspace.workspace_id).record_transition(
                feature, to_stage=target, kind="ADVANCE", reason=f"{feature['stage']} exit condition satisfied.",
                evidence=readiness.evidence, actor=actor, idempotency_key=idempotency_key,
            )
        return {"replayed": False, "feature": updated}

    def sync(self, reference: str, *, actor: str) -> dict[str, Any]:
        """Reopen stale reviews, then advance as far as the evidence allows. Idempotent."""
        moves: list[dict[str, Any]] = []
        for _ in range(self.MAX_STEPS):
            feature = self._bind_discovered(self._get(reference), actor=actor)
            if feature["status"] == "BLOCKED":
                break
            invariant = self.evidence.violated_invariant(feature)
            if invariant is not None:
                updated = self._reopen(feature, invariant[0], invariant[1], actor)
                moves.append({"kind": "REOPEN", "from": feature["stage"], "to": updated["stage"], "reason": invariant[1]})
                continue
            target = successor(feature["mode"], feature["stage"])
            if target is None or feature["stage"] == "DELIVERY_REVIEW":
                break
            readiness = self.evidence.can_leave(feature, feature["stage"])
            if not readiness.ready:
                break
            with self._store() as store, store.transaction() as connection:
                updated = LifecycleRepository(connection, self.workspace.workspace_id).record_transition(
                    feature, to_stage=target, kind="ADVANCE", reason=f"{feature['stage']} exit condition satisfied.",
                    evidence=readiness.evidence, actor=actor,
                )
            moves.append({"kind": "ADVANCE", "from": feature["stage"], "to": updated["stage"], "reason": "exit condition satisfied"})
        return {"moves": moves, "status": self.status(reference)}

    def reopen(self, reference: str, *, to_stage: str, reason: str, actor: str) -> dict[str, Any]:
        feature = self._get(reference)
        if to_stage not in STAGES or STAGES.index(to_stage) >= STAGES.index(feature["stage"]):
            raise DridhError(ErrorCode.INVALID_TRANSITION, "A feature can only be reopened to an earlier stage.",
                             details=(f"from {feature['stage']}", f"to {to_stage}"))
        if to_stage == "CODEBASE_INDEXING" and feature["mode"] == "new":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "A new codebase has no indexing stage.")
        if not reason.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A reason is required to reopen a stage.")
        return self._reopen(feature, to_stage, reason.strip(), actor)

    def _reopen(self, feature: dict[str, Any], to_stage: str, reason: str, actor: str) -> dict[str, Any]:
        with self._store() as store, store.transaction() as connection:
            return LifecycleRepository(connection, self.workspace.workspace_id).record_transition(
                feature, to_stage=to_stage, kind="REOPEN", reason=reason, evidence={"from": feature["stage"]}, actor=actor
            )

    def block(self, reference: str, *, reason: str, actor: str) -> dict[str, Any]:
        feature = self._get(reference)
        if not reason.strip():
            raise DridhError(ErrorCode.VALIDATION_FAILED, "A reason is required to block a feature.")
        with self._store() as store, store.transaction() as connection:
            return LifecycleRepository(connection, self.workspace.workspace_id).set_blocked(feature, blocked=True, reason=reason.strip(), actor=actor)

    def unblock(self, reference: str, *, reason: str, actor: str) -> dict[str, Any]:
        feature = self._get(reference)
        if feature["status"] != "BLOCKED":
            raise DridhError(ErrorCode.INVALID_TRANSITION, "The feature is not blocked.")
        with self._store() as store, store.transaction() as connection:
            return LifecycleRepository(connection, self.workspace.workspace_id).set_blocked(
                feature, blocked=False, reason=reason.strip() or "Unblocked.", actor=actor
            )

    def complete(self, reference: str, *, reviewer: str, human_confirmed: bool) -> dict[str, Any]:
        """DELIVERY_REVIEW -> COMPLETED. Reachable only from an interactive human confirmation (CLI)."""
        feature = self._get(reference)
        if not human_confirmed or not reviewer.strip():
            raise DridhError(ErrorCode.INVALID_TRANSITION, "Completing a feature requires explicit human confirmation and a reviewer name.")
        if feature["stage"] != "DELIVERY_REVIEW":
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"Only a feature in DELIVERY_REVIEW can be completed (this one is in {feature['stage']}).")
        invariant = self.evidence.violated_invariant(feature)
        if invariant is not None:
            raise DridhError(ErrorCode.INVALID_TRANSITION, f"An earlier approval is no longer valid: {invariant[1]}")
        with self._store() as store, store.transaction() as connection:
            updated = LifecycleRepository(connection, self.workspace.workspace_id).record_transition(
                feature, to_stage="COMPLETED", kind="ADVANCE", reason=f"Completed by human reviewer {reviewer.strip()}.",
                evidence={"reviewer": reviewer.strip(), "human_confirmed": True}, actor=f"human:{reviewer.strip()}",
            )
        return {"feature": updated}
