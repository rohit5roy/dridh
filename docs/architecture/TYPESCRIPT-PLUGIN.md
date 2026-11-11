# TypeScript Plugin

**Path:** `dridh/opencode/plugins/dridh-bridge/`  
**Plugin ID:** `dridh.bridge`  
**Tool:** `dridh_bridge` (Code Mode: `tools.dridh.bridge`)  
**SDK:** `@opencode/plugin` 2.2.6 (lock-pinned for the build); runs on OpenCode 2.2.5 or later. Node 24 is required to build and test it.

The plugin is the OpenCode side of Dridh. It registers one tool that forwards calls to the Python runtime through the [bridge protocol](BRIDGE-PROTOCOL.md), and a terminal UI (TUI) that shows the state of a Dridh feature inside OpenCode.

## Files

| File | Purpose |
|---|---|
| `src/index.ts` | OpenCode plugin and typed tool registration |
| `src/bridge-client.ts` | Protocol v1 client: transport, validation, timeout, cancellation, redaction |
| `src/executable.ts` | Finds the `dridh` command for the server bridge and the TUI |
| `src/cli-result.ts` | Reads a `dridh ... --json` result for the TUI: data from stdout, the error envelope from stderr |
| `tui/index.tsx` | Terminal UI: sidebar, session panel, status line and slash commands |
| `tests/bridge-client.test.mjs` | Bridge client tests (PL-01 to PL-23, REL-02) |
| `tests/executable.test.mjs` | Executable resolution tests |
| `tests/cli-result.test.mjs` | Result and error parsing tests, including the real CLI without a workspace |
| `tests/tui-plugin.test.mjs` | TUI build, wiring and command tests |
| `tests/fake-bridge.mjs` | Deterministic fake bridge for failure injection |
| `package.json` / `package-lock.json` | Pinned SDK, TypeScript and Node types |
| `tsconfig.json` | ES2022/NodeNext build to `dist/` |

## Tool input

```json
{
  "operation": "runtime.ping",
  "payload": {},
  "timeout_ms": 30000
}
```

`operation` is restricted to `ALL_OPERATIONS` in `src/index.ts`: the runtime, Jev, workflow, Ideator, Designer, Planner, Coordinator and Code Reacher operations listed in the [bridge protocol](BRIDGE-PROTOCOL.md#4-operations). The hook operations (`hook.*`) are not in the list, so agents cannot call them.

`payload` is an object. `timeout_ms` is clamped to between 1 ms and 30 min. Without it, runtime and Jev calls use 30 s (`DRIDH_BRIDGE_TIMEOUT_MS`), and the workflow, Ideator, Designer, Planner, Coordinator and Code Reacher operations use 20 min (`DRIDH_WORKFLOW_TIMEOUT_MS`).

## Transport

```text
spawn(<dridh executable>, ["bridge"])        no shell
  -> one protocol v1 JSON request on stdin
  -> exactly one JSON object on stdout
  -> diagnostics on stderr
```

### Finding the `dridh` command

The executable is resolved by `resolveDridhExecutable()` in `src/executable.ts`, in this order:

1. `DRIDH_BIN`, when set. It is used as given.
2. The installer's `dridh-bin` record: the file beside `dist/` and `tui/` in the plugin directory, then the one under the OpenCode configuration directory (`OPENCODE_CONFIG_DIR`, or `~/.config/opencode`). A recorded value is used only if it is an absolute path to an executable file. A bare name or a path that no longer exists is skipped.
3. The first executable `dridh` on `PATH`.
4. A bare `dridh`, so that the failure names the command that is missing. The message tells the user to re-run the installer or set `DRIDH_BIN`.

OpenCode does not always inherit the `PATH` of the shell that ran the installer, which is why the record exists. The server bridge and the TUI both use this resolver.

### Validation

The client checks:

- exactly one non-empty stdout line, and valid JSON;
- `protocol_version === 1`;
- the echoed `request_id` equals the generated UUID;
- a boolean `ok`;
- `ok: false` gives a structured `error` with `code`, `message`, `details`, `retryable` and `recovery`;
- `ok: true` gives a `data` object.

Exit code `0` is not success by itself. `ok: false` with exit `0` is mapped to `BRIDGE_OPERATION_FAILED`, and the bridge's error code is kept as `bridgeCode`. The error taxonomy is in [ERROR-CLASSIFICATION.md](ERROR-CLASSIFICATION.md).

## Timeout and cancellation

- Each call has a bounded timeout (see above), overridable per call.
- A timeout kills the child and returns `BRIDGE_TIMEOUT`.
- `context.signal` is passed to the client. An abort kills the child and returns `BRIDGE_CANCELLED`.
- Cancellation is never classified as a timeout or a process failure.
- stdout and stderr are each capped at 1 MB.

## TUI

`tui/index.tsx` runs in OpenCode's terminal UI. It does not change OpenCode. It reads state with read-only `dridh ui` commands, and it changes state only through existing Dridh CLI commands.

- **Session panel.** Inside a session, the Dridh views (overview, checklist, document, execution lot) open in the session panel beside the conversation. On the home screen they open as full pages. Documents render through `glow` when it is installed, with Mermaid diagrams drawn as text.
- **Sidebar.** A Dridh section lists the stages, the documents, the open checklist, the next step and the actions. Each line can be clicked, or run by its command.
- **First run.** In a project without a Dridh workspace, `/dridh` asks whether the project is a new or an existing codebase (the person chooses; the mode is never guessed), runs `dridh init --mode ...` and opens the overview. Until then the status line reads `dridh · not set up · type /dridh to start`.
- **Status line.** A one-line status shows in the home screen footer, and in the prompt footer while a session is open: the feature and its stage, `dridh · ready` when the project is set up but has no feature yet, or an error as its message (never raw JSON).
- **Slash commands.** `/dridh` opens the overview (or the first-run setup); `/dridh-next` does the next human step; `/dridh-review` opens a document (`use cases`, `design` or `plan`); `/dridh-checklist` opens the review checklist; `/dridh-approve` approves the document under review; `/dridh-lot` opens the execution lot review. The same actions are in the command palette.
- **Human confirmations.** Approvals, lot verdicts, starting a run and completing a feature are typed confirmations. For those, the plugin suspends OpenCode's screen, as OpenCode does for `$EDITOR`, and hands the real terminal to the Dridh command. The person types the confirmation into Dridh's own prompt. The plugin never sees or types it. Agents have no tool that reaches any of this.
- **Checklist ticks** run `dridh reviews checklist`, the same record the CLI keeps.

The TUI finds `dridh` with the same resolver as the server bridge. It imports `dist/executable.js` and `dist/cli-result.js`, which the installer places beside `tui/`. The CLI writes a failure as a JSON envelope on stderr; `cli-result.ts` turns it into its message and code, so `WORKSPACE_NOT_FOUND` is recognised as a first run. `tests/tui-plugin.test.mjs` checks that every `dridh` command the TUI starts goes through that resolver, and that the TUI only shells out to the `dridh` CLI (and `glow`).

## Build and test

```bash
cd dridh/opencode/plugins/dridh-bridge
npm ci
npm test
```

`npm test` builds with `tsc` and runs the Node test suite: 43 tests in four files. `bridge-client.test.mjs` uses the real `dridh bridge` for PL-01, PL-02 and PL-04, and a fake bridge for the rest. The real bridge path can be set with `DRIDH_BIN`.

## Hooks

The hooks apply to Dridh agents (`dridh/...`) only. Ordinary OpenCode sessions are not touched.

| Hook | What it does | Bridge operation |
|---|---|---|
| session `context` | Adds a compact Dridh summary (feature, stage, next human step, blockers, the human-only rules) to the model request; cached 20 s per session | `hook.context` |
| tool `execute.before` / `execute.after` | Records each tool call's metadata in `tool_invocation`: tool, agent, session, status, duration, input key names and SHA-256 (never the input) | `hook.tool_call` |
| event stream | Records `session.execution.*` and `session.compaction.*` of Dridh sessions. Events that arrive before a session is identified are buffered and flushed | `hook.session_event` |

The hook operations are not in the `dridh_bridge` tool's allow-list.
