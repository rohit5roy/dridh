"""Typed JSON bridge protocol v1."""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import dridh
from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact
from dridh.workspace.paths import validate_workspace_root

PROTOCOL_VERSION = dridh.BRIDGE_PROTOCOL_VERSION
RUNTIME_OPERATIONS = frozenset({"runtime.ping", "runtime.info"})
JEV_OPERATIONS = frozenset({"jev.decide"})
IDEATOR_OPERATIONS = frozenset({"ideator.start", "ideator.answer", "ideator.status", "ideator.generate", "ideator.parity", "ideator.gate"})
DESIGNER_OPERATIONS = frozenset({"designer.start", "designer.answer", "designer.status", "designer.generate", "designer.parity", "designer.gate"})
PLANNER_OPERATIONS = frozenset({"planner.start", "planner.clarify", "planner.status", "planner.generate", "planner.sync", "planner.gate"})
# Feature lifecycle. There is deliberately no operation that completes a feature or approves anything: those are human-only CLI commands.
COORDINATOR_OPERATIONS = frozenset({
    "coordinator.create_feature", "coordinator.status", "coordinator.sync", "coordinator.transition",
    "coordinator.get_context", "coordinator.query", "coordinator.validate", "coordinator.list_reviews", "coordinator.review_checklist",  # read-only
})
# Code Reacher: queries are open to Dridh agents; indexing is Coordinator-only (enforced in the dispatcher).
CODE_REACHER_OPERATIONS = frozenset({
    "code_reacher.lookup", "code_reacher.symbol", "code_reacher.dependencies", "code_reacher.index_status", "code_reacher.index",
    "code_reacher.code_for", "code_reacher.owners",
})
ORCHESTRATION_OPERATIONS = frozenset(
    {
        "workspace.init",
        "workspace.status",
        "workflow.create",
        "workflow.plan",
        "workflow.next",
        "workflow.execute",
        "workflow.status",
        "workflow.report",
        "workflow.cancel",
        "workflow.verify_ledger",
    }
)
ORCHESTRATION_OPERATIONS = ORCHESTRATION_OPERATIONS | IDEATOR_OPERATIONS | DESIGNER_OPERATIONS | PLANNER_OPERATIONS | COORDINATOR_OPERATIONS
# Called by the plugin's OpenCode hooks only (not in the agent tool's allow-list); see dridh.hooks.
HOOK_OPERATIONS = frozenset({"hook.context", "hook.tool_call", "hook.session_event"})
ALLOWED_OPERATIONS = RUNTIME_OPERATIONS | JEV_OPERATIONS | ORCHESTRATION_OPERATIONS | CODE_REACHER_OPERATIONS | HOOK_OPERATIONS
_UNAVAILABLE_REQUEST_ID = "unavailable"


class CallerContext(BaseModel):
    """Who asked. Supplied by the plugin from OpenCode's own session context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["plugin", "cli"]
    agent: str | None = None
    session_id: str | None = None

    @field_validator("agent")
    @classmethod
    def _bounded_agent(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 100):
            raise ValueError("agent must be a non-empty identifier of at most 100 characters")
        return value


class BridgeRequest(BaseModel):
    """One bridge request envelope. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: int
    request_id: str
    workspace_root: str
    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)
    caller: CallerContext | None = None

    @field_validator("protocol_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version: {value}")
        return value

    @field_validator("request_id")
    @classmethod
    def _uuid_request_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("request_id must be a UUID") from exc

    @field_validator("workspace_root")
    @classmethod
    def _absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("workspace_root must be an absolute path")
        return value

    @field_validator("operation")
    @classmethod
    def _non_empty_operation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operation must not be empty")
        return value


class BridgeError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    details: list[str] = Field(default_factory=list)
    retryable: bool = False
    recovery: str


class BridgeSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = 1
    request_id: str
    ok: Literal[True] = True
    data: dict[str, Any]


class BridgeFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = 1
    request_id: str
    ok: Literal[False] = False
    error: BridgeError


class BridgeRequestError(DridhError):
    """A request error that still carries a safe request ID for the failure envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        request_id: str = _UNAVAILABLE_REQUEST_ID,
        details: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)
        self.request_id = request_id


def decode_request(raw: str | bytes) -> BridgeRequest:
    """Parse and validate exactly one request object.

    JSON/syntax, protocol version and malformed-envelope failures are
    ``PROTOCOL_ERROR``; semantic field failures (bad request ID, relative
    workspace, unknown operation) are ``VALIDATION_FAILED``.
    """

    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BridgeRequestError(
                ErrorCode.PROTOCOL_ERROR,
                "request must be UTF-8 encoded JSON.",
                details=(f"decode error at byte {exc.start}",),
            ) from None
    else:
        text = raw

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BridgeRequestError(
            ErrorCode.PROTOCOL_ERROR,
            "request must be valid JSON.",
            details=(f"line {exc.lineno}, column {exc.colno}",),
        ) from None

    if not isinstance(parsed, dict):
        raise BridgeRequestError(
            ErrorCode.PROTOCOL_ERROR,
            "request must be a JSON object.",
            details=(f"received: {type(parsed).__name__}",),
        )

    raw_request_id = parsed.get("request_id")
    request_id = (
        str(redact(raw_request_id))[:200]
        if isinstance(raw_request_id, str)
        else _UNAVAILABLE_REQUEST_ID
    )

    try:
        request = BridgeRequest.model_validate(parsed)
    except ValidationError as exc:
        locations = {str(part) for error in exc.errors() for part in error.get("loc", ())}
        code = ErrorCode.PROTOCOL_ERROR if "protocol_version" in locations else ErrorCode.VALIDATION_FAILED
        details = [
            f"{'.'.join(str(part) for part in error.get('loc', ())) or 'request'}: {error.get('msg', 'invalid')}"
            for error in exc.errors()
        ]
        raise BridgeRequestError(code, "Invalid bridge request.", request_id=request_id, details=details) from None

    if request.operation not in ALLOWED_OPERATIONS:
        raise BridgeRequestError(
            ErrorCode.VALIDATION_FAILED,
            "Unknown bridge operation.",
            request_id=request.request_id,
            details=(f"operation: {request.operation}", f"allowed: {', '.join(sorted(ALLOWED_OPERATIONS))}"),
        )
    return request


def _runtime_info(workspace_root: Path) -> dict[str, Any]:
    package_root = Path(dridh.__file__).resolve().parent
    return {
        "package": "dridh",
        "package_version": dridh.__version__,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "bridge_protocol_version": PROTOCOL_VERSION,
        "runtime_location": str(package_root),
        "workspace_root": str(workspace_root),
        "operations": sorted(ALLOWED_OPERATIONS),
        "heavy_dependencies_loaded": any(
            name in sys.modules
            for name in ("fastembed", "onnxruntime", "tree_sitter", "sqlite_vec", "graphqlite")
        ),
    }


def _audit_invocation(request: BridgeRequest, ok: bool, error_code: str | None, started: float) -> None:
    """Best-effort audit row. The record never changes the outcome of the request it describes."""

    if request.operation.startswith(("runtime.", "hook.")):  # hook.* records its own, more precise rows
        return
    try:
        from dridh.state.invocations import InvocationRepository
        from dridh.state.store import StateStore
        from dridh.workspace.layout import open_workspace

        info = open_workspace(validate_workspace_root(request.workspace_root))
        with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
            InvocationRepository(connection, info.workspace_id).record(
                request_id=request.request_id, operation=request.operation, caller=request.caller, payload=request.payload,
                ok=ok, error_code=error_code, started=started)
    except Exception:  # noqa: BLE001 - auditing must never break a request (no workspace yet, older schema, locked database)
        return


def dispatch(request: BridgeRequest) -> BridgeSuccess:
    """Validate the workspace, run one allow-listed operation and audit it."""

    started = time.monotonic()
    try:
        success = _dispatch(request)
    except DridhError as exc:
        _audit_invocation(request, False, exc.code.value, started)
        raise
    _audit_invocation(request, True, None, started)
    return success


def _dispatch(request: BridgeRequest) -> BridgeSuccess:
    workspace_root = validate_workspace_root(request.workspace_root)
    if request.operation == "runtime.ping":
        data: dict[str, Any] = {
            "status": "healthy",
            "bridge_protocol_version": PROTOCOL_VERSION,
            "workspace_root": str(workspace_root),
        }
    elif request.operation == "runtime.info":
        data = _runtime_info(workspace_root)
    elif request.operation in ORCHESTRATION_OPERATIONS:
        from dridh.orchestration.bridge_ops import dispatch_orchestration

        data = dispatch_orchestration(request)
    elif request.operation in CODE_REACHER_OPERATIONS:
        from dridh.code_reacher.bridge_ops import dispatch_code_reacher

        data = dispatch_code_reacher(request)
    elif request.operation in HOOK_OPERATIONS:
        from dridh.hooks import dispatch_hook

        data = dispatch_hook(request)
    elif request.operation in JEV_OPERATIONS:
        from dridh.jev.bridge_ops import dispatch_jev

        data = dispatch_jev(request)
    else:  # defensive: decode_request already rejected unknown operations
        raise DridhError(
            ErrorCode.VALIDATION_FAILED,
            "Unknown bridge operation.",
            details=(f"operation: {request.operation}",),
        )
    return BridgeSuccess(request_id=request.request_id, data=data)


def failure_envelope(error: DridhError, request_id: str = _UNAVAILABLE_REQUEST_ID) -> BridgeFailure:
    return BridgeFailure(
        request_id=request_id,
        error=BridgeError(
            code=error.code.value,
            message=error.message,
            details=list(error.details),
            retryable=error.retryable,
            recovery=error.recovery,
        ),
    )


def handle_request(raw: str | bytes) -> BridgeSuccess | BridgeFailure:
    """Convert one raw request into exactly one response model."""

    try:
        request = decode_request(raw)
    except BridgeRequestError as exc:
        return failure_envelope(exc, exc.request_id)
    except DridhError as exc:
        return failure_envelope(exc)
    except Exception as exc:  # noqa: BLE001
        from dridh.common.errors import internal_error

        return failure_envelope(internal_error(exc))

    try:
        return dispatch(request)
    except DridhError as exc:
        return failure_envelope(exc, request.request_id)
    except Exception as exc:  # noqa: BLE001
        from dridh.common.errors import internal_error

        return failure_envelope(internal_error(exc), request.request_id)


def serialise_response(response: BridgeSuccess | BridgeFailure) -> str:
    """Serialise exactly one compact JSON line."""

    return json.dumps(response.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
