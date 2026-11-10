/**
 * Production Dridh bridge client for the OpenCode V2 TypeScript plugin.
 *
 * The client owns the protocol-v1 envelope, process transport, timeout,
 * cancellation and response/error validation. It never treats exit code 0 as
 * success without checking `ok === true`.
 */
import { spawn } from "node:child_process"
import { existsSync, statSync } from "node:fs"
import { randomUUID } from "node:crypto"

export const RUNTIME_OPERATIONS = ["runtime.info", "runtime.ping"] as const

export const JEV_OPERATIONS = ["jev.decide"] as const

export const IDEATOR_OPERATIONS = [
  "ideator.start",
  "ideator.answer",
  "ideator.status",
  "ideator.generate",
  "ideator.parity",
  "ideator.gate",
] as const

export const DESIGNER_OPERATIONS = [
  "designer.start",
  "designer.answer",
  "designer.status",
  "designer.generate",
  "designer.parity",
  "designer.gate",
] as const

export const PLANNER_OPERATIONS = ["planner.start", "planner.clarify", "planner.status", "planner.generate", "planner.sync", "planner.gate"] as const

// Feature lifecycle. No operation completes a feature or approves anything: those are human-only CLI commands.
export const CODE_REACHER_OPERATIONS = [
  "code_reacher.lookup",
  "code_reacher.symbol",
  "code_reacher.dependencies",
  "code_reacher.index_status",
  "code_reacher.index",
  "code_reacher.code_for",
  "code_reacher.owners",
] as const

export const COORDINATOR_OPERATIONS = ["coordinator.create_feature", "coordinator.status", "coordinator.sync", "coordinator.transition", "coordinator.get_context", "coordinator.query", "coordinator.validate", "coordinator.list_reviews", "coordinator.review_checklist"] as const


export const WORKFLOW_OPERATIONS = [
  "workspace.init",
  "workspace.status",
  "workflow.create",
  "workflow.plan",
  "workflow.next",
  "workflow.execute",
  "workflow.status",
  "workflow.report",
  "workflow.cancel",
  "workflow.verify_ledger",
] as const

// Called by the plugin's own OpenCode hooks (plan section 7.5), never offered to agents through the dridh.bridge tool.
export const HOOK_OPERATIONS = ["hook.context", "hook.tool_call", "hook.session_event"] as const

export type BridgeOperation = (typeof HOOK_OPERATIONS)[number] | (typeof RUNTIME_OPERATIONS)[number] | (typeof JEV_OPERATIONS)[number] | (typeof IDEATOR_OPERATIONS)[number] | (typeof DESIGNER_OPERATIONS)[number] | (typeof PLANNER_OPERATIONS)[number] | (typeof COORDINATOR_OPERATIONS)[number] | (typeof CODE_REACHER_OPERATIONS)[number] | (typeof WORKFLOW_OPERATIONS)[number]

export interface BridgeCaller {
  source: "plugin"
  agent?: string
  session_id?: string
}

export interface BridgeRequest {
  protocol_version: 1
  request_id: string
  workspace_root: string
  operation: string
  payload: Record<string, unknown>
  caller?: BridgeCaller
}

export interface BridgeSuccess {
  protocol_version: 1
  request_id: string
  ok: true
  data: Record<string, unknown>
}

export interface BridgeFailure {
  protocol_version: 1
  request_id: string
  ok: false
  error: {
    code: string
    message: string
    details: string[]
    retryable: boolean
    recovery: string
  }
}

export type BridgeResponse = BridgeSuccess | BridgeFailure

export type BridgeErrorCode =
  | "BRIDGE_CONFIGURATION_ERROR"
  | "BRIDGE_PROCESS_UNAVAILABLE"
  | "BRIDGE_PROCESS_FAILED"
  | "BRIDGE_TIMEOUT"
  | "BRIDGE_CANCELLED"
  | "BRIDGE_MALFORMED_RESPONSE"
  | "BRIDGE_PROTOCOL_ERROR"
  | "BRIDGE_OPERATION_FAILED"

export interface BridgeErrorOptions {
  details?: string[]
  retryable?: boolean
  recovery?: string
  bridgeCode?: string
  exitCode?: number | null
  requestId?: string
  cause?: unknown
}

const RECOVERY: Record<BridgeErrorCode, string> = {
  BRIDGE_CONFIGURATION_ERROR: "Check DRIDH_BIN, workspace_root and the bridge installation.",
  BRIDGE_PROCESS_UNAVAILABLE: "Install Dridh or set DRIDH_BIN to the dridh executable.",
  BRIDGE_PROCESS_FAILED: "Inspect the redacted bridge stderr and process exit code.",
  BRIDGE_TIMEOUT: "Increase the bounded timeout or retry when the bridge is responsive.",
  BRIDGE_CANCELLED: "No action required; the caller cancelled the operation.",
  BRIDGE_MALFORMED_RESPONSE: "Verify the bridge version and stdout protocol.",
  BRIDGE_PROTOCOL_ERROR: "Verify protocol_version and request_id; restart the bridge.",
  BRIDGE_OPERATION_FAILED: "Inspect the bridge error code, details and recovery guidance.",
}

const SENSITIVE_KEY =
  /(?:api[_-]?key|api[_-]?secret|access[_-]?key(?:[_-]?id)?|secret[_-]?key|private[_-]?key|session[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|password|passwd|token|authorization|credential(?:s)?|cookie|secret)/i

export function redactText(value: string): string {
  return value
    .replace(/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g, "[REDACTED]")
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, "Bearer [REDACTED]")
    .replace(/\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}/gi, "[REDACTED]")
    .replace(/\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/g, "[REDACTED]")
    .replace(/\bgithub_pat_[A-Za-z0-9_]{20,}/g, "[REDACTED]")
    .replace(/\bxox[baprs]-[A-Za-z0-9-]{10,}/g, "[REDACTED]")
    .replace(/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED]")
    .replace(/(--?[A-Za-z0-9_.-]*(?:key|token|secret|password|credential)[A-Za-z0-9_.-]*\s+)([^\s]+)/gi, "$1[REDACTED]")
    .replace(
      /(["']?[A-Za-z0-9_.-]*(?:api[_-]?key|api[_-]?secret|access[_-]?key(?:[_-]?id)?|secret[_-]?key|private[_-]?key|session[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|password|passwd|token|authorization|credential(?:s)?|cookie|secret)["']?\s*[:=]\s*)(["']?)([^\s,;"']+)\2/gi,
      "$1$2[REDACTED]$2",
    )
    .replace(/(https?:\/\/)([^/\s:@]+):([^/\s@]+)@/gi, "$1[REDACTED]@")
}

export class BridgeError extends Error {
  readonly code: BridgeErrorCode
  readonly details: string[]
  readonly retryable: boolean
  readonly recovery: string
  readonly bridgeCode?: string
  readonly exitCode?: number
  readonly requestId?: string

  constructor(code: BridgeErrorCode, message: string, options: BridgeErrorOptions = {}) {
    super(redactText(message))
    this.name = "BridgeError"
    this.code = code
    this.details = (options.details ?? []).map(redactText)
    this.retryable = options.retryable ?? false
    this.recovery = options.recovery ?? RECOVERY[code]
    this.bridgeCode = options.bridgeCode
    this.exitCode = options.exitCode ?? undefined
    this.requestId = options.requestId
    if (options.cause !== undefined) this.cause = options.cause
  }

  toJSON(): Record<string, unknown> {
    return {
      code: this.code,
      message: this.message,
      details: this.details,
      retryable: this.retryable,
      recovery: this.recovery,
      bridge_code: this.bridgeCode,
      exit_code: this.exitCode,
      request_id: this.requestId,
    }
  }
}

export interface BridgeClientOptions {
  executable?: string
  workspaceRoot: string
  timeoutMs?: number
  env?: Record<string, string>
  maxOutputBytes?: number
  killGraceMs?: number
}

export interface CallOptions {
  signal?: AbortSignal
  timeoutMs?: number
  requestId?: string
  caller?: BridgeCaller
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []
}

function isFailureEnvelopeFor(stdout: string, requestId: string): boolean {
  const lines = stdout.split(/\r?\n/).filter((line) => line.trim().length > 0)
  if (lines.length !== 1) return false
  try {
    const parsed: unknown = JSON.parse(lines[0]!)
    return isRecord(parsed) && parsed.protocol_version === 1 && parsed.request_id === requestId && parsed.ok === false
  } catch {
    return false
  }
}

export class BridgeClient {
  readonly executable: string
  readonly workspaceRoot: string
  readonly timeoutMs: number
  private readonly env: Record<string, string>
  private readonly maxOutputBytes: number
  private readonly killGraceMs: number

  constructor(options: BridgeClientOptions) {
    this.executable = options.executable ?? process.env.DRIDH_BIN ?? "dridh"
    this.workspaceRoot = options.workspaceRoot
    this.timeoutMs = options.timeoutMs ?? 30_000
    this.env = options.env ?? {}
    this.maxOutputBytes = options.maxOutputBytes ?? 1_000_000
    this.killGraceMs = options.killGraceMs ?? 5_000

    if (!this.executable.trim()) {
      throw new BridgeError("BRIDGE_CONFIGURATION_ERROR", "bridge executable is empty")
    }
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0) {
      throw new BridgeError("BRIDGE_CONFIGURATION_ERROR", "bridge timeout must be greater than zero")
    }
    if (!existsSync(this.workspaceRoot) || !statSync(this.workspaceRoot).isDirectory()) {
      throw new BridgeError("BRIDGE_CONFIGURATION_ERROR", "workspaceRoot is not an existing directory", {
        details: [`workspace_root: ${this.workspaceRoot}`],
      })
    }
  }

  async call(
    operation: BridgeOperation | string,
    payload: Record<string, unknown> = {},
    options: CallOptions = {},
  ): Promise<Record<string, unknown>> {
    const requestId = options.requestId ?? randomUUID()
    const timeoutMs = options.timeoutMs ?? this.timeoutMs
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new BridgeError("BRIDGE_CONFIGURATION_ERROR", "bridge timeout must be greater than zero")
    }

    const request: BridgeRequest = {
      protocol_version: 1,
      request_id: requestId,
      workspace_root: this.workspaceRoot,
      operation,
      payload,
      ...(options.caller ? { caller: options.caller } : {}),
    }

    const transport = await this.invoke(JSON.stringify(request), timeoutMs, options.signal)
    if (transport.timedOut) {
      throw new BridgeError("BRIDGE_TIMEOUT", `bridge timed out after ${timeoutMs}ms`, {
        retryable: true,
        requestId,
        details: [redactText(transport.stderr.slice(-1000))].filter(Boolean),
      })
    }
    if (transport.cancelled) {
      throw new BridgeError("BRIDGE_CANCELLED", "bridge invocation was cancelled", {
        requestId,
        details: [redactText(transport.stderr.slice(-1000))].filter(Boolean),
      })
    }
    if (transport.spawnError) {
      throw new BridgeError("BRIDGE_PROCESS_UNAVAILABLE", "bridge executable could not be started", {
        retryable: false,
        requestId,
        details: [redactText(transport.spawnError)],
      })
    }
    if (transport.exitCode !== 0 && !isFailureEnvelopeFor(transport.stdout, requestId)) {
      throw new BridgeError("BRIDGE_PROCESS_FAILED", `bridge exited with code ${transport.exitCode}`, {
        exitCode: transport.exitCode ?? undefined,
        requestId,
        details: [redactText(transport.stderr.slice(-2000))].filter(Boolean),
      })
    }

    return this.parseResponse(transport.stdout, requestId, transport.exitCode)
  }

  private parseResponse(stdout: string, requestId: string, exitCode: number | null): Record<string, unknown> {
    const lines = stdout.split(/\r?\n/).filter((line) => line.trim().length > 0)
    if (lines.length !== 1) {
      throw new BridgeError("BRIDGE_MALFORMED_RESPONSE", "bridge stdout must contain exactly one JSON object", {
        requestId,
        exitCode,
        details: [`stdout_lines: ${lines.length}`],
      })
    }

    let parsed: unknown
    try {
      parsed = JSON.parse(lines[0]!)
    } catch (error) {
      throw new BridgeError("BRIDGE_MALFORMED_RESPONSE", "bridge stdout is not valid JSON", {
        requestId,
        exitCode,
        details: [redactText(String(error))],
      })
    }

    if (!isRecord(parsed)) {
      throw new BridgeError("BRIDGE_MALFORMED_RESPONSE", "bridge response must be a JSON object", { requestId, exitCode })
    }
    if (parsed.protocol_version !== 1) {
      throw new BridgeError("BRIDGE_PROTOCOL_ERROR", "unsupported bridge protocol_version", {
        requestId,
        exitCode,
        details: [`protocol_version: ${String(parsed.protocol_version)}`],
      })
    }
    if (parsed.request_id !== requestId) {
      throw new BridgeError("BRIDGE_PROTOCOL_ERROR", "bridge response request_id does not match the request", {
        requestId,
        exitCode,
        details: [`returned: ${String(parsed.request_id)}`],
      })
    }
    if (typeof parsed.ok !== "boolean") {
      throw new BridgeError("BRIDGE_PROTOCOL_ERROR", "bridge response is missing boolean ok", { requestId, exitCode })
    }

    if (parsed.ok === false) {
      const error = isRecord(parsed.error) ? parsed.error : undefined
      const code = typeof error?.code === "string" ? error.code : "UNKNOWN_BRIDGE_ERROR"
      const message = typeof error?.message === "string" ? error.message : "bridge returned ok=false"
      throw new BridgeError("BRIDGE_OPERATION_FAILED", message, {
        requestId,
        exitCode,
        bridgeCode: code,
        retryable: error?.retryable === true,
        recovery: typeof error?.recovery === "string" ? error.recovery : undefined,
        details: asStringArray(error?.details),
      })
    }

    if (!isRecord(parsed.data)) {
      throw new BridgeError("BRIDGE_PROTOCOL_ERROR", "bridge success response is missing data object", {
        requestId,
        exitCode,
      })
    }
    return parsed.data
  }

  private invoke(
    request: string,
    timeoutMs: number,
    outerSignal?: AbortSignal,
  ): Promise<{
    stdout: string
    stderr: string
    exitCode: number | null
    timedOut: boolean
    cancelled: boolean
    spawnError?: string
  }> {
    return new Promise((resolve) => {
      let stdout = ""
      let stderr = ""
      let settled = false
      let timedOut = false
      let cancelled = false
      let spawnError: string | undefined
      let timer: NodeJS.Timeout | undefined

      const child = spawn(this.executable, ["bridge"], {
        cwd: this.workspaceRoot,
        env: { ...process.env, ...this.env },
        stdio: ["pipe", "pipe", "pipe"],
        shell: false,
      })

      let escalation: NodeJS.Timeout | undefined

      const finish = (exitCode: number | null) => {
        if (settled) return
        settled = true
        if (timer) clearTimeout(timer)
        if (escalation) clearTimeout(escalation)
        outerSignal?.removeEventListener("abort", onAbort)
        resolve({ stdout, stderr, exitCode, timedOut, cancelled, spawnError })
      }

      // SIGTERM lets the bridge cancel its in-flight worker process groups. SIGKILL
      // is only the escalation after the grace period.
      const terminate = () => {
        if (child.exitCode !== null || child.signalCode !== null || escalation !== undefined) return
        try {
          child.kill("SIGTERM")
        } catch {
          // already gone
        }
        escalation = setTimeout(() => {
          if (child.exitCode === null && child.signalCode === null) {
            try {
              child.kill("SIGKILL")
            } catch {
              // already gone
            }
          }
        }, this.killGraceMs)
      }

      const onAbort = () => {
        cancelled = true
        terminate()
      }

      timer = setTimeout(() => {
        timedOut = true
        terminate()
      }, timeoutMs)

      if (outerSignal) {
        if (outerSignal.aborted) {
          cancelled = true
          terminate()
        } else {
          outerSignal.addEventListener("abort", onAbort, { once: true })
        }
      }

      child.stdout?.on("data", (chunk: Buffer) => {
        stdout += chunk.toString("utf8")
        if (stdout.length > this.maxOutputBytes) {
          stdout = stdout.slice(0, this.maxOutputBytes)
          terminate()
        }
      })
      child.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString("utf8")
        if (stderr.length > this.maxOutputBytes) {
          stderr = stderr.slice(0, this.maxOutputBytes)
          terminate()
        }
      })
      child.on("error", (error: NodeJS.ErrnoException) => {
        spawnError = `${error.name}: ${error.message}`
        finish(null)
      })
      child.on("close", (exitCode: number | null) => finish(exitCode))

      child.stdin?.on("error", () => {
        // The process may exit before reading stdin; the close event reports it.
      })
      child.stdin?.end(request)
    })
  }
}
