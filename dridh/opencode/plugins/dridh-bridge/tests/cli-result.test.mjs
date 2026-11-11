// How the terminal UI reads `dridh ... --json` results (src/cli-result.ts). Includes the real CLI output for a project
// without a workspace, which the TUI used to show as raw JSON.
import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { existsSync, mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const { readCliResult, DridhCommandError, WORKSPACE_NOT_FOUND } = await import(new URL("../dist/cli-result.js", import.meta.url))

const pluginRoot = resolve(fileURLToPath(new URL("..", import.meta.url)))
const REAL = process.env.DRIDH_BIN ?? resolve(pluginRoot, "../../../.venv/bin/dridh")

const NO_WORKSPACE = JSON.stringify({ ok: false, error: { code: "WORKSPACE_NOT_FOUND", message: "The project has no Dridh workspace.",
  details: ["/p/.opencode/dridh"], retryable: false, recovery: "Run dridh init in the project root first." } })

function rejects(fn) {
  try { fn() } catch (error) { return error }
  assert.fail("expected an error")
}

test("a JSON result on stdout is returned as data", () => {
  assert.deepEqual(readCliResult('{\n  "feature": null,\n  "mode": "new"\n}\n', "", false), { feature: null, mode: "new" })
  assert.deepEqual(readCliResult(JSON.stringify({ ok: true, data: { a: 1 } }), "", false), { a: 1 })
})

test("an error envelope on stderr becomes its message and code, never raw JSON", () => {
  const error = rejects(() => readCliResult("", NO_WORKSPACE + "\n", true, "Command failed: dridh ui state"))
  assert.ok(error instanceof DridhCommandError)
  assert.equal(error.code, WORKSPACE_NOT_FOUND)
  assert.equal(error.message, "The project has no Dridh workspace.")
  assert.equal(error.recovery, "Run dridh init in the project root first.")
  assert.doesNotMatch(error.message, /[{}]/)
})

test("an error envelope after diagnostic lines on stderr is still found", () => {
  const stderr = '{"event":"bridge.request.failed","level":"error"}\nsome warning\n' + NO_WORKSPACE + "\n"
  assert.equal(rejects(() => readCliResult("", stderr, true)).code, WORKSPACE_NOT_FOUND)
})

test("an error envelope on stdout is reported too", () => {
  assert.equal(rejects(() => readCliResult(NO_WORKSPACE, "", false)).code, WORKSPACE_NOT_FOUND)
})

test("a failure without an envelope shows the end of stderr, or the process error", () => {
  assert.equal(rejects(() => readCliResult("", "Traceback ...\nRuntimeError: boom\n", true)).message, "Traceback ...\nRuntimeError: boom")
  assert.equal(rejects(() => readCliResult("", "", true, "spawn timeout")).message, "spawn timeout")
  assert.equal(rejects(() => readCliResult("", "x".repeat(1000), true)).message.length, 400)
  assert.equal(rejects(() => readCliResult("not json", "", false)).message, "dridh returned invalid JSON")
  assert.equal(rejects(() => readCliResult('{"ok": true}', "", true)).code, undefined, "a non-zero exit is never success")
})

test("against the real CLI: a project without a workspace is WORKSPACE_NOT_FOUND, and init then works", (t) => {
  if (!existsSync(REAL)) return t.skip(`no dridh at ${REAL}; set DRIDH_BIN`)
  const project = mkdtempSync(join(tmpdir(), "dridh-cli-result-"))
  t.after(() => rmSync(project, { recursive: true, force: true }))
  const env = { ...process.env, DRIDH_OUTPUT: "json" }
  const run = (args) => spawnSync(REAL, [...args, "--json", "--project", project], { cwd: project, env, encoding: "utf8", timeout: 60_000 })

  const before = run(["ui", "state"])
  const error = rejects(() => readCliResult(before.stdout, before.stderr, before.status !== 0))
  assert.equal(error.code, WORKSPACE_NOT_FOUND)

  const init = run(["init", "--mode", "new"])
  assert.equal(readCliResult(init.stdout, init.stderr, init.status !== 0).workspace.mode, "new")
  const after = run(["ui", "state"])
  const state = readCliResult(after.stdout, after.stderr, after.status !== 0)
  assert.equal(state.feature, null)
  assert.equal(state.human_action.kind, "create")
  assert.ok(existsSync(join(project, ".opencode", "dridh")), "the workspace exists")
})
