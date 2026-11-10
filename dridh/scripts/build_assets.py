"""Stage packaged Dridh assets: the built OpenCode plugin and the agent profiles.

Run after `npm run build` in opencode/plugins/dridh-bridge. The output directory
is generated and ignored by Git; hatch includes it through `artifacts`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "opencode" / "plugins" / "dridh-bridge"
AGENT_SOURCE = ROOT / "opencode" / "agents" / "dridh"
ASSETS = ROOT / "src" / "dridh" / "assets"


def fail(message: str) -> None:
    print(f"build_assets: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    dist = PLUGIN_SOURCE / "dist"
    if not (dist / "index.js").is_file() or not (dist / "bridge-client.js").is_file():
        fail("plugin dist is missing; run `npm run build` in opencode/plugins/dridh-bridge first")
    profiles = sorted(AGENT_SOURCE.rglob("*.md"))
    if not profiles:
        fail("no agent profiles found under opencode/agents/dridh")

    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    target_plugin = ASSETS / "plugin" / "dridh-bridge"
    (target_plugin / "dist").mkdir(parents=True)
    for source in sorted(dist.glob("*.js")):
        shutil.copy2(source, target_plugin / "dist" / source.name)
    # The TUI part ships as TypeScript JSX: OpenCode compiles it at load time with the @opentui/solid preset.
    tui = PLUGIN_SOURCE / "tui"
    tui_files = sorted(tui.glob("*.tsx")) + sorted(tui.glob("*.ts"))
    if tui_files:
        (target_plugin / "tui").mkdir(parents=True)
        for source in tui_files:
            shutil.copy2(source, target_plugin / "tui" / source.name)
    source_manifest = json.loads((PLUGIN_SOURCE / "package.json").read_text())
    sdk_version = (source_manifest.get("dependencies") or {}).get("@opencode/plugin")
    if not sdk_version:
        fail("the plugin source package.json does not pin @opencode/plugin")
    import runpy

    versions = runpy.run_path(str(Path(__file__).resolve().parent / "version.py"))
    dridh_version = versions["semver"](versions["version"]())  # npm needs 0.8.0-rc.1, not PEP 440's 0.8.0rc1
    manifest = {
        "name": "dridh-bridge",
        "version": dridh_version,  # the packaged plugin carries the Dridh version (single source: src/dridh/__init__.py)
        "private": True,
        "type": "module",
        "exports": {".": "./dist/index.js", **({"./tui": "./tui/index.tsx"} if (PLUGIN_SOURCE / "tui" / "index.tsx").is_file() else {})},
        # Provenance recorded in the install manifest (plan section 2.2); not used by OpenCode.
        "opencodePluginSdk": sdk_version,
    }
    (target_plugin / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")

    target_agents = ASSETS / "agents" / "dridh"
    target_agents.mkdir(parents=True)
    for profile in profiles:
        destination = target_agents / profile.relative_to(AGENT_SOURCE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(profile, destination)
    print(f"staged {len(list(dist.glob('*.js')))} plugin files and {len(profiles)} agent profiles in {ASSETS}")


if __name__ == "__main__":
    main()
