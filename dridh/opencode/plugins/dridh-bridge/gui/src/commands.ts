/**
 * Which Dridh commands the desktop panel may run, and the shape of every reply. Shared by the window and the desktop
 * app's main process; no Node or browser APIs here, so the tests run it directly.
 *
 * The main process refuses anything not listed, so even a bug in the window cannot reach another command. Decisions
 * (CONFIRM) only ever run with the person's typed confirmation; commands that agents run (generate, gate, import,
 * validate, ledger draft, ...) are not listed at all.
 */

/** Read-only views the panel shows. */
export const READ: readonly (readonly string[])[] = [
  ["ui", "state"],
  ["ui", "document"],
  ["ui", "documents"],
]

/** Records a person makes without a typed confirmation, as in the terminal panel: ticking a checklist item. */
export const RECORD: readonly (readonly string[])[] = [
  ["reviews", "checklist"],
]

/** Decisions. Each one asks for the typed confirmation Dridh shows; the person types it in the app's dialog. */
export const CONFIRM: readonly (readonly string[])[] = [
  ["ideator", "approve"],
  ["designer", "approve"],
  ["planner", "approve"],
]

/** Options the panel never passes itself: the project comes from the session, and the output format is fixed. */
const RESERVED = /^--(project|workspace|json)(=|$)/

/** True when `args` starts with one of `allowed` and carries no option the runner sets itself. */
export function permitted(allowed: readonly (readonly string[])[], args: readonly string[]): boolean {
  if (!Array.isArray(args) || args.some((arg) => typeof arg !== "string" || RESERVED.test(arg))) return false
  return allowed.some((prefix) => prefix.length <= args.length && prefix.every((word, i) => args[i] === word))
}

export interface Failure {
  readonly message: string
  readonly code?: string
  readonly recovery?: string
}

/** Every reply from the main process: the command's data, or why it failed. Sent across as JSON text. */
export type Reply<T = unknown> = { readonly ok: true; readonly data: T } | { readonly ok: false; readonly error: Failure }

export const success = <T>(data: T): string => JSON.stringify({ ok: true, data } satisfies Reply<T>)

export const failure = (error: unknown): string => {
  const e = error as { message?: unknown; code?: unknown; recovery?: unknown }
  const message = typeof e?.message === "string" && e.message ? e.message : String(error)
  return JSON.stringify({
    ok: false,
    error: { message, ...(typeof e?.code === "string" ? { code: e.code } : {}), ...(typeof e?.recovery === "string" ? { recovery: e.recovery } : {}) },
  } satisfies Reply)
}

/** Reads a reply; a malformed one becomes a failure rather than an exception in the panel. */
export function readReply<T = unknown>(text: string): Reply<T> {
  try {
    const value = JSON.parse(text) as Reply<T>
    if (value && typeof value === "object" && typeof value.ok === "boolean") return value
  } catch { /* fall through */ }
  return { ok: false, error: { message: "Dridh returned an unreadable reply." } }
}

/** A decision waiting for the person's typed confirmation. */
export interface Challenge {
  readonly id: string
  /** What Dridh printed before asking: the document, its revision, the reviewer. */
  readonly summary: string
  /** The exact text Dridh asks the person to type. */
  readonly expected: string
}

/** The interpreter named on a script's first line (`#!/path/python3` or `#!/usr/bin/env python3`), if it is Python. */
export function pythonFromShebang(firstLine: string): string[] | undefined {
  if (!firstLine.startsWith("#!")) return undefined
  const words = firstLine.slice(2).trim().split(/\s+/).filter(Boolean)
  if (!words.length) return undefined
  const program = words[0].endsWith("/env") ? words.slice(1) : words
  return program.length && /python[0-9.]*$/.test(program[0]) ? program : undefined
}
