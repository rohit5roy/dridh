/**
 * Dridh in the OpenCode desktop app. The app loads this bundle as the plugin's `./gui-experimental` export (built by
 * `npm run build`, see vite.config.ts); OpenCode itself is not modified.
 *
 * - Windows (`setup`) show a "Dridh" tab in each session's side panel for projects with a Dridh workspace, and add
 *   palette commands. They read Dridh's state with `dridh ui ...`, the same JSON the terminal panel shows.
 * - The main process (`main`, main.ts) runs the dridh command for the windows, limited to the commands in commands.ts.
 *
 * The terminal panel (tui/) is unchanged: the server, terminal and desktop parts share one OpenCode server.
 */
import { Command, Panel, Plugin, Style } from "@opencode/plugin/gui"
import { Schema } from "effect"
import { createEffect, createSignal } from "solid-js"
import { readReply } from "./commands.ts"
import { DridhDesktop } from "./contract.ts"
import { main } from "./main.ts"
import { DridhPanel, type Env } from "./panel.tsx"
import css from "./styles.css?inline"

const TAB = "dridh"

export default Plugin.define({
  setup(ctx) {
    ctx.add(Style, css)
    const desk = ctx.main(DridhDesktop)
    const reviewer = ctx.storage.store("reviewer", { schema: Schema.Struct({ name: Schema.String }), initial: { name: "" } })
    const env: Env = { ctx, desk, reviewer }

    /** Which directories hold a Dridh workspace, learnt as sessions open, so the tab shows only where Dridh is set up. */
    const [known, setKnown] = createSignal<Record<string, boolean>>({})
    const probe = async (directory: string) => {
      if (directory in known()) return
      setKnown((all) => ({ ...all, [directory]: false }))
      const reply = readReply(await desk.read({ project: directory, args: ["ui", "state"] }).catch(() => "{}"))
      setKnown((all) => ({ ...all, [directory]: reply.ok }))
    }
    createEffect(() => {
      const session = ctx.sessions.current()
      if (session?.directory) void probe(session.directory)
    })

    ctx.add(Panel, {
      id: TAB,
      region: "side",
      list: (input) => (known()[input.session.directory] || input.open.includes(TAB) ? [{ id: TAB, title: "Dridh", pinned: true, fallback: true }] : []),
      render: (props) => <DridhPanel env={env} session={props.session} screen={props.screen} />,
    })

    const open = () => {
      const session = ctx.sessions.current()
      if (!session) {
        ctx.toast.show({ title: "Open a session first", description: "The Dridh panel opens beside a session.", variant: "default" })
        return
      }
      setKnown((all) => ({ ...all, [session.directory]: true }))
      ctx.layout.open(`${ctx.id}:${TAB}`, session)
    }
    ctx.add(Command, { id: "open", title: "Dridh: Open panel", description: "Documents, reviews and approvals for this feature", keywords: "dridh review approve design use cases plan", run: open })
  },
  main,
})
