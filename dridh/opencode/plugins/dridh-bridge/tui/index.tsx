/** @jsxImportSource @opentui/solid */
/**
 * Dridh inside OpenCode's terminal UI. OpenCode loads this from the plugin package's `./tui` export; OpenCode itself is
 * not modified.
 *
 * Inside a session the Dridh views open in OpenCode's session panel next to the conversation, and the sidebar carries a
 * Dridh section (stages, documents, the open checklist, the next step, actions). On the home screen they are full pages.
 * Documents render through glow when it is installed, with Mermaid diagrams drawn as text; nothing leaves OpenCode.
 *
 * What it shows comes from `dridh ui …` (read-only JSON). What it changes goes through the existing Dridh CLI:
 * - Ticking a checklist item runs `dridh reviews checklist`, the same human record the CLI keeps.
 * - Approvals, lot verdicts, starting a run and completing a feature are typed confirmations. For those the plugin
 *   suspends OpenCode's screen, the way OpenCode opens $EDITOR, and hands the real terminal to the Dridh command. The
 *   person types the confirmation into Dridh's own prompt; the plugin never sees or forwards it. Agents have no tool
 *   that reaches any of this.
 */
import { Plugin } from "@opencode/plugin/tui"
import { useKeyboard } from "@opentui/solid"
import { createEffect, createMemo, createSignal, For, Show } from "solid-js"
import { execFile, spawn } from "node:child_process"
// Shared with the server bridge; the installer places dist/ beside tui/ (see src/executable.ts).
import { missingExecutableMessage, resolveDridhExecutable } from "../dist/executable.js"
import { readCliResult, WORKSPACE_NOT_FOUND } from "../dist/cli-result.js"

type Json = any

/** The dridh command, found the same way as the server bridge finds it: DRIDH_BIN, the installer record, PATH. */
const bin = () => resolveDridhExecutable()

/** Run a read-only or record-only Dridh command and parse its JSON. */
function dridhJson(args: string[], cwd: string): Promise<Json> {
  return new Promise((resolve, reject) => {
    const executable = bin()
    execFile(executable, [...args, "--json", "--project", cwd], { cwd, timeout: 60_000, maxBuffer: 8 << 20, env: { ...process.env, DRIDH_OUTPUT: "json" } },
      (error, stdout, stderr) => {
        if ((error as NodeJS.ErrnoException | null)?.code === "ENOENT") return reject(new Error(missingExecutableMessage(executable)))
        try {
          resolve(readCliResult(String(stdout ?? ""), String(stderr ?? ""), Boolean(error), error?.message))
        } catch (failure) {
          reject(failure)
        }
      })
  })
}

// ------------------------------------------------------------------ glow (optional) and ANSI colour codes

/** Glow (https://github.com/charmbracelet/glow) renders markdown when it is on PATH or named by DRIDH_GLOW. */
const glowBin = () => process.env.DRIDH_GLOW || "glow"

function runGlow(markdown: string, width: number, mode: "dark" | "light"): Promise<string> {
  return new Promise((resolve, reject) => {
    // OpenCode's process may carry NO_COLOR, which glow honours over CLICOLOR_FORCE; colour is wanted here because
    // the plugin turns the codes into styled spans itself.
    const { NO_COLOR: _ignored, ...env } = process.env
    const child = spawn(glowBin(), ["-s", mode, "-w", String(width), "-"], { env: { ...env, CLICOLOR_FORCE: "1", COLORTERM: "truecolor" } })
    let out = ""
    child.stdout.on("data", (chunk) => (out += chunk))
    child.on("error", reject)
    child.on("exit", (code) => (code === 0 ? resolve(out) : reject(new Error(`glow exited ${code}`))))
    child.stdin.end(markdown)
  })
}

type Seg = { text: string; fg?: string; bg?: string; bold?: boolean; italic?: boolean; underline?: boolean }

const BASE16 = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
  "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
function xterm256(n: number): string {
  if (n < 16) return BASE16[n]
  if (n >= 232) { const v = 8 + (n - 232) * 10; return "#" + v.toString(16).padStart(2, "0").repeat(3) }
  const i = n - 16, steps = [0, 95, 135, 175, 215, 255]
  return "#" + [Math.floor(i / 36), Math.floor(i / 6) % 6, i % 6].map((k) => steps[k].toString(16).padStart(2, "0")).join("")
}

/** Split terminal output into lines of styled segments (SGR colour and weight codes only). */
function parseAnsi(text: string): Seg[][] {
  const lines: Seg[][] = []
  let style: Omit<Seg, "text"> = {}
  for (const raw of text.replace(/\r/g, "").split("\n")) {
    const line: Seg[] = []
    const parts = raw.split(/\x1b\[([0-9;]*)m/)
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) {
        const chunk = parts[i].replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "")
        if (!chunk) continue
        const last = line.at(-1)
        if (last && last.fg === style.fg && last.bg === style.bg && last.bold === style.bold && last.italic === style.italic && last.underline === style.underline) last.text += chunk
        else line.push({ text: chunk, ...style })
        continue
      }
      const codes = parts[i] === "" ? [0] : parts[i].split(";").map((x) => (x === "" ? 0 : Number(x)))
      for (let k = 0; k < codes.length; k++) {
        const code = codes[k]
        if (code === 0) style = {}
        else if (code === 1) style = { ...style, bold: true }
        else if (code === 3) style = { ...style, italic: true }
        else if (code === 4) style = { ...style, underline: true }
        else if (code === 22) style = { ...style, bold: false }
        else if (code === 23) style = { ...style, italic: false }
        else if (code === 24) style = { ...style, underline: false }
        else if (code >= 30 && code <= 37) style = { ...style, fg: BASE16[code - 30] }
        else if (code >= 90 && code <= 97) style = { ...style, fg: BASE16[code - 82] }
        else if (code >= 40 && code <= 47) style = { ...style, bg: BASE16[code - 40] }
        else if (code >= 100 && code <= 107) style = { ...style, bg: BASE16[code - 92] }
        else if (code === 39) style = { ...style, fg: undefined }
        else if (code === 49) style = { ...style, bg: undefined }
        else if ((code === 38 || code === 48) && codes[k + 1] === 5) {
          style = { ...style, [code === 38 ? "fg" : "bg"]: xterm256(codes[k + 2]) }; k += 2
        } else if ((code === 38 || code === 48) && codes[k + 1] === 2) {
          style = { ...style, [code === 38 ? "fg" : "bg"]: "#" + codes.slice(k + 2, k + 5).map((v) => v.toString(16).padStart(2, "0")).join("") }; k += 4
        }
      }
    }
    lines.push(line)
  }
  while (lines.length && lines.at(-1)!.every((seg) => !seg.text.trim())) lines.pop()
  return lines
}

function palette(mode: "dark" | "light") {
  return mode === "light"
    ? { accent: "#0f766e", muted: "#59616e", text: "#1c2128", ok: "#1f7a3a", warn: "#9a5b00", bad: "#b42318", add: "#1f7a3a", del: "#b42318", dim: "#8b93a1", border: "#c9ced6", heading: "#0f5f58" }
    : { accent: "#5ec4b6", muted: "#8b93a1", text: "#d6dae1", ok: "#7fc98a", warn: "#e0b25a", bad: "#ef8f8c", add: "#7fc98a", del: "#ef8f8c", dim: "#4a5160", border: "#2a303b", heading: "#8fe0d4" }
}

const MARK = { done: "✓", current: "●", todo: "○" } as const

export default Plugin.define({
  id: "dridh",
  setup(ctx) {
    /** The next step once Dridh is set up. Works whether or not the Coordinator is already selected. */
    const START_FEATURE = "Select Dridh/Coordinator (Shift+Tab) and describe a feature."
    /** The folder OpenCode is in now. It changes with /cd, so it is read every time, never kept from startup. */
    const root = () => ctx.location?.directory ?? process.cwd()
    let shownRoot: string | undefined
    const c = palette(ctx.themeMode)
    const [state, setState] = createSignal<Json>(undefined)
    const [error, setError] = createSignal<string | undefined>(undefined)
    /** True when this project has no Dridh workspace yet: a first run, not an error. /dridh offers to set it up. */
    const [unset, setUnset] = createSignal(false)
    const [settings, setSettings] = ctx.storage.store("dridh.tui", { initial: { reviewer: "" } })
    /** The refresh in progress, shared: a second caller waits for it instead of starting another. */
    let inflight: Promise<void> | undefined
    let quietUntil = 0
    /** The next state change comes from something the person just did here; do not notify them about it. */
    const quiet = () => { quietUntil = Date.now() + 15_000 }
    let lastAction: string | undefined

    const refresh = (): Promise<void> => (inflight ??= load().finally(() => { inflight = undefined }))
    const load = async () => {
      const folder = root()
      if (folder !== shownRoot) {
        // OpenCode moved to another folder (/cd): forget everything shown for the previous one.
        shownRoot = folder
        lastAction = undefined
        setState(undefined)
        setUnset(false)
        setError(undefined)
      }
      try {
        const next = await dridhJson(["ui", "state"], folder)
        if (folder !== root()) return  // moved again while reading: the next refresh reads the new folder
        const action = next?.human_action ? `${next.feature?.slug}:${next.human_action.kind}:${next.human_action.text}` : undefined
        if (lastAction !== undefined && action && action !== lastAction && next.human_action.kind !== "wait" && Date.now() > quietUntil) {
          ctx.ui.toast.show({ title: "Dridh needs you", message: next.human_action.text, variant: "warning", duration: 8000 })
          void ctx.attention.notify({ title: "Dridh", message: next.human_action.text, notification: { when: "blurred" }, sound: { name: "question", when: "blurred" } })
        }
        lastAction = action ?? ""
        setState(next)
        setUnset(false)
        setError(undefined)
        // After an upgrade the index's vectors can come from an older search model or text: semantic search finds
        // nothing until the code is embedded again, so do that in the background, once per folder per session.
        if (next?.code_index?.vectors_stale && !indexing() && !reindexed.has(folder)) {
          reindexed.add(folder)
          ctx.ui.toast.show({ title: "Dridh", message: "Code search was improved. Re-indexing the code in the background.", variant: "info", duration: 6000 })
          startIndex("upgrade")
        } else if (next?.code_index?.code_changed) {
          // Code changed outside Dridh (an editor, git pull, another tool). Once the files hold still for one refresh
          // (5 s), update the index in the background; only changed files are re-read. A fingerprint whose update
          // failed is not retried until the files change again.
          const fingerprint = String(next.code_index.fingerprint ?? "")
          const seen = changeSeen.get(folder)
          changeSeen.set(folder, fingerprint)
          if (seen === fingerprint && !indexing() && failedFingerprint.get(folder) !== fingerprint) startIndex("changes", fingerprint)
        }
      } catch (e) {
        if (folder !== root()) return
        if ((e as { code?: string }).code === WORKSPACE_NOT_FOUND) {
          setState(undefined)
          setUnset(true)
          setError(undefined)
        } else {
          setError((e as Error).message)
        }
      }
    }
    void refresh()
    const timer = setInterval(refresh, 5000)

    const feature = () => state()?.feature
    const action = () => state()?.human_action

    // ------------------------------------------------------------------ human acts on the real terminal

    async function reviewer(): Promise<string | undefined> {
      const name = await ctx.ui.dialog.prompt({ title: "Reviewer", description: "Recorded with the decision as attribution. Use your own name.", placeholder: "Your name", value: settings.reviewer })
      if (!name?.trim()) return undefined
      await setSettings((draft) => { draft.reviewer = name.trim() })
      return name.trim()
    }

    /** First run in a project: ask whether it is a new or an existing codebase (never guessed), then create the workspace. */
    async function setUp(): Promise<void> {
      const mode = await ctx.ui.dialog.select<"new" | "existing">({
        title: "Set up Dridh",
        options: [
          { title: "New codebase", value: "new", description: "Start from an idea" },
          { title: "Existing codebase", value: "existing", description: "Add to the code here (indexed first)" },
        ],
      })
      if (!mode) return
      try {
        await busyWith("setting up", () => dridhJson(["init", "--mode", mode], root()))
        quiet()
        await refresh()
        if (mode === "existing") startIndex()  // in the background: the status line shows the progress
        else ctx.ui.toast.show({ title: "Dridh is set up", message: START_FEATURE, variant: "success" })
        show({ name: "overview" })
      } catch (e) {
        ctx.ui.toast.show({ title: "Dridh setup failed", message: (e as Error).message, variant: "error" })
      }
    }

    // ------------------------------------------------------------------ indexing in the background

    type Indexing = { folder: string; phase: "files" | "embeddings"; done: number; total: number }
    /** Folders re-indexed this session because their vectors were stale (never retried in a loop). */
    const reindexed = new Set<string>()
    /** The change fingerprint seen at the previous refresh (re-index once it holds still), and one whose update failed. */
    const changeSeen = new Map<string, string>()
    const failedFingerprint = new Map<string, string>()
    const [indexing, setIndexing] = createSignal<Indexing | undefined>(undefined)
    /** The index job for the folder OpenCode is in now, if one is running. */
    const indexingHere = () => (indexing()?.folder === root() ? indexing() : undefined)

    // ------------------------------------------------------------------ "Dridh is working" (spinner)

    /** What Dridh is doing right now: a panel action, or a Dridh tool an agent called (by tool call ID). */
    const [panelWork, setPanelWork] = createSignal<string | undefined>(undefined)
    const [agentWork, setAgentWork] = createSignal<ReadonlyArray<{ id: string; label: string }>>([])
    const toolNames = new Map<string, string>()
    /** The label shown beside the spinner; undefined when Dridh is idle. Indexing has its own progress text. */
    const working = (): string | undefined => panelWork() ?? agentWork().at(-1)?.label ?? (indexingHere() ? "indexing" : undefined)
    const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    const [frame, setFrame] = createSignal(0)
    let spinTimer: ReturnType<typeof setInterval> | undefined
    createEffect(() => {
      const busy = working() !== undefined || indexing() !== undefined
      if (busy && !spinTimer) spinTimer = setInterval(() => setFrame((f) => (f + 1) % SPINNER.length), 100)
      if (!busy && spinTimer) { clearInterval(spinTimer); spinTimer = undefined }
    })
    const spin = () => (working() !== undefined ? `${SPINNER[frame()]} ` : "")

    /** The panel's working line: the spinner and what Dridh is doing, below the list; indexing shows how far it is. */
    const panelWorkText = (): string | undefined => {
      const label = panelWork() ?? agentWork().at(-1)?.label
      if (label) return label
      const job = indexingHere()
      if (!job) return undefined
      return job.total ? `indexing the code · ${job.phase === "files" ? "reading files" : "embedding"} ${Math.floor((100 * job.done) / job.total)}%` : "indexing the code"
    }
    const WorkingLine = () => (
      <Show when={panelWorkText()}><text fg={c.warn}>{`${SPINNER[frame()]} ${panelWorkText()}…`}</text></Show>
    )

    /** Runs a panel action with the spinner and LABEL showing until it settles. */
    async function busyWith<T>(label: string, work: () => Promise<T>): Promise<T> {
      setPanelWork(label)
      try { return await work() } finally { setPanelWork(undefined) }
    }

    /** A short label for a Dridh tool call, from the tool and its operation. */
    function workLabel(tool: string, operation: unknown): string {
      const op = typeof operation === "string" ? operation : ""
      if (tool === "dridh_code") return ({ symbol: "looking up a symbol", dependencies: "following calls", code_for: "mapping code", owners: "mapping code", index_status: "checking the index" } as Record<string, string>)[op] ?? "searching code"
      if (tool === "dridh_jev_decide") return "asking Jev"
      if (op === "code_reacher.index") return "indexing"
      if (op.startsWith("code_reacher.")) return "searching code"
      if (op === "ideator.generate") return "writing use cases"
      if (op === "designer.generate") return "writing the technical design"
      if (op === "planner.generate") return "writing the plan"
      if (op.startsWith("ideator.")) return "use cases"
      if (op.startsWith("designer.")) return "technical design"
      if (op.startsWith("planner.")) return "planning"
      if (op.startsWith("coordinator.")) return "updating the feature"
      return "working"
    }
    const forget = (id: string) => { toolNames.delete(id); setAgentWork((list) => list.filter((w) => w.id !== id)) }
    const stopListening = [
      ctx.data.on("session.tool.input.started", (event: Json) => { if (String(event?.data?.name ?? "").startsWith("dridh_")) toolNames.set(event.data.id, event.data.name) }),
      ctx.data.on("session.tool.called", (event: Json) => {
        const tool = toolNames.get(event?.data?.id)
        if (tool) setAgentWork((list) => [...list.filter((w) => w.id !== event.data.id), { id: event.data.id, label: workLabel(tool, event.data.input?.operation) }])
      }),
      ctx.data.on("session.tool.success", (event: Json) => forget(event?.data?.id)),
      ctx.data.on("session.tool.failed", (event: Json) => forget(event?.data?.id)),
    ]

    /** Runs `dridh index` beside OpenCode; its progress (JSON lines on stderr) feeds the status line and the overview. */
    function startIndex(why: "setup" | "upgrade" | "changes" = "setup", fingerprint = ""): void {
      const folder = root()
      if (indexing()) return ctx.ui.toast.show({ message: "The code is already being indexed.", variant: "info" })
      setIndexing({ folder, phase: "files", done: 0, total: 0 })
      const args = ["index", "--progress", "--json", "--project", folder, ...(why === "changes" ? ["--if-changed"] : [])]
      const child = spawn(bin(), args, { cwd: folder, env: { ...process.env, DRIDH_OUTPUT: "json" } })
      let stdout = ""
      let stderr = ""
      let partial = ""
      child.stdout.on("data", (chunk) => { stdout += chunk })
      child.stderr.on("data", (chunk) => {
        partial += chunk
        const lines = partial.split("\n")
        partial = lines.pop() ?? ""
        for (const line of lines) {
          try {
            const event = JSON.parse(line)
            if (event?.progress === "files" || event?.progress === "embeddings") {
              setIndexing({ folder, phase: event.progress, done: Number(event.done) || 0, total: Number(event.total) || 0 })
              continue
            }
          } catch { /* not a progress line */ }
          stderr += line + "\n"
        }
      })
      child.on("error", () => {
        setIndexing(undefined)
        ctx.ui.toast.show({ title: "Indexing failed", message: missingExecutableMessage(bin()), variant: "error" })
      })
      child.on("close", (code) => {
        setIndexing(undefined)
        void refresh()
        try {
          const result = readCliResult(stdout, stderr + partial, code !== 0) as { files_seen?: number }
          if (why === "changes") return  // a routine update after outside edits: the spinner was enough
          const files = Number(result?.files_seen ?? 0)
          const counted = `${files} file${files === 1 ? "" : "s"}`
          const message = why === "upgrade" ? `Code search is up to date (${counted} re-indexed).` : `Code indexed (${counted}). ${START_FEATURE}`
          ctx.ui.toast.show({ title: "Dridh", message, variant: "success", duration: 8000 })
          void ctx.attention.notify({ title: "Dridh", message, notification: { when: "blurred" }, sound: { name: "done", when: "blurred" } })
        } catch (e) {
          if (why === "changes") failedFingerprint.set(folder, fingerprint)  // not retried until the files change again
          ctx.ui.toast.show({ title: "Indexing failed", message: (e as Error).message, variant: "error" })
        }
      })
    }

    /** /dridh: the overview, or the setup choice when the project has no workspace yet. Reads the current folder first. */
    async function overview(): Promise<void> {
      await refresh()
      if (shownRoot !== root()) await refresh()  // the refresh that was running had started in the previous folder
      if (unset()) void setUp()
      else show({ name: "overview" })
    }

    /** Suspend OpenCode's screen and let the human run a Dridh confirmation command on the real terminal. */
    async function onTerminal(title: string, lines: string[], args: string[]): Promise<void> {
      const go = await ctx.ui.dialog.confirm({
        title,
        message: [...lines, "", "OpenCode steps aside and Dridh asks for the typed confirmation in this terminal. You type it; nothing is typed for you."].join("\n"),
        label: { confirm: "Continue in terminal", cancel: "Cancel" },
      })
      if (!go) return
      quiet()
      const argv = [...args, "--project", root()]
      await ctx.renderer.suspend()
      let code: number | null = null
      try {
        process.stdout.write(`\n\x1b[1mDridh · ${title}\x1b[0m\n$ dridh ${args.join(" ")}\n\n`)
        code = await new Promise<number | null>((resolve) => {
          const script = '"$0" "$@"; code=$?; printf "\\n[dridh exited %s] Press Enter to return to OpenCode " "$code"; read -r _; exit "$code"'
          const child = spawn("sh", ["-c", script, bin(), ...argv], { cwd: root(), stdio: "inherit", env: { ...process.env, DRIDH_OUTPUT: "" } })
          child.on("error", () => resolve(null))
          child.on("exit", (exit) => resolve(exit))
        })
      } finally {
        await ctx.renderer.resume()
      }
      await refresh()
      ctx.ui.toast.show(code === 0
        ? { title, message: "Recorded by Dridh.", variant: "success" }
        : { title, message: "Nothing was changed. Dridh reported a problem in the terminal.", variant: "error" })
    }

    async function approveDocument(stageKey?: string) {
      const docs = state()?.documents ?? {}
      const key = stageKey || action()?.stage_key
      const doc = key ? docs[key] : undefined
      if (!doc) return ctx.ui.toast.show({ message: "Nothing is waiting for approval.", variant: "info" })
      const review = doc.review
      if (!review) return ctx.ui.toast.show({ message: `Open the ${doc.label.toLowerCase()} review first (/dridh-checklist).`, variant: "warning" })
      if (review.ticked < review.total) return ctx.ui.toast.show({ message: `${review.total - review.ticked} checklist item(s) are still unticked.`, variant: "warning" })
      const name = await reviewer()
      if (!name) return
      await onTerminal(`Approve ${doc.label.toLowerCase()} · ${feature()?.slug}`, [
        `Document   ${doc.document}`,
        ...(doc.sha256 ? [`SHA-256    ${String(doc.sha256).slice(0, 16)}…`] : []),
        ...(doc.stories ? [`Stories    ${doc.stories}   Test phases ${doc.test_phases}`] : []),
        `Checklist  ${review.ticked}/${review.total} ticked`,
        ...(doc.jev ? [`Jev        ${doc.jev.decision ?? "–"} (advisory)`] : []),
        `Reviewer   ${name}`,
        "",
        "If the document changes after approval, the approval goes stale.",
      ], [...doc.approve, "--reviewer", name])
    }

    /** The checklist is a view (session panel or page); ticking runs `dridh reviews checklist` for the person. */
    function checklist(stageKey?: string) {
      const key = stageKey || openReviewKey() || action()?.stage_key
      if (!key || !state()?.documents?.[key]) return ctx.ui.toast.show({ message: "No review is open for this feature.", variant: "info" })
      show({ name: "checklist", data: { stage: key } })
    }

    async function lotVerdict(runId: string, lot: number, verdict: "approved" | "rework") {
      let notes = ""
      if (verdict === "rework") {
        notes = (await ctx.ui.dialog.prompt({ title: `Rework lot ${lot}`, description: "What must change. Every story in the lot goes back with these notes.", placeholder: "Notes for the Executor" }))?.trim() ?? ""
        if (!notes) return
      }
      const name = await reviewer()
      if (!name) return
      await onTerminal(`${verdict === "approved" ? "Approve" : "Rework"} lot ${lot} · run ${runId.slice(0, 8)}`,
        [`The verdict applies to the whole lot.`, `Reviewer   ${name}`, ...(notes ? [`Notes      ${notes}`] : [])],
        ["execute", "review", runId, String(lot), "--verdict", verdict, "--reviewer", name, ...(notes ? ["--notes", notes] : [])])
    }

    async function next() {
      const a = action()
      if (!a) return show({ name: "overview" })
      if (a.kind === "stale") return ctx.ui.toast.show({ title: "Dridh", message: a.text, variant: "warning", duration: 10000 })
      if (a.kind === "checklist" || a.kind === "open_review") return checklist(a.stage_key)
      if (a.kind === "approve") return approveDocument(a.stage_key)
      if (a.kind === "advance") {
        quiet()
        try {
          await busyWith("syncing the feature", () => dridhJson(["feature", "sync", feature().slug], root()))
          await refresh()
          return ctx.ui.toast.show({ title: "Dridh", message: `${feature().name} is now in ${feature().stage_label}.`, variant: "success" })
        } catch (e) { return ctx.ui.toast.show({ message: (e as Error).message, variant: "error" }) }
      }
      if (a.kind === "lot") return show({ name: "lot", data: { run: a.run_id, lot: a.lot } })
      if (a.kind === "execute") {
        const name = await reviewer()
        if (!name) return
        return onTerminal(`Start execution · ${feature()?.slug}`, ["Executor agents edit only story worktrees; nothing is pushed.", "The run stops at each checkpoint lot for your verdict."],
          ["execute", "start", feature().slug, "--confirmed-by", name])
      }
      if (a.kind === "deviations") {
        // A deviation from the approved use cases is approved or rejected by a person, on the real terminal.
        const name = await reviewer()
        if (!name) return
        const id = a.ledger_ids[0]
        return onTerminal(`Approve deviation ${id} · ${feature()?.slug}`,
          [a.text, "Type the confirmation to approve. To reject instead: dridh ledger reject " + id + " --reviewer <name> --reason \"...\""],
          ["ledger", "approve", id, "--reviewer", name])
      }
      if (a.kind === "delivery_blocked") return ctx.ui.toast.show({ title: "Dridh", message: a.text, variant: "warning", duration: 10000 })
      if (a.kind === "complete") {
        const name = await reviewer()
        if (!name) return
        return onTerminal(`Complete feature · ${feature()?.slug}`, ["Completion is recorded under your name and cannot be done by an agent."], ["feature", "complete", feature().slug, "--reviewer", name])
      }
      show({ name: "overview" })
    }

    // ------------------------------------------------------------------ where a view opens

    type View = { name: "overview" | "document" | "checklist" | "lot"; data?: Record<string, any> }
    const [panelView, setPanelView] = createSignal<View>({ name: "overview" })

    /**
     * Inside a session a view opens in OpenCode's session panel, so the conversation stays beside it; on the home
     * screen it opens as a full page. OpenCode shows either its sidebar or a panel on the right, never both: closing
     * the panel (esc) brings the sidebar, with its Dridh section, back.
     */
    function show(view: View) {
      if (ctx.ui.router.current().type === "session") {
        setPanelView(view)
        if (ctx.ui.panel.current()?.name === "dridh" || ctx.ui.panel.open("dridh", { presentation: "panel" })) return
      }
      // On the home screen the views open in a dialog, not as a page: leaving a page rebuilds the home screen in the folder
      // OpenCode started in, which undoes a /cd. Closing a dialog leaves OpenCode where it is.
      ctx.ui.dialog.show(() => homeView(view), () => {})
      ctx.ui.dialog.set({ size: "xlarge" })
    }

    // ------------------------------------------------------------------ small pieces

    const Timeline = () => (
      <box flexDirection="column">
        <For each={state()?.timeline ?? []}>
          {(step: Json) => (
            <text fg={step.state === "current" ? c.warn : step.state === "done" ? c.ok : c.muted}>
              {MARK[step.state as keyof typeof MARK]} <span style={{ fg: step.state === "todo" ? c.muted : c.text }}>{step.label}</span>
            </text>
          )}
        </For>
      </box>
    )

    const reviewText = (doc: Json) => {
      if (!doc) return "–"
      if (doc.stale) return "changed after approval"
      const r = doc.review
      if (r?.status === "APPROVED") return `approved${r.approved_by ? ` by ${r.approved_by}` : ""}`
      if (r) return `checklist ${r.ticked}/${r.total}`
      return String(doc.status ?? "").toLowerCase()
    }
    const reviewColour = (doc: Json) => (doc?.stale ? c.bad : doc?.review?.status === "APPROVED" ? c.ok : c.warn)
    const DOCS: [string, string][] = [["1", "use_cases"], ["2", "design"], ["3", "plan"]]
    /** The review a person is working through now: the next step's document, else the first open one. */
    const openReviewKey = () => {
      const docs = state()?.documents ?? {}
      const key = action()?.stage_key
      if (key && docs[key]?.review && docs[key].review.status !== "APPROVED") return key
      return ["use_cases", "design", "plan"].find((k) => docs[k]?.review && docs[k].review.status !== "APPROVED")
    }

    /** Before the first feature: where setup stands, the step in progress and the one thing to do next. */
    const SetupSteps = () => (
      <Show when={!error() && !unset() && state()?.setup} fallback={<text fg={c.muted}>{error() ?? (unset() ? "Type /dridh to set up Dridh here." : "")}</text>}>
        <box flexDirection="column" gap={1}>
          <text><b>Setup · step {indexingHere() ? Math.min(2, state().setup.total) : state().setup.step} of {state().setup.total}</b></text>
          <box flexDirection="column">
            <For each={state().setup.steps}>
              {(step: Json, i) => {
                const running = () => step.id === "index" && indexingHere()
                const mark = () => (running() ? "●" : MARK[step.state as keyof typeof MARK])
                const colour = () => (running() || step.state === "current" ? c.warn : step.state === "done" ? c.ok : c.muted)
                const detail = () => {
                  const job = indexingHere()
                  if (!running() || !job) return ""
                  return job.total ? ` · ${job.phase === "files" ? "files" : "embeddings"} ${job.done} of ${job.total}` : " · starting"
                }
                return <text><span style={{ fg: colour() }}>{` ${mark()} ${i() + 1}  `}</span>{step.label}<span style={{ fg: c.muted }}>{detail()}</span></text>
              }}
            </For>
          </box>
          <WorkingLine />
          <Show when={!indexingHere()}>
            <text fg={c.accent}>{state()?.human_action?.kind === "index" ? "Next: /dridh-index to index the code." : `Next: ${START_FEATURE}`}</text>
          </Show>
        </box>
      </Show>
    )

    // ------------------------------------------------------------------ views (used by the session panel and the full pages)

    function OverviewBody(props: { active: () => boolean; narrow?: boolean }) {
      useKeyboard((key) => {
        if (!props.active()) return
        if (key.name === "n") return void next()
        if (key.name === "c") return void checklist()
        if (key.name === "a") return void approveDocument()
        const doc = { "1": "use_cases", "2": "design", "3": "plan" }[key.name as "1"]
        if (doc && state()?.documents?.[doc]) return show({ name: "document", data: { stage: doc } })
        if (key.name === "l") {
          const run = (state()?.runs ?? []).at(-1)
          if (run?.lots?.length) show({ name: "lot", data: { run: run.id, lot: run.lots.at(-1).ordinal } })
        }
      })
      return (
        <Show when={feature()} fallback={<SetupSteps />}>
          <box flexDirection={props.narrow ? "column" : "row"} gap={props.narrow ? 1 : 4} flexGrow={1}>
            <box flexDirection="column" width={props.narrow ? undefined : 30} flexShrink={0}>
              <text fg={c.muted}>STAGES</text>
              <Timeline />
            </box>
            <box flexDirection="column" flexGrow={1} gap={1}>
              <box flexDirection="column">
                <text fg={c.muted}>DOCUMENTS</text>
                <For each={DOCS}>
                  {([n, key]) => {
                    const doc = () => state()?.documents?.[key]
                    return (
                      <Show when={doc()}>
                        <text><span style={{ fg: c.accent }}>{n}</span>{" " + doc().label.padEnd(18)}
                          <Show when={!props.narrow}><span style={{ fg: c.muted }}>{String(doc().document ?? "").split("/").at(-1).padEnd(24)}</span></Show>
                          <span style={{ fg: reviewColour(doc()) }}>{reviewText(doc())}</span>
                          <Show when={doc().jev?.decision && !props.narrow}><span style={{ fg: c.muted }}>{` · Jev ${doc().jev.decision.toLowerCase()} (advisory)`}</span></Show>
                        </text>
                      </Show>
                    )
                  }}
                </For>
              </box>
              <Show when={(state()?.runs ?? []).length}>
                <box flexDirection="column">
                  <text fg={c.muted}>EXECUTION RUNS</text>
                  <For each={state().runs}>
                    {(run: Json) => (
                      <text>{`run ${run.id.slice(0, 8)}  `}<span style={{ fg: run.status === "COMPLETED" ? c.ok : run.status === "PAUSED" ? c.warn : c.muted }}>{run.status.toLowerCase().padEnd(10)}</span>
                        <span style={{ fg: c.muted }}>{run.lots.map((l: Json) => `lot ${l.ordinal} ${l.status.toLowerCase().replace("_", " ")}`).join(" · ")}</span>
                      </text>
                    )}
                  </For>
                </box>
              </Show>
              <Show when={feature().blockers?.length && feature().stage !== "COMPLETED"}>
                <box flexDirection="column">
                  <text fg={c.muted}>BLOCKERS</text>
                  <For each={feature().blockers}>{(b: string) => <text fg={c.bad}>• {b}</text>}</For>
                </box>
              </Show>
              <box flexDirection="column" border borderColor={action() ? c.warn : c.border} paddingLeft={1} paddingRight={1}>
                <Show when={action()} fallback={<text fg={c.ok}>Nothing needs you right now{feature().stage === "COMPLETED" ? ": the feature is complete." : "."}</text>}>
                  <text fg={c.warn}><b>Next:</b><span style={{ fg: c.text }}>{" " + action().text}</span></text>
                  <text fg={c.muted}>n do it now · c checklist · a approve · l latest lot</text>
                </Show>
              </box>
              <WorkingLine />
            </box>
          </box>
        </Show>
      )
    }

    function ChecklistBody(props: { stage?: string; active: () => boolean }) {
      const key = () => props.stage || openReviewKey() || action()?.stage_key
      const doc = () => (key() ? state()?.documents?.[key()!] : undefined)
      const items = () => doc()?.review?.items ?? []
      const [cursor, setCursor] = createSignal(0)
      const [busyItem, setBusyItem] = createSignal<string>()
      const openIfNeeded = async () => {
        if (doc() && !doc().review && key() === "plan") {
          try { quiet(); await busyWith("opening the review", () => dridhJson(["planner", "review", doc().session_id], root())); await refresh() } catch (e) { ctx.ui.toast.show({ message: (e as Error).message, variant: "error" }) }
        }
      }
      void openIfNeeded()
      const toggle = async () => {
        const it = items()[cursor()]
        if (!it || busyItem()) return
        setBusyItem(it.item)
        try {
          quiet()
          await busyWith("updating the checklist", () => dridhJson(["reviews", "checklist", doc().review.id, it.item, it.checked ? "--unchecked" : "--checked"], root()))
          await refresh()
        } catch (e) { ctx.ui.toast.show({ message: (e as Error).message, variant: "error" }) } finally { setBusyItem(undefined) }
      }
      useKeyboard((k) => {
        if (!props.active()) return
        if (k.name === "down" || k.name === "j") return setCursor((i) => Math.min(i + 1, Math.max(0, items().length - 1)))
        if (k.name === "up" || k.name === "k") return setCursor((i) => Math.max(i - 1, 0))
        if (k.name === "space" || k.name === "x") return void toggle()
        if (k.name === "a") return void approveDocument(key())
        if (k.name === "d" && key()) return show({ name: "document", data: { stage: key() } })
      })
      return (
        <Show when={doc()?.review} fallback={<text fg={c.muted}>{doc() ? "Opening the review…" : "No review is open for this feature."}</text>}>
          <box flexDirection="column" gap={1} flexGrow={1}>
            <text><b>{doc().label} checklist</b><span style={{ fg: c.muted }}>{`  ${doc().review.ticked}/${doc().review.total} ticked · ${String(doc().document ?? "").split("/").at(-1)}`}</span></text>
            <box flexDirection="column">
              <For each={items()}>
                {(it: Json, i) => (
                  <box flexDirection="row" gap={1}>
                    <text fg={i() === cursor() ? c.accent : c.muted} flexShrink={0}>{i() === cursor() ? "▸" : " "}</text>
                    <text fg={it.checked ? c.ok : c.text} flexShrink={0}>{busyItem() === it.item ? "[…]" : it.checked ? "[x]" : "[ ]"}</text>
                    <text fg={i() === cursor() ? c.text : it.checked ? c.muted : c.text}>{it.item}</text>
                  </box>
                )}
              </For>
            </box>
            <Show when={items()[cursor()]?.checked}>
              <text fg={c.muted}>{`ticked by ${String(items()[cursor()].checked_by ?? "").replace(/^cli:/, "")}`}</text>
            </Show>
            <text fg={c.muted}>j/k move · space tick/untick · d read the document · a approve when all are ticked</text>
          </box>
        </Show>
      )
    }

    function DocumentBody(props: { stage: string; active: () => boolean; width: () => number; fullscreen?: () => void }) {
      const [view, setView] = createSignal<Json>(undefined)
      const [problem, setProblem] = createSignal<string>()
      dridhJson(["ui", "document", props.stage], root()).then(setView).catch((e) => setProblem((e as Error).message))
      let scroll: Json
      const [raw, setRaw] = createSignal(false)
      const [rendered, setRendered] = createSignal<Seg[][]>()
      const [renderNote, setRenderNote] = createSignal("raw source")
      const outline = () => props.width() >= 120
      const textWidth = () => Math.max(40, props.width() - (outline() ? 44 : 6))
      createEffect(() => {
        const v = view()
        const width = textWidth()
        if (!v) return
        runGlow(v.display ?? v.text, width, ctx.themeMode)
          .then((out) => { setRendered(parseAnsi(out)); setRenderNote("rendered by glow") })
          .catch(() => setRenderNote("raw source (glow not found)"))
      })
      const lines = createMemo(() => String(view()?.text ?? "").split("\n")) // raw: the document exactly as stored, Mermaid source included
      const showRendered = () => !raw() && rendered() !== undefined
      const plain = (line: Seg[]) => line.map((seg) => seg.text).join("")
      const [section, setSection] = createSignal(0)
      const jump = (heading: Json) => {
        if (!scroll) return
        if (!showRendered()) return scroll.scrollTo(Math.max(0, heading.line - 1))
        const at = rendered()!.findIndex((line) => plain(line).includes(heading.title))
        if (at >= 0) scroll.scrollTo(at)
      }
      useKeyboard((key) => {
        if (!props.active() || !scroll) return
        if (key.name === "down" || key.name === "j") return scroll.scrollBy(1)
        if (key.name === "up" || key.name === "k") return scroll.scrollBy(-1)
        if (key.name === "pagedown" || key.name === "space") return scroll.scrollBy(20)
        if (key.name === "pageup") return scroll.scrollBy(-20)
        const sections = view()?.sections ?? []
        if ((key.name === "]" || key.name === "tab") && sections.length) { const i = Math.min(section() + 1, sections.length - 1); setSection(i); return jump(sections[i]) }
        if (key.name === "[" && sections.length) { const i = Math.max(section() - 1, 0); setSection(i); return jump(sections[i]) }
        if (key.name === "r") return setRaw((v) => !v)
        if ((key.name === "f" || key.name === "g") && props.fullscreen) return props.fullscreen()
        if (key.name === "c") return show({ name: "checklist", data: { stage: props.stage } })
        if (key.name === "a") return void approveDocument(props.stage)
      })
      return (
        <Show when={view()} fallback={<text fg={problem() ? c.bad : c.muted}>{problem() ?? "Loading…"}</text>}>
          <box flexDirection="column" flexGrow={1}>
            <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
              <text><b>{view().label}</b>{"  " + String(view().path).split("/").at(-1) + " "}<span style={{ fg: c.muted }}>{view().sha256 ? `· sha ${view().sha256.slice(0, 8)} ` : ""}</span><span style={{ fg: view().stale ? c.bad : c.muted }}>{view().stale ? "· changed after approval" : `· ${view().status.toLowerCase()}`}</span></text>
              <text fg={c.muted}>{showRendered() ? "glow" : raw() ? "raw source" : renderNote()}</text>
            </box>
            <box flexDirection="row" flexGrow={1} gap={2}>
              <Show when={outline()}>
                <box flexDirection="column" width={38} flexShrink={0}>
                  <text fg={c.muted}>OUTLINE</text>
                  <For each={(view().sections ?? []).filter((s: Json) => s.level <= 2).slice(0, 18)}>
                    {(s: Json) => <text fg={view().sections[section()]?.line === s.line ? c.accent : c.text}>{s.level === 2 ? "  " : ""}{s.title.slice(0, 34)}</text>}
                  </For>
                  <text> </text>
                  <text fg={c.muted}>VALIDATION</text>
                  <text fg={view().validation.ok ? c.ok : view().validation.ok === false ? c.bad : c.warn}>
                    {view().validation.ok ? "passes" : `${view().validation.errors.length} error(s)`}<span style={{ fg: c.muted }}>{` · ${view().validation.warnings.length} warning(s)`}</span>
                  </text>
                  <Show when={view().jev}>
                    <text fg={c.muted}>JEV (advisory)</text>
                    <text>{String(view().jev.decision ?? "–").toLowerCase()}<span style={{ fg: c.muted }}>{` · ${String(view().jev.detail ?? "").slice(0, 26)}`}</span></text>
                  </Show>
                  <Show when={view().concerns.length}>
                    <text fg={c.muted}>CONCERNS ({view().concerns.length})</text>
                    <For each={view().concerns.slice(0, 3)}>{(x: string) => <text fg={c.warn}>• {x.slice(0, 34)}…</text>}</For>
                  </Show>
                  <Show when={view().diagrams?.drawn || view().diagrams?.source_only}>
                    <text fg={c.muted}>DIAGRAMS</text>
                    <text>{view().diagrams.drawn} drawn as text<span style={{ fg: c.muted }}>{view().diagrams.source_only ? ` · ${view().diagrams.source_only} shown as source` : ""}</span></text>
                  </Show>
                  <Show when={view().review}>
                    <text fg={c.muted}>REVIEW</text>
                    <text fg={view().review.status === "APPROVED" ? c.ok : c.warn}>{view().review.status.toLowerCase()} · {view().review.ticked}/{view().review.total}</text>
                  </Show>
                </box>
              </Show>
              <scrollbox ref={(r: Json) => (scroll = r)} flexGrow={1} border borderColor={c.border} paddingLeft={1}>
                <box flexDirection="column">
                  <Show when={showRendered()} fallback={<For each={lines()}>{(line: string) => <text fg={/^#{1,3}\s/.test(line) ? c.heading : c.text}>{line || " "}</text>}</For>}>
                    <For each={rendered()}>
                      {(line: Seg[]) => (
                        <text>
                          <Show when={line.length} fallback={" "}>
                            {/* spans are styled through `style`, as OpenCode's own are; TextAttributes bits: bold 1, italic 4, underline 8 */}
                            <For each={line}>{(seg: Seg) => <span style={{ fg: seg.fg, bg: seg.bg, attributes: (seg.bold ? 1 : 0) | (seg.italic ? 4 : 0) | (seg.underline ? 8 : 0) }}>{seg.text}</span>}</For>
                          </Show>
                        </text>
                      )}
                    </For>
                  </Show>
                </box>
              </scrollbox>
            </box>
            <text fg={c.muted} flexShrink={0}>{`j/k scroll · ]/[ section · r ${raw() ? "rendered" : "raw"}${props.fullscreen ? " · f full screen" : ""} · c checklist · a approve`}</text>
          </box>
        </Show>
      )
    }

    function LotBody(props: { run: string; lot: number; active: () => boolean; narrow?: boolean }) {
      const [view, setView] = createSignal<Json>(undefined)
      const [problem, setProblem] = createSignal<string>()
      const [index, setIndex] = createSignal(0)
      const load = () => dridhJson(["ui", "lot", props.run, String(props.lot)], root()).then(setView).catch((e) => setProblem((e as Error).message))
      void load()
      let scroll: Json
      const story = () => view()?.stories?.[index()]
      const awaiting = () => view()?.lot?.status === "AWAITING_REVIEW" && view()?.run?.status === "PAUSED"
      useKeyboard((key) => {
        if (!props.active()) return
        const lots = (state()?.runs ?? []).find((r: Json) => r.id === props.run)?.lots ?? []
        const at = lots.findIndex((l: Json) => l.ordinal === props.lot)
        if ((key.name === "]" && at < lots.length - 1) || (key.name === "[" && at > 0)) {
          return show({ name: "lot", data: { run: props.run, lot: lots[at + (key.name === "]" ? 1 : -1)].ordinal } })
        }
        if (key.name === "tab") return setIndex((i) => (i + 1) % Math.max(1, view()?.stories?.length ?? 1))
        if (scroll && (key.name === "down" || key.name === "j")) return scroll.scrollBy(1)
        if (scroll && (key.name === "up" || key.name === "k")) return scroll.scrollBy(-1)
        if (scroll && (key.name === "pagedown" || key.name === "space")) return scroll.scrollBy(20)
        if (scroll && key.name === "pageup") return scroll.scrollBy(-20)
        if (awaiting() && key.name === "a") return void lotVerdict(props.run, props.lot, "approved").then(load)
        if (awaiting() && key.name === "w") return void lotVerdict(props.run, props.lot, "rework").then(load)
      })
      const diffColour = (line: string) => line.startsWith("+++") || line.startsWith("---") ? c.muted : line.startsWith("+") ? c.add : line.startsWith("-") ? c.del : line.startsWith("@@") ? c.accent : c.text
      const Evidence = () => (
        <Show when={story()}>
          <text fg={c.muted}>STORY {story().story_id}</text>
          <text>attempt {story().attempts} · {String(story().kind ?? "").toLowerCase()} · commit <span style={{ fg: c.accent }}>{String(story().commit ?? "–").slice(0, 8)}</span></text>
          <text>tests <span style={{ fg: story().tests.exit_code === 0 ? c.ok : c.bad }}>{story().tests.exit_code === 0 ? "passed" : story().tests.exit_code == null ? "not run" : `exit ${story().tests.exit_code}`}</span>
            {" · new mocks "}<span style={{ fg: story().new_mocks.length ? c.bad : c.ok }}>{String(story().new_mocks.length)}</span>{` · files ${story().changed_files.length}`}</text>
          <Show when={story().findings.length}><text fg={c.warn}>findings {story().findings.length}</text></Show>
          <Show when={story().summary}><text fg={c.muted}>{String(story().summary).slice(0, 200)}</text></Show>
        </Show>
      )
      return (
        <Show when={view()} fallback={<text fg={problem() ? c.bad : c.muted}>{problem() ?? "Loading…"}</text>}>
          <box flexDirection="column" flexGrow={1}>
            <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
              <text><b>{`Run ${view().run.id.slice(0, 8)} · lot ${view().lot.ordinal} ${view().lot.kind.toLowerCase()}`}</b><span style={{ fg: c.muted }}>{" · " + view().stories.map((s: Json) => s.story_id).join(", ")}</span></text>
              <text fg={awaiting() ? c.warn : view().lot.status === "APPROVED" ? c.ok : c.muted}>{awaiting() ? "awaits your verdict" : view().lot.status.toLowerCase().replace("_", " ")}</text>
            </box>
            <box flexDirection={props.narrow ? "column" : "row"} flexGrow={1} gap={props.narrow ? 0 : 2}>
              <box flexDirection="column" width={props.narrow ? undefined : 44} flexShrink={0}>
                <For each={view().stories}>
                  {(s: Json, i) => <text fg={i() === index() ? c.accent : c.text}>{i() === index() ? "▸" : " "} {s.story_id} <span style={{ fg: s.status === "PASSED" ? c.ok : c.warn }}>{String(s.status ?? "–").toLowerCase()}</span></text>}
                </For>
                <Evidence />
              </box>
              <scrollbox ref={(r: Json) => (scroll = r)} flexGrow={1} border borderColor={c.border} paddingLeft={1}>
                <box flexDirection="column">
                  <For each={String(story()?.diff || "No commit recorded for this story.").split("\n")}>{(line: string) => <text fg={diffColour(line)}>{line || " "}</text>}</For>
                </box>
              </scrollbox>
            </box>
            <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
              <text fg={c.muted}>tab story · ]/[ lot · j/k scroll</text>
              <Show when={awaiting()} fallback={<text fg={c.muted}>no verdict needed</text>}>
                <text><span style={{ fg: c.text }}>{"w rework…   "}</span><span style={{ fg: c.accent }}><b>a approve lot…</b></span></text>
              </Show>
            </box>
          </box>
        </Show>
      )
    }

    // ------------------------------------------------------------------ sidebar and status line

    /** One clickable sidebar line: a mouse click or the named command does the same thing. */
    const Action = (props: { label: string; hint: string; run: () => void; colour?: string }) => (
      <box flexDirection="row" justifyContent="space-between" onMouseUp={() => props.run()}>
        <text fg={props.colour ?? c.text}>{"› " + props.label}</text>
        <text fg={c.muted}>{props.hint}</text>
      </box>
    )

    ctx.ui.slot({
      append: "sidebar.content",
      render: () => (
        <Show when={feature()}>
          <box flexDirection="column" paddingTop={1} gap={1}>
            <box flexDirection="column">
              <text fg={c.accent}><b>DRIDH</b><span style={{ fg: c.muted }}>{state().mode === "existing" ? "  existing codebase" : "  new codebase"}</span></text>
              <text><b>{feature().name}</b></text>
              <text fg={c.muted}>{feature().stage_label}{feature().status === "BLOCKED" ? " · blocked" : ""}</text>
            </box>
            <box flexDirection="column">
              <text fg={c.muted}>{`STAGE ${Math.max(1, (state()?.timeline ?? []).findIndex((s: Json) => s.state === "current") + 1 || (state()?.timeline ?? []).length)} OF ${(state()?.timeline ?? []).length}`}</text>
              <text>
                <For each={state()?.timeline ?? []}>{(step: Json) => <span style={{ fg: step.state === "current" ? c.warn : step.state === "done" ? c.ok : c.muted }}>{MARK[step.state as keyof typeof MARK]}</span>}</For>
              </text>
            </box>
            <box flexDirection="column">
              <text fg={c.muted}>DOCUMENTS</text>
              <For each={DOCS}>
                {([, key]) => {
                  const doc = () => state()?.documents?.[key]
                  return (
                    <Show when={doc()}>
                      <box flexDirection="row" justifyContent="space-between" onMouseUp={() => show({ name: "document", data: { stage: key } })}>
                        <text>{doc().label}</text>
                        <text fg={reviewColour(doc())}>{doc().stale ? "changed" : doc().review?.status === "APPROVED" ? "✓" : doc().review ? `${doc().review.ticked}/${doc().review.total}` : "–"}</text>
                      </box>
                    </Show>
                  )
                }}
              </For>
            </box>
            <Show when={openReviewKey()}>
              {(() => {
                const doc = () => state().documents[openReviewKey()!]
                return (
                  <box flexDirection="column">
                    <text fg={c.muted}>{`CHECKLIST · ${doc().label.toUpperCase()} ${doc().review.ticked}/${doc().review.total}`}</text>
                    <For each={doc().review.items.slice(0, 12)}>
                      {(it: Json) => <text fg={it.checked ? c.muted : c.text}><span style={{ fg: it.checked ? c.ok : c.warn }}>{it.checked ? "✓ " : "○ "}</span>{it.item.length > 34 ? it.item.slice(0, 33) + "…" : it.item}</text>}
                    </For>
                  </box>
                )
              })()}
            </Show>
            <Show when={action()}>
              <box flexDirection="column">
                <text fg={action().kind === "stale" ? c.bad : c.warn}><b>Next</b></text>
                <text>{action().text}</text>
              </box>
            </Show>
            <box flexDirection="column">
              <text fg={c.muted}>ACTIONS</text>
              <Show when={action() && !["wait", "stale"].includes(action().kind)}><Action label="Do the next step" hint="/dridh-next" colour={c.warn} run={() => void next()} /></Show>
              <Action label="Overview" hint="/dridh" run={() => show({ name: "overview" })} />
              <Show when={openReviewKey()}><Action label="Checklist" hint="" run={() => show({ name: "checklist" })} /></Show>
              <Action label="Read a document" hint="" run={() => show({ name: "document", data: { stage: openReviewKey() ?? action()?.stage_key ?? "design" } })} />
              <Show when={(state()?.runs ?? []).length}><Action label="Execution lots" hint="" run={() => openLot()} /></Show>
            </box>
          </box>
        </Show>
      ),
    })

    /** The status line before the first feature: setup step, indexing progress and the next action, shortest first. */
    const setupLine = (sessionID?: string) => {
      const setup = state()?.setup
      const job = indexingHere()
      const step = setup ? `setup ${job ? Math.min(2, setup.total) : setup.step}/${setup.total}` : "ready"
      if (job) {
        const what = job.phase === "files" ? "indexing" : "embedding"
        const count = job.total ? ` ${job.done}/${job.total}` : ""
        return `${spin()}dridh · ${step} · ${what}${count}${job.phase === "files" && job.total ? " files" : ""}`
      }
      if (working()) return `${spin()}dridh · ${step} · ${working()}`
      if (state()?.human_action?.kind === "index") return `dridh · ${step} · /dridh-index to index the code`
      const agent = sessionID ? (ctx.data.session.get(sessionID) as { agent?: string } | undefined)?.agent : undefined
      return `dridh · ${step} · ${agent === "dridh/coordinator" ? "describe a feature to start" : "Shift+Tab: Dridh/Coordinator, describe a feature"}`
    }
    const statusLine = (short = false, sessionID?: string) => (
      <Show when={feature()} fallback={
        <Show when={!error()} fallback={<text fg={c.bad}>dridh · {error()!.slice(0, 72)}</text>}>
          <Show when={unset()} fallback={<Show when={state()} fallback={<Show when={working()}><text fg={c.muted}>{spin()}dridh · {working()}</text></Show>}><text fg={c.muted}>{setupLine(sessionID)}</text></Show>}>
            <text fg={c.muted}>{working() ? `${spin()}dridh · ${working()}` : short ? "dridh · not set up" : "dridh · type /dridh to set up"}</text>
          </Show>
        </Show>}>
        <text fg={c.accent}>{spin()}dridh <span style={{ fg: c.muted }}>·</span> {feature().slug} <span style={{ fg: c.muted }}>·</span> {feature().stage_label}
          <Show when={working()}><span style={{ fg: c.muted }}>{` · ${working()}`}</span></Show>
          <Show when={!working() && action() && action().kind !== "wait"}><span style={{ fg: c.warn }}>{short ? " · ⚑ needs you" : ` · ⚑ ${action().text}`}</span></Show>
        </text>
      </Show>
    )
    ctx.ui.slot({ append: "home.footer.status", render: () => statusLine() })
    ctx.ui.slot({ append: "prompt.footer.status", render: (input) => <Show when={input.sessionID}>{statusLine(true, input.sessionID)}</Show> })

    // ------------------------------------------------------------------ the session panel

    const TABS: [View["name"], string][] = [["overview", "Overview"], ["document", "Document"], ["checklist", "Checklist"], ["lot", "Lots"]]

    ctx.ui.slot({
      append: "session.panel",
      render: (input) => (
        <Show when={input.name === "dridh"}>
          {(() => {
            const active = () => input.focused
            useKeyboard((key) => {
              if (!input.focused) return
              if (key.name === "escape") return input.close()
              const tab = { o: "overview", ...(panelView().name === "document" ? {} : { d: "document" }), h: "checklist", ...(panelView().name === "lot" ? {} : { l: "lot" }) }[key.name as "o"] as View["name"] | undefined
              if (tab && tab !== panelView().name) {
                if (tab === "lot") return openLot()
                if (tab === "document") return show({ name: "document", data: { stage: openReviewKey() ?? action()?.stage_key ?? "design" } })
                return show({ name: tab })
              }
              if (key.name === "f" && panelView().name !== "document") return input.toggleFullscreen()
            })
            return (
              <box flexDirection="column" flexGrow={1} paddingLeft={1} paddingRight={1} gap={1}>
                <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
                  <text>
                    <span style={{ fg: c.accent, attributes: 1 }}>{"Dridh  "}</span>
                    <For each={TABS}>{([name, label]) => <span style={{ fg: panelView().name === name ? c.accent : c.muted, attributes: panelView().name === name ? 1 : 0 }}>{` ${label} `}</span>}</For>
                  </text>
                  <text fg={c.muted}>{`${input.presentation === "fullscreen" ? "f panel" : "f full screen"} · esc close`}</text>
                </box>
                <Show when={panelView().name === "overview"}><OverviewBody active={active} narrow={input.width < 110} /></Show>
                <Show when={panelView().name === "checklist"}><ChecklistBody stage={panelView().data?.stage} active={active} /></Show>
                <Show when={panelView().name === "document" && panelView().data?.stage} keyed>
                  {(stage: string) => <DocumentBody stage={stage} active={active} width={() => input.width} fullscreen={() => input.toggleFullscreen()} />}
                </Show>
                <Show when={panelView().name === "lot" && panelView().data ? `${panelView().data!.run}:${panelView().data!.lot}` : undefined} keyed>
                  {(ref: string) => <LotBody run={ref.split(":")[0]} lot={Number(ref.split(":")[1])} active={active} narrow={input.width < 110} />}
                </Show>
              </box>
            )
          })()}
        </Show>
      ),
    })

    // ------------------------------------------------------------------ full pages (home screen, no session)

    const Page = (props: { title: string; children: Json; close?: () => void }) => {
      useKeyboard((key) => {
        if (key.name !== "escape" && key.name !== "q") return
        if (props.close) props.close()
        else ctx.ui.router.navigate({ type: "home" })
      })
      return (
        <box flexDirection="column" flexGrow={1} paddingLeft={2} paddingRight={2} paddingTop={1} gap={1}>
          <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
            <text><span style={{ fg: c.accent, attributes: 1 }}>Dridh</span>{"   "}<b>{feature()?.name ?? ""}</b><span style={{ fg: c.muted }}>{feature() ? ` · ${feature().stage_label} · ${props.title}` : ""}</span></text>
            <text fg={c.muted}>esc back</text>
          </box>
          {props.children}
        </box>
      )
    }
    const always = () => true
    ctx.ui.router.register({ name: "overview", render: () => <Page title="overview"><OverviewBody active={always} /></Page> })
    ctx.ui.router.register({ name: "checklist", render: (input) => <Page title="checklist"><ChecklistBody stage={input.data?.stage} active={always} /></Page> })
    ctx.ui.router.register({ name: "document", render: (input) => <Page title="document"><DocumentBody stage={input.data?.stage ?? "use_cases"} active={always} width={() => (ctx.renderer.width ?? 160) - 4} /></Page> })
    ctx.ui.router.register({ name: "lot", render: (input) => <Page title="execution"><LotBody run={input.data?.run} lot={input.data?.lot} active={always} /></Page> })

    /** A view in the home-screen dialog: the same bodies as the pages, closed with esc or q. */
    function homeView(view: View): Json {
      const close = () => ctx.ui.dialog.clear()
      const width = () => Math.max(60, Math.floor((ctx.renderer.width ?? 160) * 0.8) - 8)
      if (view.name === "checklist") return <Page title="checklist" close={close}><ChecklistBody stage={view.data?.stage} active={always} /></Page>
      if (view.name === "document") return <Page title="document" close={close}><DocumentBody stage={view.data?.stage ?? "use_cases"} active={always} width={width} /></Page>
      if (view.name === "lot") return <Page title="execution" close={close}><LotBody run={view.data?.run} lot={view.data?.lot} active={always} /></Page>
      return <Page title="overview" close={close}><OverviewBody active={always} /></Page>
    }

    function openLot() {
      const a = action()
      const run = a?.kind === "lot" ? { id: a.run_id, lot: a.lot } : (() => { const r = (state()?.runs ?? []).at(-1); return r?.lots?.length ? { id: r.id, lot: r.lots.at(-1).ordinal } : undefined })()
      if (!run) return ctx.ui.toast.show({ message: "No execution run yet.", variant: "info" })
      show({ name: "lot", data: { run: run.id, lot: run.lot } })
    }

    // ------------------------------------------------------------------ commands (palette and slash)

    // Registered from a rendered slot in global mode, as OpenCode's own /btw command is.
    ctx.ui.slot({ append: "app", render: () => { ctx.keymap.layer(commands); return null } })
    function commands() { return {
      mode: "global",
      commands: [
        { id: "dridh.overview", title: "Dridh: overview", description: "Feature stages, documents, runs and what needs you", group: "Dridh", palette: true, slash: { name: "dridh" },
          run: () => void overview() },
        { id: "dridh.index", title: "Dridh: index the code", description: "Build or update the Code Reacher index in the background", group: "Dridh", palette: true, slash: { name: "dridh-index" },
          run: () => startIndex() },
        { id: "dridh.next", title: "Dridh: do the next human step", description: "Checklist, approval, lot review, start or complete", group: "Dridh", palette: true, slash: { name: "dridh-next" },
          run: () => void next() },
        { id: "dridh.review", title: "Dridh: read a document", description: "Use Cases, technical design or plan", group: "Dridh", palette: true,
          slash: { name: "dridh-review", arguments: true },
          run: (raw?: string) => {
            const word = String(raw ?? "").replace(/^\/?dridh-review\s*/, "").trim().toLowerCase()
            const stage = word.startsWith("use") || word === "uc" ? "use_cases" : word.startsWith("tdd") || word.startsWith("design") ? "design" : word.startsWith("plan") ? "plan"
              : openReviewKey() ?? action()?.stage_key ?? (["plan", "design", "use_cases"].find((k) => state()?.documents?.[k]) || "use_cases")
            show({ name: "document", data: { stage } })
          } },
        { id: "dridh.checklist", title: "Dridh: review checklist", group: "Dridh", palette: true, slash: { name: "dridh-checklist" }, run: () => void checklist() },
        { id: "dridh.approve", title: "Dridh: approve the document under review", group: "Dridh", palette: true, slash: { name: "dridh-approve" }, run: () => void approveDocument() },
        { id: "dridh.lot", title: "Dridh: execution lot review", group: "Dridh", palette: true, slash: { name: "dridh-lot" }, run: () => openLot() },
      ],
    } }

    return () => {
      clearInterval(timer)
      if (spinTimer) clearInterval(spinTimer)
      for (const stop of stopListening) stop()
    }
  },
})
