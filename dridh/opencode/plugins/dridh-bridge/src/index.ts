/**
 * Production Dridh OpenCode V2 plugin.
 *
 * Registers one typed tool, `dridh.bridge`, that invokes `dridh bridge` with
 * protocol v1 and returns the bridge data. The adapter checks `ok`, protocol
 * version, request ID and response structure; it never treats exit code 0 as
 * success by itself.
 *
 * The caller identity is taken from OpenCode's execute context, never from the
 * model. Workflow operations are authorised by the bridge only for the
 * `dridh/coordinator` agent.
 *
 * Hooks (plan section 7.5), for Dridh agents (`dridh/...`) only, so ordinary sessions never touch Dridh state:
 * - session `context`: adds a compact summary of the feature, stage, next human step and blockers to the request;
 * - tool `execute.before`/`execute.after`: records each tool call's metadata (tool, agent, session, status, duration,
 *   input keys and SHA-256, never the input itself) in Dridh's audit table;
 * - the event stream: records execution, idle and compaction events of Dridh sessions for correlation and status.
 * The experimental idle hook is not used, so no workflow correctness depends on it (D-P7-05).
 */
import { Plugin } from "@opencode/plugin"
import { createHash } from "node:crypto"
import { BridgeClient, BridgeError, CODE_REACHER_OPERATIONS, COORDINATOR_OPERATIONS, DESIGNER_OPERATIONS, IDEATOR_OPERATIONS, JEV_OPERATIONS, PLANNER_OPERATIONS, RUNTIME_OPERATIONS, WORKFLOW_OPERATIONS } from "./bridge-client.js"

const DEFAULT_RUNTIME_TIMEOUT_MS = 30_000
const DEFAULT_WORKFLOW_TIMEOUT_MS = 1_200_000
const ALL_OPERATIONS = [...RUNTIME_OPERATIONS, ...JEV_OPERATIONS, ...WORKFLOW_OPERATIONS, ...IDEATOR_OPERATIONS, ...DESIGNER_OPERATIONS, ...PLANNER_OPERATIONS, ...COORDINATOR_OPERATIONS, ...CODE_REACHER_OPERATIONS]
const MAX_TIMEOUT_MS = 1_800_000

type ToolContext = {
  signal?: AbortSignal
  sessionID?: string
  agent?: string
}

function workspaceRoot(ctx: unknown): string {
  const location = (ctx as { location?: { directory?: string } } | undefined)?.location
  return location?.directory ?? process.cwd()
}

function positiveNumberFromEnvironment(name: string, fallback: number): number {
  const raw = Number(process.env[name] ?? fallback)
  return Number.isFinite(raw) && raw > 0 ? raw : fallback
}

export default Plugin.define({
  id: "dridh.bridge",
  async setup(ctx) {
    const client = new BridgeClient({
      executable: process.env.DRIDH_BIN,
      workspaceRoot: workspaceRoot(ctx),
      timeoutMs: positiveNumberFromEnvironment("DRIDH_BRIDGE_TIMEOUT_MS", DEFAULT_RUNTIME_TIMEOUT_MS),
    })
    const workflowTimeoutMs = positiveNumberFromEnvironment("DRIDH_WORKFLOW_TIMEOUT_MS", DEFAULT_WORKFLOW_TIMEOUT_MS)

    const registration = await ctx.tool.transform((editor) => {
      editor.namespace({ name: "dridh", description: "Dridh runtime bridge tools" })
      editor.add({
        name: "bridge",
        description:
          "Invoke the Dridh Python bridge using protocol v1. Runtime operations: runtime.ping, runtime.info. " +
          "Workspace operations (Dridh Coordinator only): workspace.init, workspace.status. " +
          "The generic workflow.* operations are retired: do not create or plan workflows; features go through the lifecycle below and execution is started by a human with `dridh execute start`. " +
          "Ideator operations: ideator.start {objective, existing_context, feature} (binds the session to the feature; one per feature), ideator.answer, ideator.status, ideator.generate, ideator.parity and ideator.gate. " +
          "Discovery is resumable and human approval remains terminal-only. " +
          "Technical Designer operations: designer.start, designer.answer, designer.status, designer.generate, designer.parity and designer.gate. " +
          "TDD generation, validation and reconciliation are deterministic; TDD approval is terminal-only. " +
          "Planner operations: planner.start, planner.clarify, planner.status, planner.generate, planner.sync and planner.gate. Planner requires the current human-approved TDD and Code Reacher index for existing repositories; planning handoff remains subject to human review. "
          + "Lifecycle operations: coordinator.create_feature, coordinator.status, coordinator.sync and coordinator.transition; read-only coordinator.get_context, coordinator.query, coordinator.validate, coordinator.list_reviews {status} and coordinator.review_checklist {review_id} (what a human still has to tick; agents cannot tick or approve). Code Reacher (query the codebase index BEFORE searching files by hand): code_reacher.lookup {query,limit,semantic}, code_reacher.symbol {name}, code_reacher.dependencies {symbol,direction,depth}, code_reacher.index_status, code_reacher.code_for {target} (code implementing a story, component, use case or flow) and code_reacher.owners {path} (which stories/use cases a file implements); only the Coordinator may call code_reacher.index. Unresolved call edges are reported as unresolved, never guessed. A feature advances only when the current stage's own evidence is satisfied; completing a feature and every approval are terminal-only. Returns the bridge data object.",
        input: {
          type: "object",
          properties: {
            operation: {
              type: "string",
              enum: ALL_OPERATIONS,
              default: "runtime.ping",
              description: "Allow-listed Dridh bridge operation",
            },
            payload: {
              type: "object",
              description: "Optional operation payload, for example { workflow_id, objective, task_id, reason }",
              additionalProperties: true,
            },
            timeout_ms: {
              type: "number",
              description: "Bounded bridge timeout in milliseconds",
            },
          },
          required: [],
          additionalProperties: false,
        },
        options: { namespace: "dridh" },
        execute: async (input, context: ToolContext) => {
          const args = input as {
            operation?: string
            payload?: Record<string, unknown>
            timeout_ms?: number
          }
          const operation = args.operation ?? "runtime.ping"
          const isWorkflow = ([...WORKFLOW_OPERATIONS, ...IDEATOR_OPERATIONS, ...DESIGNER_OPERATIONS, ...PLANNER_OPERATIONS, ...COORDINATOR_OPERATIONS, ...CODE_REACHER_OPERATIONS] as readonly string[]).includes(operation)
          try {
            const data = await client.call(operation, args.payload ?? {}, {
              signal: context.signal,
              timeoutMs: args.timeout_ms === undefined ? (isWorkflow ? workflowTimeoutMs : undefined) : Math.min(Math.max(args.timeout_ms, 1), MAX_TIMEOUT_MS),
              caller: {
                source: "plugin",
                agent: context.agent,
                session_id: context.sessionID,
              },
            })
            return {
              content: [{ type: "text", text: JSON.stringify(data) }],
              metadata: {
                operation,
                protocol_version: 1,
              },
            }
          } catch (error) {
            if (error instanceof BridgeError) {
              const payload = error.toJSON()
              const surfaced = new Error(`${error.code}: ${error.message}`) as Error & Record<string, unknown>
              Object.assign(surfaced, payload)
              throw surfaced
            }
            throw error
          }
        },
      })

      editor.add({
        name: "jev_decide",
        description:
          "Ask Jev System One for a typed advisory decision. Use only noul, choice or score questions. " +
          "Jev is advisory: it cannot approve documents, override deterministic validation or replace human decisions. " +
          "The Python bridge validates the request, applies bounded provider retries, persists a redacted evaluation and returns a deterministic-fallback status when Jev is unavailable.",
        input: {
          type: "object",
          properties: {
            state: { type: "string", description: "Compact, redacted situation and evidence; do not include secrets." },
            evaluation_level: { type: "string", enum: ["TURN", "TASK", "ARTIFACT"], default: "TURN" },
            task_type: { type: "string", description: "Evaluation context, for example use-case completeness or test policy." },
            subject_id: { type: "string", description: "Stable task, artifact or session reference." },
            questions: {
              type: "array",
              minItems: 1,
              maxItems: 8,
              items: {
                type: "object",
                properties: {
                  key: { type: "string" },
                  type: { type: "string", enum: ["noul", "choice", "score"] },
                  instructions: { type: "string" },
                  criteria: { type: ["object", "array"] },
                },
                required: ["key", "type", "instructions"],
                additionalProperties: false,
              },
            },
          },
          required: ["state", "questions"],
          additionalProperties: false,
        },
        options: { namespace: "dridh" },
        execute: async (input, context: ToolContext) => {
          const args = input as {
            state: string
            evaluation_level?: "TURN" | "TASK" | "ARTIFACT"
            task_type?: string
            subject_id?: string
            questions: Array<{ key: string; type: "noul" | "choice" | "score"; instructions: string; criteria?: Record<string, unknown> | string[] }>
          }
          const data = await client.call(
            "jev.decide",
            {
              state: args.state,
              evaluation_level: args.evaluation_level ?? "TURN",
              task_type: args.task_type ?? "agent_turn",
              subject_id: args.subject_id,
              questions: args.questions,
            },
            {
              signal: context.signal,
              caller: {
                source: "plugin",
                agent: context.agent,
                session_id: context.sessionID,
              },
            },
          )
          return {
            content: [{ type: "text", text: JSON.stringify(data) }],
            metadata: { operation: "jev.decide", protocol_version: 1 },
          }
        },
      })
    })


    // ---------------------------------------------------------------- hooks (plan section 7.5)
    const isDridh = (agent: unknown): agent is string => typeof agent === "string" && agent.startsWith("dridh/")
    const dridhSessions = new Map<string, string>() // sessionID -> agent, learned from Dridh agents' requests and tool calls
    const contextCache = new Map<string, { at: number; text: string }>()
    const CONTEXT_TTL_MS = 20_000
    const hookCaller = (agent: string, sessionID: string) => ({ source: "plugin" as const, agent, session_id: sessionID })
    const inFlight = new Set<Promise<unknown>>()
    const quietly = (work: Promise<unknown>) => { // a hook must never disturb the session; shutdown waits for pending records
      const settled = work.catch(() => undefined).finally(() => inFlight.delete(settled))
      inFlight.add(settled)
    }
    // Events can arrive before a session is known to be a Dridh session (execution starts before the first request).
    const pendingEvents = new Map<string, string[]>()
    const recordEvent = (agent: string, sessionID: string, type: string) =>
      quietly(client.call("hook.session_event", { agent, session_id: sessionID, type }, { caller: hookCaller(agent, sessionID), timeoutMs: 10_000 }))
    const markDridh = (sessionID: string, agent: string) => {
      if (dridhSessions.has(sessionID)) return
      dridhSessions.set(sessionID, agent)
      for (const type of pendingEvents.get(sessionID) ?? []) recordEvent(agent, sessionID, type)
      pendingEvents.delete(sessionID)
    }

    const started = new Map<string, number>() // tool call ID -> start time
    const hooks = [
      await ctx.session.hook("context", async (input) => {
        if (!isDridh(input.agent)) return
        markDridh(input.sessionID, input.agent)
        let cached = contextCache.get(input.sessionID)
        if (!cached || Date.now() - cached.at > CONTEXT_TTL_MS) {
          try {
            const data = await client.call("hook.context", { agent: input.agent, session_id: input.sessionID }, { caller: hookCaller(input.agent, input.sessionID), timeoutMs: 10_000 })
            cached = { at: Date.now(), text: typeof data?.text === "string" ? data.text : "" }
          } catch {
            cached = { at: Date.now(), text: "" }
          }
          contextCache.set(input.sessionID, cached)
        }
        if (cached.text) input.system.push({ type: "text", text: cached.text, metadata: { source: "dridh" } })
      }),
      await ctx.tool.hook("execute.before", (input) => {
        if (isDridh(input.agent)) {
          markDridh(input.sessionID, input.agent)
          started.set(input.id, Date.now())
        }
      }),
      await ctx.tool.hook("execute.after", (input) => {
        if (!isDridh(input.agent)) return
        const begin = started.get(input.id)
        started.delete(input.id)
        const value = input.input && typeof input.input === "object" ? (input.input as Record<string, unknown>) : {}
        quietly(client.call("hook.tool_call", {
          agent: input.agent,
          session_id: input.sessionID,
          tool: input.tool,
          status: input.status,
          duration_ms: begin === undefined ? 0 : Date.now() - begin,
          input_keys: Object.keys(value).slice(0, 50),
          input_sha256: createHash("sha256").update(JSON.stringify(input.input ?? null)).digest("hex"),
        }, { caller: hookCaller(input.agent, input.sessionID), timeoutMs: 10_000 }))
      }),
    ]

    const RECORDED_EVENTS = ["session.execution.started", "session.execution.succeeded", "session.execution.failed",
      "session.execution.interrupted", "session.idle", "session.compaction.started", "session.compaction.ended",
      "session.compaction.failed"] as const
    const events = new AbortController()
    quietly((async () => {
      for await (const event of ctx.event.subscribe({ types: RECORDED_EVENTS, signal: events.signal })) {
        const sessionID = (event as { data?: { sessionID?: string } }).data?.sessionID
        if (!sessionID) continue
        const agent = dridhSessions.get(sessionID)
        if (agent) recordEvent(agent, sessionID, event.type)
        else if (pendingEvents.size < 100) pendingEvents.set(sessionID, [...(pendingEvents.get(sessionID) ?? []), event.type].slice(-5))
      }
    })())

    return async () => {
      events.abort()
      // Let pending audit records finish (bounded), so a short `opencode run` does not drop its last events.
      await Promise.race([Promise.allSettled([...inFlight]), new Promise((resolve) => setTimeout(resolve, 3_000))])
      for (const hook of hooks) await hook.dispose()
      await registration.dispose()
    }
  },
})
