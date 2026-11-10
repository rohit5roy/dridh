"""Bridge operations for workflows.

Only the Dridh Coordinator, acting through the plugin, may call these. Approval
and rejection are not operations at all: they exist only in the terminal
interface, where a person can type a confirmation.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.protocol import BridgeRequest
from dridh.designer.service import DesignerService
from dridh.ideator.service import IdeatorService
from dridh.orchestration.engine import WorkflowEngine
from dridh.orchestration.legacy import require_legacy
from dridh.orchestration.roster import COORDINATOR
from dridh.planner.service import PlannerService
from dridh.workspace.layout import init_workspace, open_workspace
from dridh.workspace.paths import validate_workspace_root

WORKFLOW_ID = re.compile(r"^wf-[0-9a-f]{12}$")
TASK_ID = re.compile(r"^T-\d{3}$")


def _actor(request: BridgeRequest) -> str:
    caller = request.caller
    if caller is None:
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "Workflow operations need a caller identity.",
            recovery="Call workflow operations from the Dridh Coordinator session.",
        )
    if caller.source != "plugin" or caller.agent != COORDINATOR:
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "Only the Dridh Coordinator, acting through the plugin, may call workflow operations.",
            details=(f"source: {caller.source}", f"caller: {caller.agent}"),
            recovery="Use the Dridh Coordinator session to drive the workflow. The CLI uses the engine directly.",
        )
    return f"agent:{caller.agent}"


def _workflow_id(payload: dict[str, Any]) -> str:
    value = payload.get("workflow_id")
    if not isinstance(value, str) or not WORKFLOW_ID.match(value):
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "workflow_id must look like wf-0123456789ab.",
            details=(f"received: {value!r}"[:80],),
        )
    return value


def dispatch_orchestration(request: BridgeRequest) -> dict[str, Any]:
    actor = _actor(request)
    root = validate_workspace_root(request.workspace_root)
    payload = request.payload
    operation = request.operation

    if operation == "workspace.init":
        requested_mode = payload.get("mode")
        info, created = init_workspace(root, mode=requested_mode)
        # The bridge cannot ask the human, so when no mode is passed the Git hint is used and reported; the
        # Coordinator must confirm it with the user (pass `mode` after asking).
        return {"created": created, "mode_inferred": created and requested_mode is None, "workspace": info.to_dict()}

    info = open_workspace(root)
    if operation.startswith("coordinator."):
        return _dispatch_coordinator(info, operation, payload, actor)
    engine = WorkflowEngine(info, actor=actor)
    if operation.startswith("ideator."):
        ideator = IdeatorService(info)
        if operation == "ideator.start":
            objective = payload.get("objective")
            context = payload.get("existing_context", "")
            if not isinstance(objective, str) or not isinstance(context, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "objective and existing_context must be strings.")
            feature = _ideator_feature(info, payload.get("feature"))
            started = ideator.start(objective, existing_context=context)
            if feature is None:
                started["warning"] = ("No feature is bound to this Ideator session, so the lifecycle cannot advance. "
                                      "Create the feature first (coordinator.create_feature) and pass its slug as `feature`.")
                return started
            from dridh.coordinator.lifecycle import FeatureLifecycle

            FeatureLifecycle(info).bind_session(feature["slug"], "ideator_session_id", started["session_id"], actor=actor)
            started["feature"] = feature["slug"]
            return started
        if operation == "ideator.answer":
            required = ("session_id", "topic", "question", "answer")
            if any(not isinstance(payload.get(key), str) for key in required):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "ideator.answer requires string session_id, topic, question and answer.")
            actor_refs = payload.get("actor_refs", [])
            flow_refs = payload.get("flow_refs", [])
            if not isinstance(actor_refs, list) or not isinstance(flow_refs, list):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "actor_refs and flow_refs must be arrays.")
            return ideator.record_answer(
                payload["session_id"],
                topic=payload["topic"],
                question=payload["question"],
                answer=payload["answer"],
                source=str(payload.get("source", "user")),
                unresolved=payload.get("unresolved", False),
                actor_refs=actor_refs,
                flow_refs=flow_refs,
            )
        if operation in {"ideator.status", "ideator.parity"}:
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "session_id must be a string.")
            return ideator.status(session_id) if operation == "ideator.status" else ideator.parity(session_id)
        if operation == "ideator.generate":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "session_id must be a string.")
            model = payload.get("model")
            if model is not None and not isinstance(model, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "model must be a string.")
            return ideator.generate(session_id, model=model)
        if operation == "ideator.gate":
            session_id = payload.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "session_id must be a string.")
            return ideator.gate(session_id)
    if operation.startswith("designer."):
        designer = DesignerService(info)
        if operation == "designer.start":
            ideator_session_id = payload.get("ideator_session_id")
            feature = payload.get("feature")
            if not isinstance(ideator_session_id, str) or not isinstance(feature, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "designer.start requires ideator_session_id and feature strings.")
            existing_context = payload.get("existing_context", "")
            evidence = payload.get("code_reacher_evidence", "")
            model = payload.get("model")
            if not isinstance(existing_context, str) or not isinstance(evidence, str) or (model is not None and not isinstance(model, str)):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "Designer context/evidence must be strings and model must be a string when supplied.")
            return designer.start(ideator_session_id, feature=feature, existing_context=existing_context, code_reacher_evidence=evidence, model=model)
        if operation == "designer.answer":
            required = ("session_id", "topic", "answer")
            if any(not isinstance(payload.get(key), str) for key in required):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "designer.answer requires string session_id, topic and answer.")
            refs = {}
            for key in ("related_use_case_ids", "related_flow_ids", "related_component_ids"):
                value = payload.get(key, [])
                if not isinstance(value, list):
                    raise DridhError(ErrorCode.VALIDATION_FAILED, f"{key} must be an array.")
                refs[key] = value
            return designer.answer(
                payload["session_id"],
                topic=payload["topic"],
                answer=payload["answer"],
                source=str(payload.get("source", "user")),
                impact=payload.get("impact"),
                unresolved=payload.get("unresolved", False),
                **refs,
            )
        if operation in {"designer.status", "designer.parity"}:
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "session_id must be a string.")
            return designer.status(session_id) if operation == "designer.status" else designer.parity(session_id)
        if operation == "designer.generate":
            session_id = payload.get("session_id")
            model = payload.get("model")
            if not isinstance(session_id, str) or (model is not None and not isinstance(model, str)):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "designer.generate requires a session_id and optional string model.")
            return designer.generate(session_id, model=model)
        if operation == "designer.gate":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "designer.gate requires a session_id.")
            return designer.gate(session_id)
    if operation.startswith("planner."):
        planner = PlannerService(info)
        if operation == "planner.start":
            designer_session_id = payload.get("designer_session_id")
            model = payload.get("model")
            if not isinstance(designer_session_id, str) or (model is not None and not isinstance(model, str)):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "planner.start requires designer_session_id and an optional model string.")
            return planner.start(designer_session_id, model=model)
        if operation == "planner.clarify":
            session_id = payload.get("session_id")
            question, answer = payload.get("question"), payload.get("answer")
            source, model = payload.get("source", "user"), payload.get("model")
            if not isinstance(session_id, str) or not isinstance(question, str) or not isinstance(answer, str) or not isinstance(source, str) or (model is not None and not isinstance(model, str)):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "planner.clarify requires session_id, question, answer, source and optional model strings.")
            return planner.clarify(session_id, question=question, answer=answer, source=source, model=model)
        if operation == "planner.status":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "planner.status requires a session_id.")
            return planner.status(session_id)
        if operation == "planner.generate":
            session_id = payload.get("session_id")
            model = payload.get("model")
            if not isinstance(session_id, str) or (model is not None and not isinstance(model, str)):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "planner.generate requires a session_id and optional model string.")
            return planner.generate(session_id, model=model)
        if operation == "planner.sync":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "planner.sync requires a session_id.")
            return planner.sync(session_id)
        if operation == "planner.gate":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "planner.gate requires a session_id.")
            return planner.gate(session_id)
    if operation == "workspace.status":
        return {"workspace": info.to_dict(), "ledger": engine.verify_ledger()}
    if operation == "workflow.create":
        require_legacy("workflow.create")
        return engine.create(str(payload.get("objective", "")))
    if operation == "workflow.verify_ledger":
        return engine.verify_ledger()

    workflow_id = _workflow_id(payload)
    if operation == "workflow.plan":
        require_legacy("workflow.plan")
        return asyncio.run(engine.plan(workflow_id))
    if operation == "workflow.next":
        return engine.next_task(workflow_id)
    if operation == "workflow.execute":
        task_id = payload.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not TASK_ID.match(task_id)):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "task_id must look like T-001.")
        return asyncio.run(engine.execute_task(workflow_id, task_id))
    if operation == "workflow.status":
        return engine.status(workflow_id)
    if operation == "workflow.report":
        return engine.report(workflow_id)
    if operation == "workflow.cancel":
        return engine.cancel(workflow_id, reason=str(payload.get("reason", "requested by the Coordinator"))[:300])
    raise DridhError(ErrorCode.VALIDATION_FAILED, "Unknown orchestration operation.", details=(operation,))


def _ideator_feature(info: Any, requested: Any) -> dict[str, Any] | None:
    """The feature a new Ideator session belongs to (bound at start, so the lifecycle can advance).

    One feature has one Ideator session: starting another for a feature that already has one is refused, so a stuck agent
    cannot pile up sessions; it continues the bound session instead.
    """
    from dridh.coordinator.lifecycle import FeatureLifecycle

    if requested is not None and (not isinstance(requested, str) or not requested.strip()):
        raise DridhError(ErrorCode.VALIDATION_FAILED, "ideator.start takes an optional feature slug as a string.")
    features = [f for f in FeatureLifecycle(info).list_features() if f.get("status") == "ACTIVE"]
    if requested:
        chosen = next((f for f in features if f["slug"] == requested.strip()), None)
        if chosen is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown or inactive feature '{requested}'.",
                             details=tuple(f["slug"] for f in features) or ("no features exist yet",))
    else:
        if not features:
            return None
        open_features = [f for f in features if not f.get("ideator_session_id")]
        if len(open_features) != 1:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "ideator.start needs `feature`: say which feature this discovery is for.",
                             details=tuple(f["slug"] for f in open_features) or ("every active feature already has an Ideator session",))
        chosen = open_features[0]
    if chosen.get("ideator_session_id"):
        raise DridhError(
            ErrorCode.CONFIGURATION_CONFLICT,
            f"Feature '{chosen['slug']}' already has Ideator session {chosen['ideator_session_id']}; a feature has one.",
            recovery="Continue that session with ideator.status, ideator.answer and ideator.generate. Do not start test or replacement sessions.",
        )
    return chosen


def _dispatch_coordinator(info: Any, operation: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Feature lifecycle operations. Every transition is decided by the stage evidence, never by the caller."""

    from dridh.coordinator.lifecycle import FeatureLifecycle

    lifecycle = FeatureLifecycle(info)
    feature = payload.get("feature")
    if feature is not None and not isinstance(feature, str):
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{operation} takes an optional feature slug as a string.")

    def reference() -> str:
        if feature:
            return feature
        features = lifecycle.list_features()
        if len(features) == 1:
            return features[0]["slug"]
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{operation} needs a feature slug.",
                         details=tuple(item["slug"] for item in features) or ("no features exist yet",))

    if operation in ("coordinator.get_context", "coordinator.query", "coordinator.validate", "coordinator.list_reviews", "coordinator.review_checklist"):
        from dridh.coordinator import queries

        if operation == "coordinator.list_reviews":
            status = payload.get("status")
            if status is not None and not isinstance(status, str):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "coordinator.list_reviews takes an optional status string.")
            return queries.list_reviews(info, status=status)
        if operation == "coordinator.review_checklist":
            review_id = payload.get("review_id")
            if not isinstance(review_id, str) or not review_id.strip() or len(review_id) > 80:
                raise DridhError(ErrorCode.VALIDATION_FAILED, "coordinator.review_checklist requires a review_id string.")
            return queries.review_checklist(info, review_id)

        if operation == "coordinator.get_context":
            return queries.get_context(info, feature or None)
        if operation == "coordinator.query":
            kind = payload.get("kind")
            optional = {key: payload.get(key) for key in ("feature", "status", "run_id")}
            if not isinstance(kind, str) or any(v is not None and not isinstance(v, str) for v in optional.values()):
                raise DridhError(ErrorCode.VALIDATION_FAILED, "coordinator.query requires a string kind and optional string status and run_id.")
            return queries.query(info, kind, status=optional["status"], run_id=optional["run_id"])
        kind, path = payload.get("kind"), payload.get("path")
        if not isinstance(kind, str) or not isinstance(path, str):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "coordinator.validate requires string kind and path.")
        return queries.validate_document(info, kind, path)
    if operation == "coordinator.create_feature":
        name = payload.get("name")
        mode = payload.get("mode")
        if not isinstance(name, str) or not name.strip() or (mode is not None and not isinstance(mode, str)):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "coordinator.create_feature requires a name and an optional mode string.")
        return {"feature": lifecycle.create_feature(name, actor=actor, mode=mode)}
    if operation == "coordinator.status":
        return lifecycle.status(reference())
    if operation == "coordinator.sync":
        return lifecycle.sync(reference(), actor=actor)
    if operation == "coordinator.transition":
        key = payload.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not key or len(key) > 120):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "idempotency_key must be a non-empty string of at most 120 characters.")
        return lifecycle.advance(reference(), actor=actor, idempotency_key=key)
    raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown coordinator operation {operation}.")
