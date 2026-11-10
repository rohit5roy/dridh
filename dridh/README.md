# dridh

The Python runtime of [Dridh](https://github.com/rohit5roy/dridh), a document-driven feature harness for
OpenCode: the `dridh` command, the versioned bridge the OpenCode plugin calls, the SQLite workspace state, the stage
services (Ideator, Technical Designer, Planner, Executor, delivery), Code Reacher, and the OpenCode plugin and agent
profiles it installs.

Install and use it as described in the [repository README](https://github.com/rohit5roy/dridh#readme); work on it
as described in [CONTRIBUTING.md](https://github.com/rohit5roy/dridh/blob/main/CONTRIBUTING.md).

## Layout

| Path | What it holds |
|---|---|
| `src/dridh/cli` | the `dridh` command |
| `src/dridh/common` | errors, logging, redaction, the bridge protocol |
| `src/dridh/coordinator`, `orchestration` | the feature lifecycle and the bridge operations agents may call |
| `src/dridh/ideator`, `designer`, `planner`, `executor`, `delivery` | the stage services |
| `src/dridh/document_contracts` | the packaged document standards and their validators |
| `src/dridh/code_reacher` | code indexing, search and story-to-code mapping |
| `src/dridh/state`, `workspace` | SQLite state, migrations, workspace layout and worktrees |
| `src/dridh/ui` | read-only views for the OpenCode TUI plugin, Mermaid-to-text |
| `src/dridh/install` | `dridh install`, `upgrade`, `uninstall` |
| `opencode/plugins/dridh-bridge` | the OpenCode plugin (server part and TUI part) |
| `opencode/agents/dridh` | the Coordinator, stage and worker agent profiles |
| `tests` | unit, integration, contract, security and Docker E2E tests |

The version is set only in `src/dridh/__init__.py`.
