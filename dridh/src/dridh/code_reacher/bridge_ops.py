"""Bridge operations for Code Reacher.

Any Dridh agent may query the index (the Planner and Executor are told to ask Code Reacher before searching the
repository by hand). Only the Coordinator may start indexing, which writes to the workspace database.
"""

from __future__ import annotations

from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.protocol import BridgeRequest
from dridh.workspace.layout import open_workspace
from dridh.workspace.paths import validate_workspace_root

COORDINATOR = "dridh/coordinator"


def _text(payload: dict[str, Any], key: str, operation: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{operation} requires `{key}` as a non-empty string of at most 500 characters.")
    return value.strip()


def _integer(payload: dict[str, Any], key: str, default: int, low: int, high: int, operation: str) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DridhError(ErrorCode.VALIDATION_FAILED, f"{operation}: `{key}` must be an integer from {low} to {high}.")
    return value


def dispatch_code_reacher(request: BridgeRequest) -> dict[str, Any]:
    caller = request.caller
    if caller is None or caller.source != "plugin" or not (caller.agent or "").startswith("dridh/"):
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "Code Reacher is available only to Dridh OpenCode agents.",
            recovery="Call it from a Dridh agent session; humans use `dridh code` and `dridh index`.",
        )
    operation, payload = request.operation, request.payload
    info = open_workspace(validate_workspace_root(request.workspace_root))
    from dridh.code_reacher.search import CodeSearch

    search = CodeSearch(info)
    if operation == "code_reacher.lookup":
        semantic = payload.get("semantic", True)
        if not isinstance(semantic, bool):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "code_reacher.lookup: `semantic` must be a boolean.")
        return search.lookup(_text(payload, "query", operation), limit=_integer(payload, "limit", 10, 1, 50, operation), semantic=semantic)
    if operation == "code_reacher.symbol":
        return search.symbol(_text(payload, "name", operation))
    if operation == "code_reacher.dependencies":
        direction = payload.get("direction", "both")
        if direction not in ("callers", "callees", "both"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "code_reacher.dependencies: `direction` must be callers, callees or both.")
        return search.dependencies(_text(payload, "symbol", operation), direction=direction, depth=_integer(payload, "depth", 2, 1, 5, operation))
    if operation in ("code_reacher.code_for", "code_reacher.owners"):
        from dridh.code_reacher.mapping import StoryCodeMapper

        mapper = StoryCodeMapper(info)
        if operation == "code_reacher.code_for":
            return mapper.code_for(_text(payload, "target", operation))
        return mapper.targets_for(_text(payload, "path", operation))
    if operation == "code_reacher.index_status":
        return search.status()
    if operation == "code_reacher.index":
        if caller.agent != COORDINATOR:
            raise DridhError(
                ErrorCode.AUTHORIZATION_DENIED,
                "Only the Dridh Coordinator may start indexing.",
                details=(f"caller: {caller.agent}",),
                recovery="Ask the Coordinator to run code_reacher.index, then query the result.",
            )
        embed = payload.get("embed", True)
        if not isinstance(embed, bool):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "code_reacher.index: `embed` must be a boolean.")
        from dridh.code_reacher.indexer import CodeIndexer

        return CodeIndexer(info).index(embed=embed)
    raise DridhError(ErrorCode.VALIDATION_FAILED, f"Unknown Code Reacher operation {operation}.")
