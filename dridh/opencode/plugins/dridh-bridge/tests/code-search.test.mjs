// Every Dridh agent searches code with Code Reacher first (dridh_code), then grep or glob. The plugin's setup runs here
// against a stand-in OpenCode context; the bridge is the fake bridge in echo mode.
import { test } from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const pluginRoot = resolve(fileURLToPath(new URL("..", import.meta.url)))
const plugin = await import(new URL("../dist/index.js", import.meta.url))
const { CODE_SEARCH_OPERATIONS, TEXT_SEARCH_TOOLS, SEARCH_FIRST_MESSAGE, isCodeSearch } = plugin

async function started() {
  process.env.DRIDH_BIN = join(pluginRoot, "tests", "fake-bridge.mjs")
  process.env.FAKE_BRIDGE_MODE = "echo"
  const tools = new Map()
  const toolHooks = new Map()
  const ctx = {
    location: { directory: mkdtempSync(join(tmpdir(), "dridh-code-search-")) },
    tool: {
      transform: async (build) => {
        build({ namespace: () => {}, add: (tool) => tools.set(tool.name, tool) })
        return { dispose: async () => {} }
      },
      hook: async (name, fn) => { toolHooks.set(name, fn); return { dispose: async () => {} } },
    },
    session: { hook: async () => ({ dispose: async () => {} }) },
    event: { subscribe: () => ({ async *[Symbol.asyncIterator]() {} }) },
  }
  const stop = await plugin.default.setup(ctx)
  return { tools, before: toolHooks.get("execute.before"), stop }
}

const call = (tool, agent = "dridh/executor", sessionID = "ses_a", input = {}) => ({ tool, agent, sessionID, id: `c_${Math.random()}`, messageID: "m", input })

test("dridh_code offers Code Reacher's read-only operations, never indexing", async () => {
  const { tools, stop } = await started()
  try {
    const code = tools.get("code")
    assert.ok(code, "the dridh_code tool is registered")
    assert.deepEqual([...CODE_SEARCH_OPERATIONS].sort(), ["code_for", "dependencies", "index_status", "lookup", "owners", "symbol"])
    assert.deepEqual(code.input.properties.operation.enum, CODE_SEARCH_OPERATIONS)
    assert.ok(!CODE_SEARCH_OPERATIONS.includes("index"), "indexing stays with the Coordinator, through the bridge")
    assert.match(code.description, /FIRST, before grep or glob/)
  } finally { await stop() }
})

test("a lookup reaches the bridge as code_reacher.lookup, as the calling agent", async () => {
  const { tools, stop } = await started()
  try {
    const result = await tools.get("code").execute({ operation: "lookup", query: "validate an email address", limit: 5 },
      { agent: "dridh/planner", sessionID: "ses_p", signal: new AbortController().signal })
    const data = JSON.parse(result.content[0].text)
    assert.equal(data.operation, "code_reacher.lookup")
    assert.deepEqual(data.payload, { query: "validate an email address", limit: 5 })
    assert.deepEqual(data.caller, { source: "plugin", agent: "dridh/planner", session_id: "ses_p" })
    await assert.rejects(tools.get("code").execute({ operation: "index" }, { agent: "dridh/planner", sessionID: "ses_p" }), /Unknown code search operation/)
  } finally { await stop() }
})

test("a Dridh agent's first grep or glob is refused until it has searched with dridh_code", async () => {
  const { before, stop } = await started()
  try {
    for (const tool of TEXT_SEARCH_TOOLS) assert.throws(() => before(call(tool, "dridh/executor", "ses_1")), (e) => e.message === SEARCH_FIRST_MESSAGE)
    before(call("read", "dridh/executor", "ses_1"))  // reading a known file is not searching
    before(call("dridh_code", "dridh/executor", "ses_1", { operation: "lookup", query: "x" }))
    for (const tool of TEXT_SEARCH_TOOLS) before(call(tool, "dridh/executor", "ses_1"))  // allowed from now on
    assert.throws(() => before(call("grep", "dridh/executor", "ses_2")), /dridh_code/, "each session searches first")
  } finally { await stop() }
})

test("a refused grep is recorded as refused, with the input's keys only", async () => {
  const { existsSync, readFileSync } = await import("node:fs")
  const log = join(mkdtempSync(join(tmpdir(), "dridh-bridge-log-")), "requests.jsonl")
  process.env.FAKE_BRIDGE_LOG = log
  const { before, stop } = await started()
  try {
    assert.throws(() => before(call("grep", "dridh/executor", "ses_r", { pattern: "secret-value" })))
    for (let i = 0; i < 50 && !existsSync(log); i++) await new Promise((r) => setTimeout(r, 100))
    const recorded = readFileSync(log, "utf8").trim().split("\n").map((line) => JSON.parse(line)).find((r) => r.operation === "hook.tool_call")
    assert.ok(recorded, "the refusal reaches the bridge")
    assert.equal(recorded.payload.tool, "grep")
    assert.equal(recorded.payload.refused, true)
    assert.equal(recorded.payload.status, "error")
    assert.deepEqual(recorded.payload.input_keys, ["pattern"])
    assert.ok(!JSON.stringify(recorded).includes("secret-value"), "the input itself is never sent")
  } finally { delete process.env.FAKE_BRIDGE_LOG; await stop() }
})

test("a code_reacher operation through the bridge also counts, and other agents are never affected", async () => {
  const { before, stop } = await started()
  try {
    before(call("dridh_bridge", "dridh/coordinator", "ses_c", { operation: "code_reacher.lookup", payload: { query: "x" } }))
    before(call("grep", "dridh/coordinator", "ses_c"))
    assert.throws(() => before(call("grep", "dridh/coordinator", "ses_d")))
    before(call("dridh_bridge", "dridh/coordinator", "ses_d", { operation: "coordinator.status" }))
    assert.throws(() => before(call("grep", "dridh/coordinator", "ses_d")), /dridh_code/, "only a code search counts")
    before(call("grep", "build", "ses_e"))  // OpenCode's own agents are untouched
    before(call("glob", "plan", "ses_e"))
    assert.equal(isCodeSearch("dridh_code", {}), true)
    assert.equal(isCodeSearch("dridh_bridge", { operation: "code_reacher.symbol" }), true)
    assert.equal(isCodeSearch("dridh_bridge", { operation: "ideator.status" }), false)
  } finally { await stop() }
})

test("every Dridh agent profile grants dridh_code and says to search with it first", async () => {
  const { readdirSync, readFileSync } = await import("node:fs")
  const agents = resolve(pluginRoot, "..", "..", "agents", "dridh")
  const files = [...readdirSync(agents).filter((f) => f.endsWith(".md")), ...readdirSync(join(agents, "workers")).map((f) => `workers/${f}`)]
  assert.equal(files.length, 17)
  for (const file of files) {
    const text = readFileSync(join(agents, file), "utf8")
    assert.match(text, /- action: dridh_code\n {4}resource: "\*"\n {4}effect: allow/, `${file} grants dridh_code`)
    assert.match(text, /## Finding code/, `${file} explains how to find code`)
    if (/- action: grep/.test(text)) assert.match(text, /Use the `dridh_code` tool FIRST/, `${file} searches with dridh_code before grep`)
  }
})
