/**
 * The desktop app's main process side of Dridh: runs the dridh command for the app's windows.
 *
 * - read and record run a listed command with --json and return its data.
 * - begin starts a decision in a pseudo-terminal of its own (confirm.py) and returns what Dridh asks the person to
 *   type; finish passes the person's typed text through unchanged. Dridh itself checks it, exactly as on a terminal.
 *
 * Agents never reach this: OpenCode gives them tools on the server, and this runs only in the desktop app, for the
 * app's own windows.
 */
import { execFile, spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { randomUUID } from "node:crypto"
import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { isAbsolute, join } from "node:path"
import type { MainContext } from "@opencode/plugin/gui/main"
import { missingExecutableMessage, resolveDridhExecutable } from "../../src/executable.ts"
import { readCliResult } from "../../src/cli-result.ts"
import { CONFIRM, failure, permitted, pythonFromShebang, READ, RECORD, success, type Challenge } from "./commands.ts"
import { DridhDesktop } from "./contract.ts"
import HELPER from "./confirm.py?raw"

/** The installed plugin directory, where the installer records the dridh command (dridh-bin). */
function pluginDirectory(): string {
  const configured = process.env.OPENCODE_CONFIG_DIR?.trim()
  const base = configured && isAbsolute(configured) ? configured : join(homedir(), ".config", "opencode")
  return join(base, "dridh", "plugin", "dridh-bridge")
}

const executable = () => resolveDridhExecutable({ pluginDirectory: pluginDirectory() })

const environment = () => ({ ...process.env, DRIDH_OUTPUT: "json", NO_COLOR: "1" })

/** Runs a listed command with --json and returns its data, or throws Dridh's own error. */
function runJson(project: string, args: readonly string[]): Promise<unknown> {
  const bin = executable()
  return new Promise((resolve, reject) => {
    execFile(bin, [...args, "--json", "--project", project], { cwd: project, timeout: 60_000, maxBuffer: 8 << 20, env: environment() },
      (error, stdout, stderr) => {
        if ((error as NodeJS.ErrnoException | null)?.code === "ENOENT") return reject(new Error(missingExecutableMessage(bin)))
        try {
          resolve(readCliResult(String(stdout ?? ""), String(stderr ?? ""), Boolean(error), error?.message))
        } catch (problem) {
          reject(problem)
        }
      })
  })
}

/** The Python that runs the dridh command (from its first line), else python3: confirm.py needs only the standard library. */
function python(bin: string): string[] {
  try {
    const first = readFileSync(bin, "utf8").split("\n", 1)[0] ?? ""
    return pythonFromShebang(first) ?? ["python3"]
  } catch {
    return ["python3"]
  }
}

type HelperEvent =
  | { event: "prompt"; summary: string; expected: string }
  | { event: "exit"; code: number; stdout: string; tty: string }

interface Pending {
  readonly child: ChildProcessWithoutNullStreams
  readonly next: () => Promise<HelperEvent>
  readonly timer: ReturnType<typeof setTimeout>
}

const pending = new Map<string, Pending>()

/** Starts confirm.py for `args` and returns a reader of its JSON events. */
function startDecision(project: string, args: readonly string[]) {
  if (process.platform === "win32") throw new Error("Decisions from the desktop panel need macOS or Linux. Use the terminal panel instead.")
  const bin = executable()
  const [interpreter, ...options] = python(bin)
  const child = spawn(interpreter, [...options, "-I", "-c", HELPER, JSON.stringify([bin, ...args, "--json", "--project", project])],
    { cwd: project, env: environment(), stdio: ["pipe", "pipe", "pipe"] })
  const events: HelperEvent[] = []
  const waiting: ((event: HelperEvent) => void)[] = []
  let buffered = ""
  let errors = ""
  let exited = false
  const deliver = (event: HelperEvent) => {
    if (exited) return
    if (event.event === "exit") exited = true
    if (waiting.length) waiting.shift()!(event)
    else events.push(event)
  }
  child.stdout.setEncoding("utf8")
  child.stdout.on("data", (chunk: string) => {
    buffered += chunk
    let newline: number
    while ((newline = buffered.indexOf("\n")) >= 0) {
      const line = buffered.slice(0, newline)
      buffered = buffered.slice(newline + 1)
      try { deliver(JSON.parse(line) as HelperEvent) } catch { /* not an event */ }
    }
  })
  child.stderr.on("data", (chunk) => { errors += String(chunk) })
  const ended = (detail: string) => deliver({ event: "exit", code: -1, stdout: "", tty: detail })
  child.on("error", (error) => ended(`Cannot start ${interpreter}: ${error.message}`))
  child.on("close", () => ended(errors.trim() || "The confirmation helper stopped before Dridh answered."))
  const next = () => new Promise<HelperEvent>((resolve) => (events.length ? resolve(events.shift()!) : waiting.push(resolve)))
  return { child, next }
}

function result(event: HelperEvent): unknown {
  if (event.event !== "exit") throw new Error("Dridh asked for a second confirmation.")
  return readCliResult(event.stdout, event.tty, event.code !== 0, event.tty.trim().split("\n").at(-1))
}

function abandon(id: string) {
  const entry = pending.get(id)
  if (!entry) return
  pending.delete(id)
  clearTimeout(entry.timer)
  entry.child.stdin.end() // confirm.py stops the command when no text arrives; nothing is recorded
}

export function main(ctx: MainContext) {
  ctx.provide(DridhDesktop, {
    async read({ project, args }) {
      if (!permitted(READ, args)) return failure(new Error(`Not a view the panel may read: ${args.join(" ")}`))
      try { return success(await runJson(project, args)) } catch (e) { return failure(e) }
    },
    async record({ project, args }) {
      if (!permitted(RECORD, args)) return failure(new Error(`Not a record the panel may make: ${args.join(" ")}`))
      try { return success(await runJson(project, args)) } catch (e) { return failure(e) }
    },
    async begin({ project, args }) {
      if (!permitted(CONFIRM, args)) return failure(new Error(`Not a decision the panel may start: ${args.join(" ")}`))
      try {
        const { child, next } = startDecision(project, args)
        const first = await next()
        if (first.event === "exit") return success({ done: true, result: result(first) })
        const id = randomUUID()
        pending.set(id, { child, next, timer: setTimeout(() => abandon(id), 10 * 60_000) })
        return success({ id, summary: first.summary, expected: first.expected } satisfies Challenge)
      } catch (e) {
        return failure(e)
      }
    },
    async finish({ id, typed }) {
      const entry = pending.get(id)
      if (!entry) return failure(new Error("This confirmation has expired. Start again."))
      pending.delete(id)
      clearTimeout(entry.timer)
      try {
        entry.child.stdin.write(typed.replace(/[\r\n]+/g, " ") + "\n")
        return success(result(await entry.next()))
      } catch (e) {
        return failure(e)
      }
    },
    cancel(id) {
      abandon(id)
    },
  })
  return () => { for (const id of [...pending.keys()]) abandon(id) }
}
