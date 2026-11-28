/**
 * The Dridh side panel: the feature's progress, its documents, and the document reading view with its review
 * checklist, Approve and Request changes. Built from the app's own components (ctx.ui) and theme colours, so it
 * matches OpenCode in light and dark mode. Diagrams in documents are drawn by the app's Markdown renderer.
 */
import type { IpcClient, MountedSession, Persisted, SessionScreen, SetupContext } from "@opencode/plugin/gui"
import { createEffect, createMemo, createSignal, For, Match, on, onCleanup, Show, Switch, type JSX } from "solid-js"
import { type Challenge, type Failure, failure, readReply } from "./commands.ts"
import type { DridhDesktop } from "./contract.ts"

type Json = any // eslint-disable-line @typescript-eslint/no-explicit-any -- `dridh ui` JSON, documented in the CLI

export interface Env {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- the panel declares no stores or icons
  readonly ctx: SetupContext<any>
  readonly desk: IpcClient<(typeof DridhDesktop)["spec"]>
  readonly reviewer: Persisted<{ readonly name: string }>
}

/** The order documents appear in, and the noun used on buttons. */
const DOCUMENTS: readonly { key: string; noun: string }[] = [
  { key: "use_cases", noun: "use cases" },
  { key: "design", noun: "technical design" },
  { key: "plan", noun: "plan" },
]
const STAGES_WITH_DOCUMENT_ACTION = new Set(["checklist", "open_review", "approve"])

type Tone = "success" | "attention" | "danger" | "neutral"
interface Status { readonly label: string; readonly tone: Tone }

/** A document's status in words a person reads at a glance. */
export function documentStatus(doc: Json): Status {
  if (doc?.stale) return { label: "Changed since approval", tone: "danger" }
  const review = doc?.review
  if (doc?.status === "APPROVED" || review?.status === "APPROVED") return { label: "Approved", tone: "success" }
  if (review?.status === "OPEN") return { label: "Waiting for your review", tone: "attention" }
  if (review?.status === "REJECTED") return { label: "Changes requested", tone: "danger" }
  return { label: "Being written", tone: "neutral" }
}

const ICON: Record<Tone, string> = { success: "circle-check", attention: "circle-exclamation", danger: "warning", neutral: "file-document" }

function Pill(props: { status: Status; env: Env }) {
  const ui = props.env.ctx.ui
  return (
    <span class={`dridh-pill dridh-pill--${props.status.tone}`}>
      <ui.Icon name={ICON[props.status.tone] as never} size="small" />
      {props.status.label}
    </span>
  )
}

function Problem(props: { error: Failure }) {
  return (
    <div class="dridh-callout dridh-callout--danger" role="alert">
      <strong>{props.error.message}</strong>
      <Show when={props.error.recovery}><span>{props.error.recovery}</span></Show>
    </div>
  )
}

/** Reads `dridh ui state` for the session's folder now and every 5 seconds while the panel is open. */
function useDridhState(env: Env, project: () => string) {
  const [state, setState] = createSignal<Json>()
  const [error, setError] = createSignal<Failure>()
  const [loading, setLoading] = createSignal(true)
  let alive = true
  const load = async () => {
    const folder = project()
    const reply = readReply(await env.desk.read({ project: folder, args: ["ui", "state"] }).catch((e: unknown) => failure(e)))
    if (!alive || folder !== project()) return
    if (reply.ok) { setState(reply.data); setError(undefined) } else { setError(reply.error); if (reply.error.code === "WORKSPACE_NOT_FOUND") setState(undefined) }
    setLoading(false)
  }
  createEffect(on(project, () => { setLoading(true); setState(undefined); void load() }))
  const timer = setInterval(() => void load(), 5000)
  onCleanup(() => { alive = false; clearInterval(timer) })
  return { state, error, loading, refresh: load }
}

export function DridhPanel(props: { env: Env; session: MountedSession; screen: SessionScreen }) {
  const ui = props.env.ctx.ui
  const project = () => props.session.directory
  const live = useDridhState(props.env, project)
  const [open, setOpen] = createSignal<string>() // the document being read, by stage key
  createEffect(on(project, () => setOpen(undefined)))
  return (
    <div class="dridh">
      <Switch>
        <Match when={live.loading() && !live.state()}>
          <div class="dridh-center"><ui.Loader size="large" /><span>Reading Dridh…</span></div>
        </Match>
        <Match when={live.error()?.code === "WORKSPACE_NOT_FOUND"}>
          <div class="dridh-empty">
            <div class="dridh-eyebrow">Dridh</div>
            <h1 class="dridh-title">Dridh is not set up in this project</h1>
            <p class="dridh-muted">Set it up from the terminal with <code>dridh init</code>, or type <code>/dridh</code> in the OpenCode terminal.</p>
          </div>
        </Match>
        <Match when={!live.state() && live.error()}>
          <div class="dridh-pad"><Problem error={live.error()!} /></div>
        </Match>
        <Match when={open() && live.state()?.documents?.[open()!]}>
          <DocumentView env={props.env} project={project()} stage={open()!} state={live.state()} screen={props.screen}
            onBack={() => setOpen(undefined)} refresh={live.refresh} />
        </Match>
        <Match when={live.state()}>
          <Overview env={props.env} state={live.state()} onOpen={setOpen} />
        </Match>
      </Switch>
    </div>
  )
}

function Overview(props: { env: Env; state: Json; onOpen: (stage: string) => void }) {
  const ui = props.env.ctx.ui
  const feature = () => props.state.feature
  const timeline = (): Json[] => props.state.timeline ?? []
  const current = () => Math.max(0, timeline().findIndex((step) => step.state === "current"))
  const action = () => props.state.human_action
  const docs = () => DOCUMENTS.filter((d) => props.state.documents?.[d.key]).map((d) => ({ ...d, doc: props.state.documents[d.key] }))
  return (
    <div class="dridh-pad dridh-stack">
      <header class="dridh-head">
        <div class="dridh-eyebrow">Dridh</div>
        <h1 class="dridh-title">{feature()?.name ?? "No feature yet"}</h1>
        <Show when={feature()}>
          <p class="dridh-muted">{feature().stage_label} · stage {current() + 1} of {timeline().length}</p>
        </Show>
      </header>

      <Show when={timeline().length}>
        <ol class="dridh-track" aria-label="Feature stages">
          <For each={timeline()}>
            {(step) => <li class={`dridh-track__step dridh-track__step--${step.state}`} title={`${step.label}: ${step.state === "done" ? "done" : step.state === "current" ? "now" : "to do"}`} />}
          </For>
        </ol>
      </Show>

      <Show when={action()}>
        <section class={`dridh-next ${STAGES_WITH_DOCUMENT_ACTION.has(action().kind) ? "dridh-next--you" : ""}`}>
          <div class="dridh-eyebrow">{STAGES_WITH_DOCUMENT_ACTION.has(action().kind) ? "Your turn" : "Next step"}</div>
          <p>{action().text}</p>
          <Show when={STAGES_WITH_DOCUMENT_ACTION.has(action().kind) && action().stage_key}>
            <div><ui.Button variant="contrast" icon="file-document" onClick={() => props.onOpen(action().stage_key)}>
              Review the {DOCUMENTS.find((d) => d.key === action().stage_key)?.noun ?? "document"}
            </ui.Button></div>
          </Show>
        </section>
      </Show>

      <section class="dridh-stack dridh-stack--tight">
        <h2 class="dridh-h2">Documents</h2>
        <Show when={docs().length} fallback={<p class="dridh-muted">No documents yet. They appear here as the agents write them.</p>}>
          <For each={docs()}>
            {(item) => (
              <button type="button" class="dridh-doc-card" onClick={() => props.onOpen(item.key)}>
                <span class="dridh-doc-card__icon"><ui.Icon name="file-document" /></span>
                <span class="dridh-doc-card__body">
                  <span class="dridh-doc-card__title">{item.doc.label}</span>
                  <span class="dridh-mono dridh-muted">{String(item.doc.document ?? "").split("/").at(-1)}</span>
                  <Show when={item.doc.review?.status === "OPEN" && item.doc.review.total}>
                    <Meter done={item.doc.review.ticked} total={item.doc.review.total} />
                  </Show>
                </span>
                <Pill status={documentStatus(item.doc)} env={props.env} />
              </button>
            )}
          </For>
        </Show>
      </section>
    </div>
  )
}

function Meter(props: { done: number; total: number }) {
  return (
    <span class="dridh-meter" role="progressbar" aria-valuemin={0} aria-valuemax={props.total} aria-valuenow={props.done}>
      <span class="dridh-meter__bar"><span style={{ width: `${props.total ? (100 * props.done) / props.total : 0}%` }} /></span>
      <span class="dridh-meter__text">{props.done} of {props.total} checked</span>
    </span>
  )
}

function DocumentView(props: {
  env: Env; project: string; stage: string; state: Json; screen: SessionScreen; onBack: () => void; refresh: () => Promise<void>
}) {
  const { ctx, desk } = props.env
  const ui = ctx.ui
  const summary = () => props.state.documents[props.stage] // from ui state: refreshed every 5 s
  const [doc, setDoc] = createSignal<Json>()
  const [text, setText] = createSignal<string>()
  const [error, setError] = createSignal<Failure>()
  const [busyItem, setBusyItem] = createSignal<string>()
  const [tocOpen, setTocOpen] = createSignal(false)
  let body: HTMLElement | undefined
  let checklistCard: HTMLElement | undefined

  const load = async () => {
    const reply = readReply<Json>(await desk.read({ project: props.project, args: ["ui", "document", props.stage] }).catch((e: unknown) => failure(e)))
    if (reply.ok) {
      setDoc(reply.data)
      if (reply.data?.text !== text()) setText(String(reply.data?.text ?? "")) // re-draw the text only when it changed
      setError(undefined)
    } else setError(reply.error)
  }
  // Reload when the document or its review changes (a new revision, a tick from the terminal, an approval).
  createEffect(on(() => `${summary()?.sha256}|${summary()?.review?.ticked}|${summary()?.review?.status}|${summary()?.status}`, () => void load()))

  const review = () => doc()?.review
  const status = () => documentStatus(summary())
  const noun = () => DOCUMENTS.find((d) => d.key === props.stage)?.noun ?? "document"
  const remaining = () => (review() ? review().total - review().ticked : 0)
  const reviewOpen = () => review()?.status === "OPEN"
  const sections = createMemo((): Json[] => (doc()?.sections ?? []).filter((s: Json) => s.level >= 2 && s.level <= 3))

  const jump = (title: string) => {
    const headings = body?.querySelectorAll("h1, h2, h3, h4") ?? []
    const target = [...headings].find((h) => h.textContent?.trim() === title) ?? [...headings].find((h) => h.textContent?.includes(title))
    target?.scrollIntoView({ behavior: "smooth", block: "start" })
    setTocOpen(false)
  }

  const tick = async (item: Json) => {
    if (busyItem() || !reviewOpen()) return
    setBusyItem(item.item)
    const reply = readReply(await desk.record({ project: props.project, args: ["reviews", "checklist", review().id, item.item, item.checked ? "--unchecked" : "--checked"] }).catch((e: unknown) => failure(e)))
    if (!reply.ok) ctx.toast.show({ title: "Could not update the checklist", description: reply.error.message, variant: "error" })
    await load()
    void props.refresh()
    setBusyItem(undefined)
  }

  /** Diagrams are drawn small in a narrow panel: a click opens one large, with its title from the nearest heading. */
  const enlarge = (event: MouseEvent) => {
    const svg = (event.target as Element | null)?.closest?.("svg[aria-roledescription]")
    if (!svg) return
    let heading: Element | null = svg
    while (heading && !/^H[1-4]$/.test(heading.tagName)) heading = heading.previousElementSibling ?? heading.parentElement
    const title = heading && heading !== svg ? heading.textContent?.trim() || "Diagram" : "Diagram"
    const copy = svg.cloneNode(true) as SVGElement
    copy.removeAttribute("width")
    copy.removeAttribute("height")
    copy.style.maxWidth = "100%"
    copy.style.width = "100%"
    copy.style.height = "auto"
    ctx.dialogs.open(() => (
      <ui.Dialog title={title} size="x-large"><div class="dridh-diagram">{copy}</div></ui.Dialog>
    ))
  }

  const approve = () => ctx.dialogs.open((dialog) => (
    <ApproveDialog env={props.env} project={props.project} noun={noun()} doc={doc()} approve={summary().approve}
      close={() => dialog.close()} done={async () => { await props.refresh(); await load() }} />
  ))

  const requestChanges = () => ctx.dialogs.open((dialog) => (
    <ChangesDialog env={props.env} noun={noun()} file={String(doc()?.path ?? summary()?.document ?? "")} screen={props.screen} close={() => dialog.close()} />
  ))

  return (
    <div class="dridh-doc">
      <div class="dridh-doc__bar">
        <ui.IconButton icon="arrow-left" label="Back to the overview" onClick={props.onBack} />
        <div class="dridh-doc__heading">
          <span class="dridh-doc__title">{summary()?.label}</span>
          <span class="dridh-mono dridh-muted">{doc()?.path ?? summary()?.document}</span>
        </div>
        <Pill status={status()} env={props.env} />
      </div>

      <Show when={error()}><div class="dridh-pad"><Problem error={error()!} /></div></Show>
      <Show when={!doc() && !error()}><div class="dridh-center"><ui.Loader size="large" /><span>Opening the document…</span></div></Show>

      <Show when={doc()}>
        <div class="dridh-doc__scroll">
          <div class="dridh-doc__meta">
            <span class="dridh-chip dridh-mono" title={doc().sha256}>rev {String(doc().sha256 ?? "").slice(0, 12)}</span>
            <Show when={doc().validation}>
              <span class={`dridh-chip ${doc().validation.ok ? "dridh-chip--success" : "dridh-chip--danger"}`}>
                {doc().validation.ok ? "Checks passed" : `${doc().validation.errors.length} check(s) failed`}
                {doc().validation.warnings?.length ? ` · ${doc().validation.warnings.length} warning(s)` : ""}
              </span>
            </Show>
            <Show when={doc().jev?.decision}><span class="dridh-chip">Jev: {doc().jev.decision} (advisory)</span></Show>
          </div>

          <Show when={doc().concerns?.length}>
            <div class="dridh-callout dridh-callout--attention">
              <strong>Open concerns</strong>
              <ul><For each={doc().concerns}>{(c: Json) => <li>{typeof c === "string" ? c : c.text ?? JSON.stringify(c)}</li>}</For></ul>
            </div>
          </Show>

          <div class="dridh-doc__grid">
            <nav class={`dridh-toc ${tocOpen() ? "dridh-toc--open" : ""}`} aria-label="On this page">
              <button type="button" class="dridh-toc__toggle" aria-expanded={tocOpen()} onClick={() => setTocOpen((v) => !v)}>
                <ui.Icon name="bullet-list" size="small" /> On this page <span class="dridh-muted">({sections().length})</span>
              </button>
              <ul class="dridh-toc__list">
                <For each={sections()}>
                  {(s: Json) => <li class={`dridh-toc__item dridh-toc__item--l${s.level}`}><button type="button" onClick={() => jump(s.title)}>{s.title}</button></li>}
                </For>
              </ul>
            </nav>
            <article class="dridh-prose" ref={body} onClick={enlarge}>
              <Show when={text() !== undefined}><ui.Markdown text={text()!} /></Show>
            </article>
          </div>

          <Show when={review()}>
            <section class="dridh-card" aria-labelledby="dridh-checklist" ref={checklistCard}>
              <div class="dridh-card__head">
                <h2 class="dridh-h2" id="dridh-checklist">Review checklist</h2>
                <Show when={review().total}><Meter done={review().ticked} total={review().total} /></Show>
              </div>
              <Show when={review().status === "APPROVED"}>
                <div class="dridh-callout dridh-callout--success"><strong>Approved{review().approved_by ? ` by ${review().approved_by}` : ""}.</strong>
                  <span>If the document changes, the approval goes stale and it is reviewed again.</span></div>
              </Show>
              <ul class="dridh-checklist">
                <For each={review().items ?? []}>
                  {(item: Json) => (
                    <li>
                      <label class={`dridh-check ${item.checked ? "dridh-check--on" : ""}`}>
                        <input type="checkbox" checked={item.checked} disabled={!reviewOpen() || Boolean(busyItem())} onChange={() => void tick(item)} />
                        <span>{item.item}</span>
                        <Show when={busyItem() === item.item}><ui.Loader size="small" /></Show>
                      </label>
                    </li>
                  )}
                </For>
              </ul>
            </section>
          </Show>
        </div>

        <Show when={reviewOpen()}>
          <div class="dridh-actions">
            <button type="button" class="dridh-actions__hint" onClick={() => checklistCard?.scrollIntoView({ behavior: "smooth", block: "start" })}>
              {remaining() > 0 ? `${remaining()} checklist item${remaining() === 1 ? "" : "s"} left to check` : "Checklist complete"}
              <ui.Icon name="chevron-down" size="small" />
            </button>
            <ui.Button variant="neutral" icon="pencil-line" onClick={requestChanges}>Request changes</ui.Button>
            <ui.Tooltip value={remaining() > 0 ? "Check every item on the review checklist first" : `Approve this ${noun()} under your name`}>
              <ui.Button variant="contrast" icon="check" disabled={remaining() > 0} onClick={approve}>Approve</ui.Button>
            </ui.Tooltip>
          </div>
        </Show>
      </Show>
    </div>
  )
}

/** Approve: the person names themselves, Dridh shows what is approved and the phrase, the person types it. */
function ApproveDialog(props: { env: Env; project: string; noun: string; doc: Json; approve: string[]; close: () => void; done: () => Promise<void> }) {
  const { ctx, desk, reviewer } = props.env
  const ui = ctx.ui
  const [name, setName] = createSignal(reviewer.value?.name ?? "")
  const [step, setStep] = createSignal<"who" | "starting" | "type" | "saving">("who")
  const [challenge, setChallenge] = createSignal<Challenge>()
  const [typed, setTyped] = createSignal("")
  const [error, setError] = createSignal<Failure>()
  let finished = false
  onCleanup(() => { const c = challenge(); if (c && !finished) void desk.cancel(c.id) }) // Escape or Cancel: nothing is recorded

  const start = async () => {
    const who = name().trim()
    if (!who) return
    reviewer.set({ name: who })
    setError(undefined)
    setStep("starting")
    const reply = readReply<Json>(await desk.begin({ project: props.project, args: [...props.approve, "--reviewer", who] }).catch((e: unknown) => failure(e)))
    if (!reply.ok) { setError(reply.error); return setStep("who") }
    if (reply.data.done) { finished = true; await props.done(); return props.close() }
    setChallenge(reply.data as Challenge)
    setStep("type")
  }

  const confirm = async () => {
    const c = challenge()
    if (!c || !typed().trim()) return
    setStep("saving")
    finished = true
    const reply = readReply(await desk.finish({ id: c.id, typed: typed() }).catch((e: unknown) => failure(e)))
    if (!reply.ok) {
      setChallenge(undefined)
      setTyped("")
      // Dridh's own advice names the terminal command; here the person presses Continue instead.
      setError(reply.error.code === "APPROVAL_REQUIRED" ? { ...reply.error, recovery: "Press Continue and type the confirmation exactly as shown." } : reply.error)
      return setStep("who")
    }
    ctx.toast.show({ title: `${props.noun[0].toUpperCase()}${props.noun.slice(1)} approved`, description: `Recorded by Dridh under ${name().trim()}.`, variant: "success" })
    await props.done()
    props.close()
  }

  const matches = () => Boolean(challenge()) && typed() === challenge()!.expected
  const footer = (): JSX.Element => (
    <>
      <ui.Button variant="ghost" onClick={props.close}>Cancel</ui.Button>
      <Show when={step() === "who" || step() === "starting"} fallback={
        <ui.Button variant="contrast" icon="check" busy={step() === "saving"} disabled={!typed().trim()} onClick={() => void confirm()}>Approve</ui.Button>
      }>
        <ui.Button variant="contrast" busy={step() === "starting"} disabled={!name().trim()} onClick={() => void start()}>Continue</ui.Button>
      </Show>
    </>
  )

  return (
    <ui.Dialog title={`Approve the ${props.noun}`} description="Your approval is recorded under your name. Agents cannot approve; only you can, by typing the confirmation." footer={footer()} size="fit">
      <div class="dridh-dialog">
        <Show when={error()}><Problem error={error()!} /></Show>
        <dl class="dridh-facts">
          <dt>Document</dt><dd class="dridh-mono">{props.doc?.path}</dd>
          <dt>Revision</dt><dd class="dridh-mono">{String(props.doc?.sha256 ?? "").slice(0, 16)}…</dd>
          <Show when={props.doc?.review}><dt>Checklist</dt><dd>{props.doc.review.ticked} of {props.doc.review.total} checked</dd></Show>
        </dl>
        <Show when={step() === "who" || step() === "starting"}>
          <label class="dridh-field">
            <span class="dridh-field__label">Your name</span>
            <ui.TextInput value={name()} onInput={setName} onSubmit={() => void start()} label="Your name" placeholder="Recorded with the approval" size="large" class="dridh-wide" autofocus />
          </label>
        </Show>
        <Show when={challenge()}>
          <Show when={challenge()!.summary}><pre class="dridh-summary">{challenge()!.summary}</pre></Show>
          <div class="dridh-field" ref={(el) => setTimeout(() => el.querySelector("input")?.focus(), 50)}>
            <span class="dridh-field__label">To approve, type exactly</span>
            <code class="dridh-phrase">{challenge()!.expected}</code>
            <ui.TextInput value={typed()} onInput={setTyped} onSubmit={() => void confirm()} label="Confirmation" placeholder="Type the text above" size="large" class="dridh-wide" autofocus invalid={Boolean(typed()) && !challenge()!.expected.startsWith(typed())} />
            <span class={`dridh-field__hint ${matches() ? "dridh-field__hint--ok" : ""}`}>{matches() ? "Matches. Press Approve." : "Dridh checks the text exactly; it is not filled in for you."}</span>
          </div>
        </Show>
        <p class="dridh-muted dridh-small">If the document changes after approval, the approval goes stale and the document is reviewed again.</p>
      </div>
    </ui.Dialog>
  )
}

/** Request changes: the person's note goes into the session's message box, addressed to the agent. They send it. */
function ChangesDialog(props: { env: Env; noun: string; file: string; screen: SessionScreen; close: () => void }) {
  const { ctx } = props.env
  const ui = ctx.ui
  const [text, setText] = createSignal("")
  const put = () => {
    const note = text().trim()
    if (!note) return
    props.screen.composer.setText(`Changes requested for the ${props.noun} (${props.file}):\n\n${note}\n\nPlease revise the ${props.noun} and ask me to review it again.`)
    props.screen.composer.focus()
    props.close()
    ctx.toast.show({ title: "Your note is in the message box", description: "Read it over and press Enter to send it to the agent.", variant: "default" })
  }
  return (
    <ui.Dialog title={`Request changes to the ${props.noun}`} description="Write what should change. It goes into the message box for the agent; you send it." size="fit"
      footer={<><ui.Button variant="ghost" onClick={props.close}>Cancel</ui.Button><ui.Button variant="contrast" icon="pencil-line" disabled={!text().trim()} onClick={put}>Put in the message box</ui.Button></>}>
      <div class="dridh-dialog">
        <label class="dridh-field">
          <span class="dridh-field__label">What should change, and why</span>
          <textarea class="dridh-textarea" rows={7} value={text()} onInput={(e) => setText(e.currentTarget.value)} placeholder="For example: The refund flow is missing. Add it as UC-04, with the partial-refund case." autofocus />
        </label>
      </div>
    </ui.Dialog>
  )
}
