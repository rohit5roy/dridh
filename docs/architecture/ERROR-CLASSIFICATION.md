# Error Classification

Dridh reports every failure with a stable code, a safe message, redacted details, a `retryable` flag and recovery guidance. This page lists the categories each layer produces and how callers should react to them.

## Bridge transport and response categories

Defined in `dridh/opencode/plugins/dridh-bridge/src/bridge-client.ts`. These are raised by the plugin, not by the Python runtime.

| Code | Trigger | Result |
|---|---|---|
| `BRIDGE_CONFIGURATION_ERROR` | Empty executable, invalid timeout or workspace | No process started |
| `BRIDGE_PROCESS_UNAVAILABLE` | `spawn` failed with ENOENT or EACCES | No process started |
| `BRIDGE_TIMEOUT` | Bounded timer fired | Child killed |
| `BRIDGE_CANCELLED` | Caller `AbortSignal` fired | Child killed |
| `BRIDGE_PROCESS_FAILED` | Non-zero exit before an envelope was written | Exit code preserved |
| `BRIDGE_MALFORMED_RESPONSE` | stdout is not exactly one JSON object | Response rejected |
| `BRIDGE_PROTOCOL_ERROR` | Wrong version, request ID mismatch, missing `ok` or `data` | Response rejected |
| `BRIDGE_OPERATION_FAILED` | `ok: false` in a well-formed envelope | The bridge's `error.code` is kept as `bridgeCode` |

Cancellation is never reported as a timeout or a process failure. The plugin surfaces one `<CODE>: <safe message>` string and attaches the structured error; it does not add a second prefix.

## Python bridge categories

The codes the Python bridge can return, with their default `retryable` values, are listed in full in [BRIDGE-PROTOCOL.md](BRIDGE-PROTOCOL.md#5-error-codes). That table is generated from `dridh/src/dridh/common/errors.py` and is the reference; this page does not repeat it.

The bridge writes every one of these inside an `ok: false` envelope and exits `0`. A consumer must check `ok`, not the exit code.

## Agent runner categories

See [AGENT-RUNNER.md](AGENT-RUNNER.md). Provider and quota failures are classified separately from process failures and timeouts. When the runner's errors reach the bridge, `from_agent_error` in `errors.py` maps them: a timeout becomes `WORKER_TIMEOUT`, a cancellation `CANCELLED`, an unavailable OpenCode or exhausted quota `DEPENDENCY_UNAVAILABLE`, and anything else `WORKER_FAILED`.

## Retry policy

- **No automatic retries of failed calls.** The bridge, the plugin and the agent runner do not re-run a failed operation or a failed process. A caller decides whether to retry, using the `retryable` flag and `recovery` text.
- **Retryable codes** are the ones whose default is `true` in `DEFAULT_RETRYABLE`: `DEPENDENCY_UNAVAILABLE`, `DEPENDENCY_NOT_READY`, `WORKER_FAILED`, `WORKER_TIMEOUT` and `JEV_UNAVAILABLE`. The bridge's `BRIDGE_TIMEOUT` recovery text also suggests a retry when the bridge is responsive.
- **Quota and invocation errors are not retried.** `QUOTA_EXHAUSTED` (reported as `DEPENDENCY_UNAVAILABLE`) and `INVALID_INVOCATION` are not retried; retrying a quota error in a loop only spends more quota.
- **Contract repair is not a retry.** The Ideator, Technical Designer and Planner re-prompt a worker when its reply fails the stage's validator. At most three repairs are attempted; then the blocker is saved and a human must intervene. Provider, quota, timeout and other runner failures are never repaired this way. The Executor's CI repairs are bounded by `max_ci_repairs` (default 2, at most 3).
- **SQLite contention.** A writer that finds the database locked waits up to `DRIDH_SQLITE_BUSY_TIMEOUT_MS` (default 10 s, bounded to 100 ms–120 s). If the lock is still held, it raises `DEPENDENCY_UNAVAILABLE` with the message that the workspace database is busy. Nothing is changed, and the error is retryable. Contention is never reported as corruption.
