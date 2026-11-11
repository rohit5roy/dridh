/**
 * Reads the result of a `dridh ... --json` command, as the terminal UI runs them.
 *
 * On success the CLI prints the result as JSON on stdout. On failure it prints a JSON error envelope
 * (`{"ok": false, "error": {"code", "message", "recovery", ...}}`) on stderr and exits non-zero, leaving stdout
 * empty. Both streams are read, so a failure is shown as its message, never as raw JSON.
 */

export const WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"

export class DridhCommandError extends Error {
  readonly code: string | undefined
  readonly recovery: string | undefined

  constructor(message: string, code?: string, recovery?: string) {
    super(message)
    this.name = "DridhCommandError"
    this.code = code
    this.recovery = recovery
  }
}

/** The last line of `text` that parses as a JSON object, or the whole text when it is one JSON document. */
function lastJsonObject(text: string): Record<string, unknown> | undefined {
  const candidates = [text, ...text.split("\n").reverse()]
  for (const candidate of candidates) {
    const trimmed = candidate.trim()
    if (!trimmed.startsWith("{")) continue
    try {
      const value: unknown = JSON.parse(trimmed)
      if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
    } catch {
      // not JSON; try the next candidate
    }
  }
  return undefined
}

function errorFrom(envelope: Record<string, unknown> | undefined): DridhCommandError | undefined {
  if (!envelope || envelope.ok !== false) return undefined
  const error = (envelope.error ?? {}) as Record<string, unknown>
  const message = typeof error.message === "string" && error.message ? error.message : "dridh reported an error"
  return new DridhCommandError(message, typeof error.code === "string" ? error.code : undefined, typeof error.recovery === "string" ? error.recovery : undefined)
}

/**
 * Turns one finished command into its data, or throws a DridhCommandError.
 *
 * @param failed - whether the process failed (non-zero exit, signal or timeout)
 * @param failure - the process error's own message, used only when neither stream explains the failure
 */
export function readCliResult(stdout: string, stderr: string, failed: boolean, failure?: string): unknown {
  const out = lastJsonObject(stdout)
  const reported = errorFrom(out) ?? errorFrom(lastJsonObject(stderr))
  if (reported) throw reported
  if (!failed && out) return "data" in out && out.ok === true ? out.data : out
  const text = (stderr || failure || (failed ? "dridh failed" : "dridh returned invalid JSON")).toString().trim()
  throw new DridhCommandError(text.slice(-400))
}
