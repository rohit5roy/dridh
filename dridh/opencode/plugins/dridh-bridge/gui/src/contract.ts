/**
 * The bridge between a desktop app window and the app's main process. The window cannot start programs; the main
 * process runs the dridh command for it, limited to the commands in commands.ts. Every reply is a JSON `Reply` text.
 * On the web there is no main process, so every call rejects and the panel says Dridh needs the desktop app.
 */
import { Ipc } from "@opencode/plugin/gui"
import { Schema } from "effect"

const Command = Schema.Struct({ project: Schema.String, args: Schema.Array(Schema.String) })

export const DridhDesktop = Ipc.define({
  id: "dridh.bridge.desktop",
  methods: {
    /** A read-only view (`dridh ui ...`). */
    read: { input: Command, output: Schema.String },
    /** A record a person makes without a typed confirmation (a checklist tick). */
    record: { input: Command, output: Schema.String },
    /** Starts a decision and returns Dridh's summary and the phrase to type (a `Challenge`), or why it cannot start. */
    begin: { input: Command, output: Schema.String },
    /** Passes the person's typed text to the waiting decision and returns its result. */
    finish: { input: Schema.Struct({ id: Schema.String, typed: Schema.String }), output: Schema.String },
    /** Abandons a waiting decision; nothing is recorded. */
    cancel: { input: Schema.String },
  },
})
