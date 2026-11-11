// How the plugin finds the dridh command (src/executable.ts), exercised against real files: OpenCode may start the
// plugin with a PATH that does not include the directory the installer put dridh in.
import { test } from "node:test"
import assert from "node:assert/strict"
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { delimiter, join } from "node:path"

const { resolveDridhExecutable, recordedExecutable, recordFiles, missingExecutableMessage } = await import(
  new URL("../dist/executable.js", import.meta.url)
)
const { BridgeClient } = await import(new URL("../dist/bridge-client.js", import.meta.url))

function sandbox(t) {
  const root = mkdtempSync(join(tmpdir(), "dridh-executable-"))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const plugin = join(root, "config", "opencode", "dridh", "plugin", "dridh-bridge")
  mkdirSync(plugin, { recursive: true })
  const home = join(root, "home")
  mkdirSync(home)
  return { root, plugin, home }
}

function program(path, mode = 0o755) {
  mkdirSync(join(path, ".."), { recursive: true })
  writeFileSync(path, "#!/bin/sh\nexit 0\n")
  chmodSync(path, mode)
  return path
}

const record = (directory, value) => writeFileSync(join(directory, "dridh-bin"), value)

test("the installer record is used when PATH does not contain dridh", (t) => {
  const { root, plugin, home } = sandbox(t)
  const installed = program(join(root, "uv", "tools", "dridh", "bin", "dridh"))
  record(plugin, installed + "\n")
  const env = { HOME: home, PATH: "/usr/bin" + delimiter + "/bin" }
  assert.equal(resolveDridhExecutable({ env, pluginDirectory: plugin }), installed)
})

test("DRIDH_BIN wins over the installer record and PATH", (t) => {
  const { root, plugin, home } = sandbox(t)
  record(plugin, program(join(root, "recorded", "dridh")))
  const onPath = program(join(root, "path", "dridh"))
  const env = { HOME: home, PATH: join(root, "path"), DRIDH_BIN: " /opt/custom/dridh " }
  assert.equal(resolveDridhExecutable({ env, pluginDirectory: plugin }), "/opt/custom/dridh")
  assert.notEqual(resolveDridhExecutable({ env: { ...env, DRIDH_BIN: "  " }, pluginDirectory: plugin }), onPath)
})

test("the installer record wins over PATH", (t) => {
  const { root, plugin, home } = sandbox(t)
  const installed = program(join(root, "recorded", "dridh"))
  record(plugin, installed)
  program(join(root, "path", "dridh"))
  assert.equal(resolveDridhExecutable({ env: { HOME: home, PATH: join(root, "path") }, pluginDirectory: plugin }), installed)
})

test("an unusable record falls back to PATH instead of failing", (t) => {
  const { root, plugin, home } = sandbox(t)
  const onPath = program(join(root, "path", "dridh"))
  const env = { HOME: home, PATH: join(root, "path") }
  for (const value of ["dridh\n", "relative/dridh\n", join(root, "gone", "dridh") + "\n", "", "\n"]) {
    record(plugin, value)
    assert.equal(resolveDridhExecutable({ env, pluginDirectory: plugin }), onPath, `record ${JSON.stringify(value)}`)
  }
  record(plugin, program(join(root, "not-executable", "dridh"), 0o644))
  assert.equal(resolveDridhExecutable({ env, pluginDirectory: plugin }), onPath, "a recorded file that cannot be run")
  record(plugin, join(root, "path"))
  assert.equal(resolveDridhExecutable({ env, pluginDirectory: plugin }), onPath, "a recorded directory")
})

test("the record under the OpenCode configuration directory is found when the plugin directory has none", (t) => {
  const { root, plugin, home } = sandbox(t)
  const installed = program(join(root, "recorded", "dridh"))
  record(plugin, installed)
  const elsewhere = join(root, "loaded-from-elsewhere")
  mkdirSync(elsewhere)
  const config = join(root, "config", "opencode")
  assert.equal(resolveDridhExecutable({ env: { OPENCODE_CONFIG_DIR: config, PATH: "" }, pluginDirectory: elsewhere }), installed)

  const defaultHome = join(root, "default-home")
  const defaultPlugin = join(defaultHome, ".config", "opencode", "dridh", "plugin", "dridh-bridge")
  mkdirSync(defaultPlugin, { recursive: true })
  record(defaultPlugin, installed)
  assert.equal(resolveDridhExecutable({ env: { HOME: defaultHome, PATH: "" }, pluginDirectory: elsewhere }), installed)
  assert.deepEqual(recordFiles({ HOME: home }, plugin), [join(plugin, "dridh-bin"), join(home, ".config", "opencode", "dridh", "plugin", "dridh-bridge", "dridh-bin")])
})

test("PATH is searched in order, skipping relative entries and entries without an executable dridh", (t) => {
  const { root, plugin, home } = sandbox(t)
  program(join(root, "first", "dridh"), 0o644)
  const second = program(join(root, "second", "dridh"))
  program(join(root, "third", "dridh"))
  const env = { HOME: home, PATH: ["", "relative", join(root, "empty"), join(root, "first"), join(root, "second"), join(root, "third")].join(delimiter) }
  assert.equal(resolveDridhExecutable({ env, pluginDirectory: plugin }), second)
})

test("with nothing found, the bare command is returned and the message says how to fix it", (t) => {
  const { plugin, home } = sandbox(t)
  assert.equal(resolveDridhExecutable({ env: { HOME: home, PATH: "" }, pluginDirectory: plugin }), "dridh")
  assert.equal(recordedExecutable({ HOME: home }, plugin), undefined)
  assert.match(missingExecutableMessage("dridh"), /not found\. Re-run the installer or set DRIDH_BIN/)
  assert.match(missingExecutableMessage("/gone/dridh"), /Cannot start \/gone\/dridh\./)
  assert.ok(missingExecutableMessage("dridh").length <= 72, "fits the status line")
})

test("the bridge client resolves its executable the same way", (t) => {
  const { root, plugin, home } = sandbox(t)
  const installed = program(join(root, "recorded", "dridh"))
  record(plugin, installed)
  const saved = { DRIDH_BIN: process.env.DRIDH_BIN, PATH: process.env.PATH, HOME: process.env.HOME, OPENCODE_CONFIG_DIR: process.env.OPENCODE_CONFIG_DIR }
  t.after(() => { for (const [key, value] of Object.entries(saved)) value === undefined ? delete process.env[key] : (process.env[key] = value) })
  delete process.env.DRIDH_BIN
  process.env.PATH = ""
  process.env.HOME = home
  process.env.OPENCODE_CONFIG_DIR = join(root, "config", "opencode")
  assert.equal(new BridgeClient({ workspaceRoot: root }).executable, installed)
  assert.equal(new BridgeClient({ workspaceRoot: root, executable: "/explicit/dridh" }).executable, "/explicit/dridh")
})
