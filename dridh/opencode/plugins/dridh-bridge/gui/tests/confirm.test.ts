// confirm.py: runs a decision command in a pseudo-terminal of its own and relays its typed confirmation.
// A stand-in for Dridh asks the way Dridh does (stdin must be a terminal; "Type exactly: <phrase>"), then prints JSON.
import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"

const HELPER = readFileSync(new URL("../src/confirm.py", import.meta.url), "utf8")
const FAKE = `
import json, sys
if not sys.stdin.isatty():
    print(json.dumps({"ok": False, "error": {"code": "AUTHORIZATION_DENIED", "message": "needs a terminal"}}), file=sys.stderr); sys.exit(3)
if sys.argv[1] == "refuse":
    print(json.dumps({"ok": False, "error": {"code": "VALIDATION_FAILED", "message": "checklist incomplete"}}), file=sys.stderr); sys.exit(2)
sys.stderr.write("Approve doc.md for review R?\\nType exactly: APPROVE R 1234\\n> "); sys.stderr.flush()
typed = sys.stdin.readline().strip()
if typed != "APPROVE R 1234":
    print(json.dumps({"ok": False, "error": {"code": "APPROVAL_REQUIRED", "message": "did not match"}}), file=sys.stderr); sys.exit(4)
print(json.dumps({"ok": True, "data": {"approved_by": sys.argv[2]}}))
`
const fake = join(mkdtempSync(join(tmpdir(), "dridh-confirm-")), "fake-dridh.py")
writeFileSync(fake, FAKE)

type Event = Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any

/** Runs the helper; `answer` decides what to type when asked (undefined: close stdin, as Cancel does). */
function run(args: string[], answer: (prompt: Event) => string | undefined): Promise<Event[]> {
  return new Promise((resolve) => {
    const child = spawn("python3", ["-I", "-c", HELPER, JSON.stringify(["python3", fake, ...args])])
    const events: Event[] = []
    let buffer = ""
    child.stdout.setEncoding("utf8")
    child.stdout.on("data", (chunk: string) => {
      buffer += chunk
      for (let i; (i = buffer.indexOf("\n")) >= 0;) {
        const event = JSON.parse(buffer.slice(0, i)) as Event
        buffer = buffer.slice(i + 1)
        events.push(event)
        if (event.event === "prompt") {
          const typed = answer(event)
          if (typed === undefined) child.stdin.end()
          else child.stdin.write(typed + "\n")
        }
      }
    })
    child.on("close", () => resolve(events))
  })
}

test("relays Dridh's summary and phrase, then passes the typed text through unchanged", async () => {
  const events = await run(["approve", "Ada"], () => "APPROVE R 1234")
  assert.deepEqual(events[0], { event: "prompt", summary: "Approve doc.md for review R?", expected: "APPROVE R 1234" })
  assert.equal(events[1].event, "exit")
  assert.equal(events[1].code, 0)
  assert.deepEqual(JSON.parse(events[1].stdout), { ok: true, data: { approved_by: "Ada" } })
})

test("a wrong phrase is refused by the command itself", async () => {
  const [, exit] = await run(["approve", "Ada"], () => "APPROVE something else")
  assert.equal(exit.code, 4)
  assert.equal(exit.stdout, "")
  assert.match(exit.tty, /did not match/)
})

test("cancelling stops the command, and nothing is printed as a result", async () => {
  const [prompt, exit] = await run(["approve", "Ada"], () => undefined)
  assert.equal(prompt.event, "prompt")
  assert.notEqual(exit.code, 0)
  assert.equal(exit.stdout, "")
})

test("a command that refuses before asking returns its error at once", async () => {
  const events = await run(["refuse", "Ada"], () => "never asked")
  assert.equal(events.length, 1)
  assert.equal(events[0].event, "exit")
  assert.equal(events[0].code, 2)
  assert.match(events[0].tty, /checklist incomplete/)
})
