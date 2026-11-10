#!/usr/bin/env node
import { readFileSync } from "node:fs"

const raw = readFileSync(0, "utf8")
let request = {}
try {
  request = JSON.parse(raw)
} catch {
  process.stdout.write("not json\n")
  process.exit(0)
}

const mode = process.env.FAKE_BRIDGE_MODE ?? "success"
const requestId = request.request_id ?? "missing"
const base = { protocol_version: 1, request_id: requestId }

function ok(data) {
  process.stdout.write(JSON.stringify({ ...base, ok: true, data }) + "\n")
}

function fail(code, message, details = []) {
  process.stdout.write(
    JSON.stringify({
      ...base,
      ok: false,
      error: { code, message, details, retryable: false, recovery: "fake recovery" },
    }) + "\n",
  )
}

switch (mode) {
  case "success":
    ok({ status: "healthy", echo: request.payload ?? {} })
    break
  case "info":
    ok({ package: "dridh", package_version: "0.1.0", operations: ["runtime.ping", "runtime.info"] })
    break
  case "error":
    fail("VALIDATION_FAILED", "Unknown bridge operation.", ["operation: runtime.unknown"])
    break
  case "version":
    process.stdout.write(JSON.stringify({ ...base, protocol_version: 2, ok: true, data: {} }) + "\n")
    break
  case "malformed":
    process.stdout.write("this is not json\n")
    break
  case "missing":
    process.stdout.write("{}\n")
    break
  case "mismatch":
    process.stdout.write(
      JSON.stringify({ ...base, request_id: "different-request-id", ok: true, data: {} }) + "\n",
    )
    break
  case "fail":
    process.stderr.write("fake bridge failed\n")
    process.exit(7)
  case "slow":
    setTimeout(() => ok({ status: "late" }), 10000)
    break
  case "stderr":
    process.stderr.write("DIAGNOSTIC_ONLY: this must not corrupt stdout\n")
    ok({ status: "healthy", mode: "stderr" })
    break
  case "secret":
    fail("VALIDATION_FAILED", "openai_api_key=SYNTHETIC_SECRET_VALUE_9f3a7c1e", [
      "aws_secret_access_key=SYNTHETIC_SECRET_VALUE_9f3a7c1e",
    ])
    break
  case "two-lines":
    process.stdout.write(JSON.stringify({ ...base, ok: true, data: {} }) + "\n")
    process.stdout.write(JSON.stringify({ ...base, ok: true, data: {} }) + "\n")
    break
  case "echo-caller":
    ok({ caller: request.caller ?? null })
    break
  case "fail-envelope":
    fail("APPROVAL_REQUIRED", "The plan has not been approved, so no task can run.", ["state: AWAITING_APPROVAL"])
    process.exit(1)
  case "ignore-term":
    process.on("SIGTERM", () => {})
    setInterval(() => {}, 1000)
    break
  default:
    fail("UNKNOWN", `unknown fake mode ${mode}`)
}
