import assert from "node:assert/strict"
import { test } from "node:test"
import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const pluginRoot = resolve(fileURLToPath(new URL("..", import.meta.url)))
const { BridgeClient, BridgeError } = await import(new URL("../dist/bridge-client.js", import.meta.url))
const FAKE = join(pluginRoot, "tests", "fake-bridge.mjs")
const REAL = process.env.DRIDH_BIN ?? resolve(pluginRoot, "../../../.venv/bin/dridh")
const SECRET = "SYNTHETIC_SECRET_VALUE_9f3a7c1e"

function workspace() {
  return mkdtempSync(join(tmpdir(), "dridh-plugin-test-"))
}

function fake(mode, timeoutMs = 5000) {
  return new BridgeClient({
    executable: FAKE,
    workspaceRoot: workspace(),
    timeoutMs,
    env: { FAKE_BRIDGE_MODE: mode },
  })
}

function real() {
  return new BridgeClient({ executable: REAL, workspaceRoot: workspace(), timeoutMs: 15000 })
}

test("PL-01 real bridge runtime.ping", async () => {
  const data = await real().call("runtime.ping")
  assert.equal(data.status, "healthy")
  assert.equal(data.bridge_protocol_version, 1)
})

test("PL-02 real bridge runtime.info", async () => {
  const data = await real().call("runtime.info")
  assert.equal(data.package, "dridh")
  assert.ok(Array.isArray(data.operations))
})

test("PL-04 real bridge invalid operation", async () => {
  await assert.rejects(
    () => real().call("runtime.unknown"),
    (error) => {
      assert.ok(error instanceof BridgeError)
      assert.equal(error.code, "BRIDGE_OPERATION_FAILED")
      assert.equal(error.bridgeCode, "VALIDATION_FAILED")
      return true
    },
  )
})

test("PL-05 protocol version mismatch", async () => {
  await assert.rejects(() => fake("version").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_PROTOCOL_ERROR")
    return true
  })
})

test("PL-06 malformed JSON response", async () => {
  await assert.rejects(() => fake("malformed").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_MALFORMED_RESPONSE")
    return true
  })
})

test("PL-07 missing response fields", async () => {
  await assert.rejects(() => fake("missing").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_PROTOCOL_ERROR")
    return true
  })
})

test("PL-08 ok=false is an operation failure", async () => {
  await assert.rejects(() => fake("error").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_OPERATION_FAILED")
    assert.equal(error.bridgeCode, "VALIDATION_FAILED")
    return true
  })
})

test("PL-09 bridge executable unavailable", async () => {
  const client = new BridgeClient({
    executable: join(pluginRoot, "tests", "does-not-exist"),
    workspaceRoot: workspace(),
    timeoutMs: 2000,
  })
  await assert.rejects(() => client.call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_PROCESS_UNAVAILABLE")
    return true
  })
})

test("PL-10 bridge process failure", async () => {
  await assert.rejects(() => fake("fail").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_PROCESS_FAILED")
    assert.equal(error.exitCode, 7)
    return true
  })
})

test("PL-11 bridge timeout", async () => {
  await assert.rejects(() => fake("slow", 200).call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_TIMEOUT")
    assert.equal(error.retryable, true)
    return true
  })
})

test("PL-12 stderr diagnostics do not corrupt stdout", async () => {
  const data = await fake("stderr").call("runtime.ping")
  assert.equal(data.status, "healthy")
  assert.equal(data.mode, "stderr")
})

test("PL-13 unexpected stdout is rejected", async () => {
  await assert.rejects(() => fake("two-lines").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_MALFORMED_RESPONSE")
    return true
  })
})

test("PL-14 request ID mismatch is rejected", async () => {
  await assert.rejects(() => fake("mismatch").call("runtime.ping"), (error) => {
    assert.equal(error.code, "BRIDGE_PROTOCOL_ERROR")
    return true
  })
})

test("PL-15 credentials are redacted from bridge errors", async () => {
  await assert.rejects(() => fake("secret").call("runtime.ping"), (error) => {
    const serialised = JSON.stringify(error.toJSON())
    assert.ok(!serialised.includes(SECRET))
    assert.equal(error.code, "BRIDGE_OPERATION_FAILED")
    assert.ok(error.message.includes("[REDACTED]"))
    return true
  })
})

test("REL-02 cancellation is distinct from timeout", async () => {
  const controller = new AbortController()
  const client = fake("slow", 30000)
  const pending = client.call("runtime.ping", {}, { signal: controller.signal })
  setTimeout(() => controller.abort(), 100)
  await assert.rejects(() => pending, (error) => {
    assert.equal(error.code, "BRIDGE_CANCELLED")
    return true
  })
})

test("PL-16 caller identity is carried on every request", async () => {
  const data = await fake("echo-caller").call("workflow.status", { workflow_id: "wf-000000000000" }, {
    caller: { source: "plugin", agent: "dridh/coordinator", session_id: "ses_test" },
  })
  assert.deepEqual(data.caller, { source: "plugin", agent: "dridh/coordinator", session_id: "ses_test" })
})

test("PL-17 requests without a caller carry no caller field", async () => {
  const data = await fake("echo-caller").call("runtime.ping")
  assert.equal(data.caller, null)
})

test("PL-18 a structured failure with exit code 1 is an operation failure, not a process failure", async () => {
  await assert.rejects(() => fake("fail-envelope").call("workflow.execute"), (error) => {
    assert.ok(error instanceof BridgeError)
    assert.equal(error.code, "BRIDGE_OPERATION_FAILED")
    assert.equal(error.bridgeCode, "APPROVAL_REQUIRED")
    assert.equal(error.exitCode, 1)
    return true
  })
})

test("PL-19 a worker that ignores SIGTERM is killed after the grace period", async () => {
  const client = new BridgeClient({
    executable: FAKE,
    workspaceRoot: workspace(),
    timeoutMs: 300,
    killGraceMs: 200,
    env: { FAKE_BRIDGE_MODE: "ignore-term" },
  })
  const started = Date.now()
  await assert.rejects(() => client.call("workflow.plan"), (error) => {
    assert.equal(error.code, "BRIDGE_TIMEOUT")
    return true
  })
  assert.ok(Date.now() - started < 5000, "escalation must be bounded")
})

test("PL-20 workflow operation names match the bridge allow-list", async () => {
  const { RUNTIME_OPERATIONS, WORKFLOW_OPERATIONS, JEV_OPERATIONS } = await import(new URL("../dist/bridge-client.js", import.meta.url))
  assert.deepEqual([...RUNTIME_OPERATIONS].sort(), ["runtime.info", "runtime.ping"])
  assert.equal(WORKFLOW_OPERATIONS.length, 10)
  assert.deepEqual([...JEV_OPERATIONS], ["jev.decide"])
  assert.ok(!WORKFLOW_OPERATIONS.includes("workflow.approve"))
  assert.ok(!WORKFLOW_OPERATIONS.includes("workflow.reject"))
})

 
test("PL-21 Planner operation names match the Python allow-list", async () => {
  const { PLANNER_OPERATIONS } = await import(new URL("../dist/bridge-client.js", import.meta.url))
  assert.deepEqual([...PLANNER_OPERATIONS], [
    "planner.start",
    "planner.clarify",
    "planner.status",
    "planner.generate",
    "planner.sync",
    "planner.gate",
  ])
})

test("PL-22 Coordinator lifecycle operations match the Python allow-list and expose no completion or approval", async () => {
  const { COORDINATOR_OPERATIONS } = await import(new URL("../dist/bridge-client.js", import.meta.url))
  assert.deepEqual([...COORDINATOR_OPERATIONS], [
    "coordinator.create_feature",
    "coordinator.status",
    "coordinator.sync",
    "coordinator.transition",
    "coordinator.get_context",
    "coordinator.query",
    "coordinator.validate",
    "coordinator.list_reviews",
    "coordinator.review_checklist",
  ])
  assert.ok(![...COORDINATOR_OPERATIONS].some((operation) => /approve|reject|complete|waive|tick|check_item/.test(operation)))
})

test("PL-23 Code Reacher operation names match the Python allow-list", async () => {
  const { CODE_REACHER_OPERATIONS } = await import(new URL("../dist/bridge-client.js", import.meta.url))
  assert.deepEqual([...CODE_REACHER_OPERATIONS], [
    "code_reacher.lookup",
    "code_reacher.symbol",
    "code_reacher.dependencies",
    "code_reacher.index_status",
    "code_reacher.index",
    "code_reacher.code_for",
    "code_reacher.owners",
  ])
})
