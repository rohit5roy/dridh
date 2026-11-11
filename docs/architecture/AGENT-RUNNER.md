# OpenCode Agent Runner

**Module:** `dridh/src/dridh/integrations/opencode.py`  
**Entry point:** `OpenCodeRunner`  
**Scope:** one-shot invocation and resume only

## API

```python
from dridh.integrations import OpenCodeRunner

runner = OpenCodeRunner(
    executable="opencode",  # or the path in DRIDH_OPENCODE_BIN
    workspace_root="/workspace",
    config_dir="/disposable/opencode/config",          # optional
    timeout=120,
)
result = await runner.run(
    agent="dridh/coordinator",
    prompt="...",
    session_id=None,                                  # or ses_...
    model=None,
    title=None,
)
```

`AgentRunResult` contains `agent`, `prompt`, `session_id`, `text`, `exit_code`,
`duration_seconds`, `events`, `stderr` and `resumed`.

The runner invokes:

```text
opencode --standalone run --agent <agent> --format json [--session <id>] <prompt>
```

It always selects the agent explicitly. It does not use `--continue`, a default agent, or native subagents. The JSONL event stream is the supported output surface used for session extraction.

The runner sets `PWD` to the workspace in the child environment. OpenCode takes its working directory from `$PWD`, not from the process's real working directory, so an inherited `PWD` (the directory Dridh was started from) would send edits outside the intended workspace.

## Failure categories

| Code | Meaning |
|---|---|
| `INVALID_INVOCATION` | Empty agent/prompt, bad session ID, missing workspace/config, bad timeout |
| `OPENCODE_UNAVAILABLE` | Executable missing or not executable |
| `PROCESS_FAILURE` | Non-zero exit without a recognized provider/quota marker |
| `TIMEOUT` | Bounded `run_process` timeout |
| `SESSION_EXTRACTION_FAILED` | No valid `ses_...` ID in JSON output |
| `SESSION_RESUME_FAILED` | Returned session ID differs from the requested one |
| `MALFORMED_OUTPUT` | No parseable events or no assistant text |
| `PROVIDER_FAILURE` | Recognized provider/model/API failure |
| `QUOTA_EXHAUSTED` | Free usage limit, quota exhaustion, rate limit or 429 text |
| `CANCELLED` | Caller cancelled the asyncio task |

Every failure carries safe `details`, `retryable`, `recovery` and an optional
`session_id`. Credentials are redacted before details are stored or returned.
How these reach the bridge is described in [ERROR-CLASSIFICATION.md](ERROR-CLASSIFICATION.md).

## Timeout and cancellation

- `run_process` uses `asyncio.wait_for` and kills the process group on timeout.
- `ProcessExecutionError.timed_out` is the canonical timeout signal.
- Cancellation kills the process group and is converted to
  `AgentRunError(CANCELLED)`; callers can distinguish it from `TIMEOUT`.
- No automatic retries are implemented. A caller may retry explicitly with a
  bounded policy; quota exhaustion must not be retried in a tight loop.

## Tests

| File | Tests | Scope |
|---|---:|---|
| `dridh/tests/integration/test_opencode_runner.py` | 21 | Fake OpenCode executable: invocation, resume, invalid session, empty agent, missing session, mismatch, malformed output, no text, provider failure, quota, timeout, cancellation, missing executable, invalid workspace, and the contract that the agent is always explicit and no continue or default-agent option is added |
| `dridh/tests/unit/test_opencode_runner_pwd.py` | 1 | The child process gets `PWD` set to the workspace |
| `dridh/tests/integration/test_opencode_runner_live.py` | 4 | Real OpenCode: invocation and extraction, session resume, a Jev decision, and an Ideator pipeline |

The live tests are skipped unless these variables are set:

| Variable | Purpose |
|---|---|
| `DRIDH_REAL_OPENCODE=1` | Enables the live tests |
| `DRIDH_OPENCODE_BIN` | Path to the OpenCode CLI |
| `DRIDH_REAL_OPENCODE_CONFIG` | A disposable OpenCode configuration directory |
| `DRIDH_REAL_OPENCODE_WORKSPACE` | An existing disposable workspace directory |
| `OPENROUTER_API_KEY` | The model provider credential the live tests use |

The live tests call a real model and can spend quota. Run them only against a disposable configuration.
