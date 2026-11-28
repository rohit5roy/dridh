// Builds dist/gui-experimental.cjs: one CommonJS file the OpenCode desktop app loads as the plugin's ./gui-experimental
// export. `gui()` compiles Solid JSX and keeps the modules the app provides (Solid, Effect, the plugin SDK) external.
import { gui } from "@opencode/plugin/gui/vite"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [gui({ entry: "src/gui.tsx", outFile: "dist/gui-experimental.cjs" })],
  build: { minify: false, target: "es2022" },
})
