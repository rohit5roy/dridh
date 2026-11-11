# Dridh Bridge Protocol v1

**Implementation:** `dridh/src/dridh/common/protocol.py`, `dridh/src/dridh/common/bridge.py`, `dridh/src/dridh/common/errors.py`  
**Transport:** one JSON request on stdin, one JSON response on stdout  
**Version:** `protocol_version: 1` (integer)

The bridge is the only interface between the OpenCode plugin and the Dridh Python runtime. The plugin starts `dridh bridge` for each call, sends one request, reads one response and exits.

## 1. Request envelope

```json
{
  "protocol_version": 1,
  "request_id": "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01",
  "workspace_root": "/absolute/path/to/workspace",
  "operation": "runtime.ping",
  "payload": {},
  "caller": { "source": "plugin", "agent": "dridh/coordinator", "session_id": "ses_example" }
}
```

| Field | Required | Type | Rules |
|---|---|---|---|
| `protocol_version` | yes | integer | Must equal `1`; any other value is `PROTOCOL_ERROR` |
| `request_id` | yes | string | Must be a UUID; normalised to lowercase canonical form and echoed |
| `workspace_root` | yes | string | Must be an absolute path; dispatch requires an existing directory |
| `operation` | yes | string | Must be one of the allow-listed operations (section 4) |
| `payload` | no | object | Defaults to `{}`; arrays and scalars are rejected |
| `caller` | no | object | Who asked; see below. Omitted when the caller is not known |

Unknown top-level fields are rejected (`extra="forbid"`). The same rule applies inside `caller`.

### The `caller` field

`caller` is supplied by the plugin from OpenCode's own session context. It records who asked; it does not grant any permission.

| Field | Required | Rules |
|---|---|---|
| `source` | yes | `plugin` or `cli` |
| `agent` | no | Non-empty identifier, at most 100 characters (for example `dridh/coordinator`) |
| `session_id` | no | The OpenCode session ID |

The plugin sends `source: "plugin"` with the agent name and session when OpenCode provides them. The schema also accepts `source: "cli"`, but no code in this repository sends it. The bridge records the caller in the invocation audit row.

## 2. Success envelope

```json
{
  "protocol_version": 1,
  "request_id": "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01",
  "ok": true,
  "data": {
    "status": "healthy",
    "bridge_protocol_version": 1,
    "workspace_root": "/absolute/path/to/workspace"
  }
}
```

## 3. Failure envelope

```json
{
  "protocol_version": 1,
  "request_id": "3f0d6f1e-2f7a-4c39-9f31-2a8c2f6f2a01",
  "ok": false,
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Unknown bridge operation.",
    "details": ["operation: runtime.unknown"],
    "retryable": false,
    "recovery": "Correct the request fields identified in details, then retry."
  }
}
```

`request_id` is echoed when it can be validated. If the request ID cannot be parsed, the failure uses `"unavailable"`. `error.details` is always an array of redacted strings.

## 4. Operations

Every operation name is listed in `protocol.py`. The bridge dispatches a request only if its name is in that allow-list; anything else is `VALIDATION_FAILED`.

| Group | Operations |
|---|---|
| runtime | `runtime.ping`, `runtime.info` |
| jev | `jev.decide` |
| workflow | `workspace.init`, `workspace.status`, `workflow.create`, `workflow.plan`, `workflow.next`, `workflow.execute`, `workflow.status`, `workflow.report`, `workflow.cancel`, `workflow.verify_ledger` |
| ideator | `ideator.start`, `ideator.answer`, `ideator.status`, `ideator.generate`, `ideator.parity`, `ideator.gate` |
| designer | `designer.start`, `designer.answer`, `designer.status`, `designer.generate`, `designer.parity`, `designer.gate` |
| planner | `planner.start`, `planner.clarify`, `planner.status`, `planner.generate`, `planner.sync`, `planner.gate` |
| coordinator | `coordinator.create_feature`, `coordinator.status`, `coordinator.sync`, `coordinator.transition`, `coordinator.get_context`, `coordinator.query`, `coordinator.validate`, `coordinator.list_reviews`, `coordinator.review_checklist` |
| code reacher | `code_reacher.lookup`, `code_reacher.symbol`, `code_reacher.dependencies`, `code_reacher.index_status`, `code_reacher.index`, `code_reacher.code_for`, `code_reacher.owners` |
| hook | `hook.context`, `hook.tool_call`, `hook.session_event` |

`runtime.ping` returns `status`, `bridge_protocol_version` and `workspace_root`. `runtime.info` returns the package and Python version, the runtime location, the operation list and `heavy_dependencies_loaded`.

Rules that the code states:

- No bridge operation approves a plan, a document or an execution lot, and none completes a feature. Those are human-only CLI commands, typed by the person in the terminal. The Coordinator operations either read state or record lifecycle changes. `coordinator.review_checklist` is read-only.
- Code Reacher queries are open to Dridh agents. Indexing (`code_reacher.index`) is Coordinator-only, enforced in the dispatcher.
- The hook operations are called only by the plugin's own OpenCode hooks. They are not in the plugin tool's allow-list, so agents cannot call or forge them.

## 5. Error codes

Every error has a stable `code`, safe `message`, redacted `details`, a `retryable` flag and a `recovery` sentence. `retryable` is the default for the code unless the error sets it.

| Code | Default `retryable` | Meaning |
|---|---|---|
| `CONFIGURATION_ERROR` | false | Dridh configuration is invalid |
| `WORKSPACE_NOT_FOUND` | false | Workspace does not exist or cannot be resolved |
| `VALIDATION_FAILED` | false | A field is invalid, or the operation is unknown |
| `DEPENDENCY_UNAVAILABLE` | true | A required dependency is unavailable, or the workspace database is busy |
| `PROCESS_EXECUTION_FAILED` | false | A subprocess failed |
| `PROTOCOL_ERROR` | false | Malformed JSON, a non-object, or a wrong `protocol_version` |
| `INTERNAL_ERROR` | false | Unexpected internal failure; the raw exception text is not exposed |
| `CONFIGURATION_CONFLICT` | false | Configuration files or settings conflict |
| `STATE_CORRUPT` | false | Stored workspace state is damaged |
| `SCHEMA_UNSUPPORTED` | false | The stored schema needs a different Dridh version or a migration |
| `WORKFLOW_NOT_FOUND` | false | No workflow with that ID in this workspace |
| `INVALID_TRANSITION` | false | The change is not allowed from the current state |
| `PLAN_INVALID` | false | A plan field is invalid |
| `APPROVAL_REQUIRED` | false | A human approval is needed before execution continues |
| `DEPENDENCY_NOT_READY` | true | Listed dependencies have not completed |
| `AUTHORIZATION_DENIED` | false | This caller may not perform the operation |
| `WORKER_FAILED` | true | A model worker failed |
| `WORKER_TIMEOUT` | true | A model worker exceeded its time limit |
| `ACCEPTANCE_FAILED` | false | A deterministic acceptance check failed |
| `CANCELLED` | false | The operation was cancelled by the caller |
| `JEV_UNAVAILABLE` | true | The optional Jev service did not answer in time; the deterministic fallback was used |
| `SESSION_UNSAFE` | false | The stored agent session cannot safely be continued |

`recovery` text for each code is defined in `RECOVERY_GUIDANCE` in `errors.py`. A `DridhError` can override `retryable` or `recovery` for a specific case.

## 6. Exit-code semantics

The bridge exits `0` whenever it has written one envelope to stdout. That includes every `ok: false` response, whatever its error code. A non-zero exit means the process ended before it could write an envelope (a crash or a signal).

| Situation | Exit code |
|---|---:|
| An envelope was written, `ok: true` | 0 |
| An envelope was written, `ok: false` (any error code) | 0 |
| The process crashed or was killed before writing an envelope | non-zero (OS-dependent) |

**Consumer rule:** an adapter must never treat exit code `0` as success without also checking `ok === true`. It must reject a missing `protocol_version`, a mismatched `request_id`, a missing `ok`, and malformed JSON. The plugin maps `ok: false` to `BRIDGE_OPERATION_FAILED` and keeps the bridge's `error.code` as `bridgeCode`.

## 7. stdout and stderr

- stdout: exactly one compact JSON object followed by a newline.
- stderr: zero or more JSON diagnostic records, one per line. Each record has an event name (for example `bridge.request.succeeded` or `bridge.request.failed`), a safe message, and the `correlation_id` (the request ID), the `operation` and, for failures, the `error_code`.
- stdout must not contain logs, model output, progress text or stack traces.
- Neither stream carries request payload values or credentials.

On SIGTERM the bridge stops in-flight worker process groups and writes a `CANCELLED` envelope. A parent watchdog terminates the workers if the OpenCode process that started the bridge dies.

## 8. Versioning

`protocol_version` is an integer. Version 1 is the only version the bridge accepts. The version changes only for incompatible envelope changes. Additive operations and optional fields (such as `caller`) are added without a new version. A consumer that receives a `protocol_version` it does not support must reject the response.

## 9. Tests

| Behaviour | Test |
|---|---|
| Valid `runtime.ping` and `runtime.info` through the real bridge | `PL-01`, `PL-02` (`opencode/plugins/dridh-bridge/tests/bridge-client.test.mjs`) |
| Invalid operation, rejected by the real bridge | `PL-04` |
| Protocol version mismatch | `PL-05` |
| Malformed JSON output, missing fields, unexpected stdout | `PL-06`, `PL-07`, `PL-13` |
| `ok: false` is an operation failure | `PL-08`, `PL-18` |
| Executable unavailable, process failure, timeout | `PL-09`, `PL-10`, `PL-11` |
| Diagnostics on stderr do not corrupt stdout | `PL-12` |
| Request ID mismatch | `PL-14` |
| Credentials are redacted from errors | `PL-15` |
| Cancellation is distinct from timeout | `REL-02` |
| Caller identity is carried on every request | `PL-16`, `PL-17` |
| Operation names match the Python allow-list | `PL-20`, `PL-21`, `PL-22`, `PL-23` |
| Envelope, request validation and dispatch in Python | `dridh/tests/unit/test_protocol.py` |
| Orchestration and Ideator operations over the bridge | `dridh/tests/contract/test_orchestration_bridge_contract.py`, `dridh/tests/contract/test_ideator_bridge_contract.py` |

Run the plugin tests from `dridh/opencode/plugins/dridh-bridge` with `npm ci && npm test`, and the Python tests from `dridh` with `uv run pytest`.
