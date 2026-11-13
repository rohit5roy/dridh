// The TUI part (tui/index.tsx) is compiled by OpenCode itself when it loads the plugin, so a syntax error would only
// show up as a silently missing Dridh UI. Parse it here with the plugin's TypeScript compiler (tsc 7 has no JavaScript
// API, so the CLI runs). Only syntax errors (TS1xxx, JSX TS17xxx) fail the test: OpenCode supplies @opentui/solid and
// its types at load time, so type errors from their absence here are expected.
import { test } from "node:test"
import assert from "node:assert/strict"
import { existsSync, readFileSync } from "node:fs"
import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"

const source = readFileSync(new URL("../tui/index.tsx", import.meta.url), "utf8")
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"))

test("tui/index.tsx parses as TSX without syntax errors", () => {
  const tsc = fileURLToPath(new URL("../node_modules/.bin/tsc", import.meta.url))
  const file = fileURLToPath(new URL("../tui/index.tsx", import.meta.url))
  const run = spawnSync(tsc, ["--ignoreConfig", "--noEmit", "--pretty", "false", "--jsx", "preserve", "--target", "es2022", "--module", "esnext",
    "--moduleResolution", "bundler", "--skipLibCheck", "--types", "node", file], { encoding: "utf8" })
  assert.ok(run.status !== null, `tsc did not run: ${run.error}`)
  const syntax = `${run.stdout}${run.stderr}`.split("\n").filter((line) => /error TS(1\d{3}|17\d{3}):/.test(line))
  assert.deepEqual(syntax, [])
})

test("the TUI plugin uses OpenCode's JSX runtime and defines one plugin", () => {
  assert.match(source, /@jsxImportSource @opentui\/solid/)
  assert.match(source, /export default Plugin\.define\(\{\s*id: "dridh"/)
})

test("it only shells out to the dridh CLI (and glow), never to an approval it could type itself", () => {
  // Approvals are typed by the person in Dridh's own terminal prompt; the plugin must not pass a confirmation.
  assert.doesNotMatch(source, /--confirmation/)
  assert.doesNotMatch(source, /APPROVE (PLAN|[0-9a-f])/)
})


test("the TUI finds dridh with the same resolver as the server bridge, shipped beside it in dist/", () => {
  assert.ok(source.includes('from "../dist/executable.js"'), "imports the shared resolver")
  assert.ok(existsSync(new URL("../dist/executable.js", import.meta.url)), "npm run build produces dist/executable.js")
  assert.ok(source.includes("const bin = () => resolveDridhExecutable()"))
  assert.ok(!source.includes("process.env.DRIDH_BIN"), "no second, diverging lookup")
})

test("every dridh command the TUI starts goes through the resolver", () => {
  const starts = [...source.matchAll(/\b(execFile|spawn)\(([^,]+),/g)].map((match) => `${match[1]}(${match[2].trim()}`)
  assert.deepEqual(starts.sort(), ["execFile(executable", "spawn(\"sh\"", "spawn(glowBin()", "spawn(bin()"].sort())
  assert.ok(source.includes("const executable = bin()"))
  assert.ok(source.includes('["-c", script, bin(), ...argv]'), "the confirmation terminal runs the resolved command")
  assert.ok(source.includes("missingExecutableMessage(executable)"), "a missing command gets a message that says how to fix it")
})

test("a project without a workspace is a first run: /dridh offers setup instead of showing an error", () => {
  assert.ok(source.includes('from "../dist/cli-result.js"'), "results are read by the shared parser")
  assert.ok(existsSync(new URL("../dist/cli-result.js", import.meta.url)), "npm run build produces dist/cli-result.js")
  assert.ok(source.includes("readCliResult(String(stdout"), "stdout and stderr are both read")
  assert.ok(!source.includes("JSON.parse(stdout)"), "no second, stdout-only parser")
  assert.ok(source.includes("=== WORKSPACE_NOT_FOUND"), "a missing workspace is recognised by its code")
  assert.match(source, /slash: \{ name: "dridh" \},\s*run: \(\) => void overview\(\)/, "/dridh goes through the setup check")
  assert.match(source, /if \(unset\(\)\) void setUp\(\)/)
  assert.ok(source.includes('dridhJson(["init", "--mode", mode], root())'), "setup runs dridh init with the chosen mode")
  assert.ok(source.includes('value: "new"') && source.includes('value: "existing"'), "the person chooses; the mode is never guessed")
  assert.ok(source.includes("dridh · type /dridh to set up"), "the status line explains how to start")
})

test("the TUI follows OpenCode to another folder (/cd) instead of keeping the one it started in", () => {
  assert.ok(source.includes("const root = () => ctx.location?.directory ?? process.cwd()"), "the folder is read every time")
  assert.doesNotMatch(source, /const root = ctx\.location/, "never kept from startup")
  assert.doesNotMatch(source, /[(,] *root *[,)]/, "every command gets the current folder, root(), not a saved value")
  assert.match(source, /if \(folder !== shownRoot\) \{[^}]*setState\(undefined\)[^}]*setUnset\(false\)/s, "a new folder clears what was shown")
  assert.ok(source.includes("if (folder !== root()) return"), "a result for a folder OpenCode has left is dropped")
  assert.match(source, /async function overview\(\)[^{]*\{\s*await refresh\(\)/, "/dridh reads the current folder before deciding")
})

test("setting up an existing codebase indexes it in the background, with progress in the status line", () => {
  assert.match(source, /if \(mode === "existing"\) startIndex\(\)/, "an existing codebase is indexed right after setup")
  assert.match(source, /const args = \["index", "--progress", "--json", "--project", folder,/, "dridh index runs beside OpenCode, not in the terminal")
  assert.match(source, /spawn\(bin\(\), args, \{ cwd: folder/)
  assert.ok(!/onTerminal\([^)]*"index"/.test(source), "indexing no longer takes over the terminal")
  assert.match(source, /event\?\.progress === "files" \|\| event\?\.progress === "embeddings"/, "progress lines feed the status line")
  assert.ok(source.includes('const what = job.phase === "files" ? "indexing" : "embedding"') && source.includes("${what}${count}"), "the status line shows how far indexing is")
  assert.match(source, /slash: \{ name: "dridh-index" \},\s*run: \(\) => startIndex\(\)/, "/dridh-index starts it by hand")
})

test("vectors from an older search model or text are rebuilt in the background, once per folder per session", () => {
  assert.match(source, /next\?\.code_index\?\.vectors_stale && !indexing\(\) && !reindexed\.has\(folder\)/, "only when stale, idle and not tried yet")
  assert.match(source, /reindexed\.add\(folder\)[\s\S]{0,300}startIndex\("upgrade"\)/, "remembered before starting, so a failure is not retried in a loop")
  assert.match(source, /Code search was improved\. Re-indexing the code in the background\./, "the user is told why indexing started")
  assert.match(source, /why === "upgrade" \? `Code search is up to date/, "and when it is done")
})

test("code changed outside Dridh is re-indexed in the background once the files hold still", () => {
  assert.match(source, /else if \(next\?\.code_index\?\.code_changed\)/, "after the upgrade check")
  assert.match(source, /if \(seen === fingerprint && !indexing\(\) && failedFingerprint\.get\(folder\) !== fingerprint\) startIndex\("changes", fingerprint\)/,
    "only when the fingerprint is the same as at the previous refresh, nothing is indexing and that fingerprint did not already fail")
  assert.match(source, /why === "changes" \? \["--if-changed"\] : \[\]/, "it updates the folder the index was built from, and only when something changed")
  assert.match(source, /if \(why === "changes"\) return/, "a routine update shows the spinner, not a toast")
  assert.match(source, /if \(why === "changes"\) failedFingerprint\.set\(folder, fingerprint\)/, "a failure is not retried in a loop")
})

test("deviations from the approved use cases are approved by a person on the real terminal", () => {
  assert.match(source, /if \(a\.kind === "deviations"\) \{[\s\S]{0,500}onTerminal\([\s\S]{0,300}\["ledger", "approve", id, "--reviewer", name\]\)/)
  assert.match(source, /if \(a\.kind === "delivery_blocked"\) return ctx\.ui\.toast\.show/)
})

test("before the first feature the TUI shows the setup steps and one next action", () => {
  assert.ok(source.includes("Setup · step"), "the overview shows which step setup is at")
  assert.match(source, /`dridh · \$\{step\} · /, "the status line shows the setup step")
  assert.ok(source.includes('const START_FEATURE = "Select Dridh/Coordinator (Shift+Tab) and describe a feature."'))
  assert.ok(source.includes('agent === "dridh/coordinator" ? "describe a feature to start"'), "inside a Coordinator session it does not ask for Shift+Tab")
  assert.ok(!source.includes("Press Shift+Tab for the Dridh Coordinator and tell it what you want to build"), "the long message is gone")
})

test("a spinner shows in the status line while Dridh is working, and only then", () => {
  assert.ok(source.includes('const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]'))
  assert.match(source, /if \(busy && !spinTimer\) spinTimer = setInterval/, "the timer runs only while busy")
  assert.match(source, /if \(!busy && spinTimer\) \{ clearInterval\(spinTimer\)/, "and stops when idle")
  assert.ok(source.includes('ctx.data.on("session.tool.called"') && source.includes('startsWith("dridh_")'), "agents' Dridh tool calls are shown")
  assert.ok(source.includes('busyWith("setting up"'), "panel actions are shown")
  assert.ok(source.includes('if (tool === "dridh_code")') && source.includes('"searching code"'), "the label says what Dridh is doing")
  assert.ok(source.includes("const WorkingLine = () =>") && (source.match(/<WorkingLine \/>/g) ?? []).length === 2,
    "the panel shows it too, below the setup steps and below the feature overview")
  assert.ok(source.includes('indexing the code · ${job.phase === "files" ? "reading files" : "embedding"} ${Math.floor((100 * job.done) / job.total)}%'), "indexing shows how far it is")
  assert.match(source, /if \(spinTimer\) clearInterval\(spinTimer\)\s*\n\s*for \(const stop of stopListening\) stop\(\)/, "unloading stops the timer and the listeners")
})
