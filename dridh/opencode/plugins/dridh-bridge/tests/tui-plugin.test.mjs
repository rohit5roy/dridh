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
  assert.deepEqual(starts.sort(), ["execFile(executable", "spawn(\"sh\"", "spawn(glowBin()"].sort())
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
  assert.ok(source.includes("dridh · not set up · type /dridh to start"), "the status line explains how to start")
})

test("the TUI follows OpenCode to another folder (/cd) instead of keeping the one it started in", () => {
  assert.ok(source.includes("const root = () => ctx.location?.directory ?? process.cwd()"), "the folder is read every time")
  assert.doesNotMatch(source, /const root = ctx\.location/, "never kept from startup")
  assert.doesNotMatch(source, /[(,] *root *[,)]/, "every command gets the current folder, root(), not a saved value")
  assert.match(source, /if \(folder !== shownRoot\) \{[^}]*setState\(undefined\)[^}]*setUnset\(false\)/s, "a new folder clears what was shown")
  assert.ok(source.includes("if (folder !== root()) return"), "a result for a folder OpenCode has left is dropped")
  assert.match(source, /async function overview\(\)[^{]*\{\s*await refresh\(\)/, "/dridh reads the current folder before deciding")
})

test("setting up an existing codebase offers to index it, with the progress in the terminal", () => {
  assert.match(source, /if \(mode === "existing"\) \{[\s\S]*?onTerminal\("Index the codebase", \[[\s\S]*?\], \["index"\]/)
  assert.ok(source.includes("Dridh shows the indexing progress in this terminal"))
  assert.ok(!/onTerminal\([^)]*--confirmation/.test(source), "indexing needs no confirmation")
})
