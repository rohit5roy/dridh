"""Refuse to build a Dridh wheel without the OpenCode plugin and agent profiles inside it.

`src/dridh/assets/` is generated (scripts/build_assets.py) and ignored by Git. Without this hook, a build from a fresh
checkout (for example `uv tool install git+https://...`) quietly produces a wheel that `dridh install` cannot use.

When the assets are missing and Node.js/npm are available, the hook builds them; otherwise it stops with the commands to
run. Set DRIDH_SKIP_ASSET_BUILD=1 to forbid the automatic build (the check still applies).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "src" / "dridh" / "assets"
PLUGIN = ROOT / "opencode" / "plugins" / "dridh-bridge"


def _assets_complete() -> bool:
    plugin = ASSETS / "plugin" / "dridh-bridge"
    return (plugin / "package.json").is_file() and (plugin / "dist" / "index.js").is_file() and any((ASSETS / "agents" / "dridh").rglob("*.md"))


def _build_assets() -> None:
    npm = shutil.which("npm")
    if npm is None or not (PLUGIN / "package.json").is_file():
        return
    subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=PLUGIN, check=True)
    subprocess.run([npm, "run", "build"], cwd=PLUGIN, check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_assets.py")], cwd=ROOT, check=True)


class AssetCheckHook(BuildHookInterface):
    PLUGIN_NAME = "dridh-assets"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or _assets_complete():
            return
        if os.environ.get("DRIDH_SKIP_ASSET_BUILD") != "1":
            _build_assets()
        if not _assets_complete():
            raise RuntimeError(
                "Dridh wheel build stopped: the packaged OpenCode plugin and agent profiles (src/dridh/assets) are missing.\n"
                "Build them first (Node.js 24 and npm are needed):\n"
                "  (cd opencode/plugins/dridh-bridge && npm ci && npm run build)\n"
                "  python scripts/build_assets.py\n"
                "or install a released wheel instead (see install.sh)."
            )
