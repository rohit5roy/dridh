/**
 * Finds the dridh command for both halves of the plugin: the server bridge (src/) and the terminal UI (tui/).
 *
 * OpenCode does not necessarily inherit the PATH of the shell that ran the installer, so a bare `dridh` can fail
 * inside OpenCode even though it works in a terminal. The installer therefore records the absolute path of the
 * command it installed in a `dridh-bin` file in the plugin directory. Order of preference:
 *
 *   1. DRIDH_BIN, when set: an explicit choice is used as given.
 *   2. The `dridh-bin` file beside dist/ and tui/, then the one under the OpenCode configuration directory
 *      (OPENCODE_CONFIG_DIR, or ~/.config/opencode, the same rule as the installer). A recorded value is used only
 *      when it is an absolute path to an executable file; anything else (a bare "dridh", a path that no longer
 *      exists) is skipped rather than trusted.
 *   3. The first executable `dridh` on PATH.
 *   4. A bare `dridh`, so the failure names the command that is missing.
 */
import { accessSync, constants, readFileSync, statSync } from "node:fs"
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

export const EXECUTABLE_FILE = "dridh-bin"
export const COMMAND = "dridh"

export interface ResolveOptions {
  env?: NodeJS.ProcessEnv
  /** The installed plugin directory, which holds dist/, tui/ and dridh-bin. Defaults to the parent of this file's folder. */
  pluginDirectory?: string
}

const defaultPluginDirectory = () => resolve(dirname(fileURLToPath(import.meta.url)), "..")

export function isExecutableFile(path: string): boolean {
  try {
    if (!statSync(path).isFile()) return false
    accessSync(path, constants.X_OK)
    return true
  } catch {
    return false
  }
}

/** The dridh-bin files to consult, nearest first. */
export function recordFiles(env: NodeJS.ProcessEnv = process.env, pluginDirectory = defaultPluginDirectory()): string[] {
  const files = [join(pluginDirectory, EXECUTABLE_FILE)]
  const configured = env.OPENCODE_CONFIG_DIR?.trim()
  const base = configured ? configured : env.HOME ? join(env.HOME, ".config", "opencode") : undefined
  if (base && isAbsolute(base)) {
    const installed = join(base, "dridh", "plugin", "dridh-bridge", EXECUTABLE_FILE)
    if (!files.includes(installed)) files.push(installed)
  }
  return files
}

/** The absolute executable path recorded by the installer, if one is recorded and still usable. */
export function recordedExecutable(env: NodeJS.ProcessEnv = process.env, pluginDirectory = defaultPluginDirectory()): string | undefined {
  for (const file of recordFiles(env, pluginDirectory)) {
    let recorded: string
    try {
      recorded = readFileSync(file, "utf8").trim()
    } catch {
      continue
    }
    if (recorded && isAbsolute(recorded) && isExecutableFile(recorded)) return recorded
  }
  return undefined
}

export function executableOnPath(env: NodeJS.ProcessEnv = process.env): string | undefined {
  for (const directory of (env.PATH ?? "").split(delimiter)) {
    if (!directory || !isAbsolute(directory)) continue
    const candidate = join(directory, COMMAND)
    if (isExecutableFile(candidate)) return candidate
  }
  return undefined
}

export function resolveDridhExecutable(options: ResolveOptions = {}): string {
  const env = options.env ?? process.env
  const configured = env.DRIDH_BIN?.trim()
  if (configured) return configured
  return recordedExecutable(env, options.pluginDirectory ?? defaultPluginDirectory()) ?? executableOnPath(env) ?? COMMAND
}

/** A message for a dridh command that could not be started, with the way to fix it. */
export function missingExecutableMessage(executable: string): string {
  return executable === COMMAND
    ? "Dridh command not found. Re-run the installer or set DRIDH_BIN."
    : `Cannot start ${executable}. Re-run the installer or set DRIDH_BIN.`
}
