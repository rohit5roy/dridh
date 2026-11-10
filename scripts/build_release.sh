#!/usr/bin/env bash
# Build the release wheel: OpenCode plugin + agent profiles staged inside, validated, with a sha256 file next to it.
#   scripts/build_release.sh [OUT_DIR]    (default: dridh/dist)
# Needs uv, Node.js 24 and npm.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dridh/dist}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

(cd "$ROOT/dridh/opencode/plugins/dridh-bridge" && npm ci --no-audit --no-fund && npm run build)
(cd "$ROOT/dridh" && uv run --frozen python scripts/build_assets.py)
(cd "$ROOT/dridh" && DRIDH_SKIP_ASSET_BUILD=1 uv build --wheel --out-dir "$OUT")

version="$(python3 "$ROOT/dridh/scripts/version.py")"
wheel="dridh-$version-py3-none-any.whl"
python3 "$ROOT/scripts/ci/check_wheel.py" "$OUT/$wheel" >/dev/null || { python3 "$ROOT/scripts/ci/check_wheel.py" "$OUT/$wheel"; exit 1; }
(cd "$OUT" && sha256sum "$wheel" > "$wheel.sha256")
echo "$OUT/$wheel"
