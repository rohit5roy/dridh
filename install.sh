#!/usr/bin/env bash
# Install Dridh and wire it into OpenCode.
#
#   curl -fsSL https://github.com/rohit5roy/dridh/releases/latest/download/install.sh | bash
#                                         the newest release (the default whenever this script is not in a checkout)
#   ./install.sh                          inside a checkout: build from it (needs Node.js 24 + npm for the plugin)
#   ./install.sh --release v0.2.0         a specific release ("latest" also works)
#   ./install.sh --wheel PATH_OR_URL      a wheel you already have
#
# Releases download without credentials from a public repository; for a private one the gh CLI (signed in) or
# GITHUB_TOKEN is used.
#
# Before anything is installed every prerequisite is checked and a plan is printed. Missing pieces are then
# installed: uv (pinned GitHub release), Python 3.13 (through uv), fastembed (with the dridh command) and glow
# (Homebrew when present, otherwise the pinned GitHub release, checksum-verified, into ~/.local/bin). Git is
# installed through the system package manager when one is available. OpenCode is never installed for you:
# Dridh is an OpenCode plugin, so without OpenCode the script stops before changing anything.
#
# Other options:
#   --check             report the prerequisites and the plan, change nothing
#   --no-embeddings     do not install fastembed (Code Reacher then uses its hashing fallback)
#   --no-glow           do not install glow (the TUI reviewer then shows raw Markdown)
#   --config-dir DIR    OpenCode configuration directory (default: OPENCODE_CONFIG_DIR or ~/.config/opencode)
#   --no-configure      install the dridh command only; do not run `dridh install`
#   --dry-run           show what `dridh install` would change, write nothing to the OpenCode configuration
#
# The OpenCode configuration is changed only by `dridh install`, which backs up every file it touches;
# `dridh uninstall` restores it.
set -euo pipefail

REPO="rohit5roy/dridh"
OPENCODE_MINIMUM="2.2.5"   # oldest supported OpenCode V2 release; any later V2 release is accepted
PYTHON_VERSION="3.13"
FASTEMBED_VERSION="0.9.1"
GLOW_VERSION="2.1.1"
UV_VERSION="0.11.32"
# When piped (curl ... | bash) there is no script file, so there is no checkout either.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || echo /nonexistent)"
LOCAL_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"

source_kind="auto"; source_value=""; config_args=(); configure=1; dry_run=0
check_only=0; want_embeddings=1; want_glow=1

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
version_at_least() {  # version_at_least FOUND MINIMUM  (numeric x.y.z comparison)
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$2" ]
}
sha256_check() { if have sha256sum; then sha256sum -c - ; else shasum -a 256 -c - ; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --release) [ $# -ge 2 ] || die "--release needs a tag or 'latest'"; source_kind="release"; source_value="$2"; shift 2 ;;
    --wheel) [ $# -ge 2 ] || die "--wheel needs a path or URL"; source_kind="wheel"; source_value="$2"; shift 2 ;;
    --config-dir) [ $# -ge 2 ] || die "--config-dir needs a directory"; config_args=(--config-dir "$2"); shift 2 ;;
    --no-configure) configure=0; shift ;;
    --dry-run) dry_run=1; shift ;;
    --check) check_only=1; shift ;;
    --no-embeddings) want_embeddings=0; shift ;;
    --no-glow) want_glow=0; shift ;;
    -h|--help) [ -f "$0" ] && awk 'NR>1 && /^set -euo/ {exit} NR>1 && /^#/ {sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# No explicit source: build from the checkout this script sits in, otherwise install the newest release.
if [ "$source_kind" = auto ]; then
  if [ -f "$HERE/dridh/pyproject.toml" ]; then source_kind="checkout"; else source_kind="release"; source_value="latest"; fi
fi

# Tools installed into ~/.local/bin by this script must be found by the rest of it.
case ":$PATH:" in *":$LOCAL_BIN:"*) ;; *) PATH="$LOCAL_BIN:$PATH" ;; esac

# --- 1. check everything, change nothing ---------------------------------------------------------------------
plan=(); report=()
row() { report+=("$(printf '  %-9s %-8s %s' "$1" "$2" "$3")"); }

downloader=""
if have curl; then downloader="curl"; elif have wget; then downloader="wget"; fi
fetch() {  # fetch URL FILE
  if [ "$downloader" = curl ]; then curl -fsSL -o "$2" "$1"; else wget -q -O "$2" "$1"; fi \
    || die "could not download $1 (network or proxy?); nothing further was installed"
}

if have opencode; then
  oc_version="$(opencode --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  oc_major="${oc_version%%.*}"
  if [ -z "$oc_version" ]; then row opencode warn "version unknown (Dridh needs $OPENCODE_MINIMUM or later)"
  elif ! version_at_least "$oc_version" "$OPENCODE_MINIMUM"; then row opencode OLD "$oc_version (Dridh needs $OPENCODE_MINIMUM or later)"; opencode_too_old=1
  elif [ "$oc_major" != 2 ]; then row opencode warn "$oc_version (Dridh is built for OpenCode V2)"
  else row opencode ok "$oc_version"; fi
else
  row opencode MISSING "required: Dridh is a plugin for OpenCode"
fi

if have git; then row git ok "$(git --version | awk '{print $3}')"; else row git missing "will install with the system package manager"; plan+=(git); fi
if [ -n "$downloader" ]; then row download ok "$downloader"; else row download missing "curl or wget is needed to fetch uv and glow"; fi

if have uv; then row uv ok "$(uv --version | awk '{print $2}')"; else row uv missing "will install uv $UV_VERSION (GitHub release, checksum-verified)"; plan+=(uv); fi

if have uv && uv python find "$PYTHON_VERSION" >/dev/null 2>&1; then
  row python ok "$(uv python find "$PYTHON_VERSION")"
elif have "python$PYTHON_VERSION"; then
  row python ok "$(command -v "python$PYTHON_VERSION")"
else
  row python missing "Python $PYTHON_VERSION will be installed through uv"; plan+=(python)
fi

if [ "$want_embeddings" -eq 1 ]; then row fastembed install "fastembed==$FASTEMBED_VERSION goes into the dridh tool environment"
else row fastembed skip "--no-embeddings: Code Reacher uses its hashing fallback"; fi

if [ "$want_glow" -eq 0 ]; then row glow skip "--no-glow: the TUI reviewer shows raw Markdown"
elif have glow; then row glow ok "$(glow --version 2>/dev/null | head -1)"
else row glow missing "will install glow $GLOW_VERSION"; plan+=(glow); fi

if [ "$source_kind" = checkout ] && [ ! -f "$HERE/dridh/src/dridh/assets/plugin/dridh-bridge/dist/index.js" ]; then
  if have npm; then row node ok "$(node --version 2>/dev/null) (builds the OpenCode plugin)"
  else row node MISSING "building from a checkout needs Node.js 24 and npm; or use --release / --wheel"; fi
fi

case "$source_kind" in
  checkout) row source ok "build from the checkout at $HERE" ;;
  release) row source ok "release $source_value of $REPO" ;;
  wheel) row source ok "wheel $source_value" ;;
esac
say "prerequisites"
printf '%s\n' "${report[@]}"
have opencode || die "OpenCode is not installed. Dridh is a plugin for OpenCode, so nothing was changed.
Install OpenCode $OPENCODE_MINIMUM or later first (npm install -g @opencode/cli@latest, see https://opencode.ai), then run this script again."
[ "${opencode_too_old:-0}" -eq 0 ] || die "OpenCode $oc_version is older than $OPENCODE_MINIMUM, so nothing was changed.
Upgrade it (npm install -g @opencode/cli@latest), then run this script again."
if [ "$source_kind" = checkout ] && [ ! -f "$HERE/dridh/src/dridh/assets/plugin/dridh-bridge/dist/index.js" ] && ! have npm; then
  die "building from a checkout needs Node.js 24 and npm for the OpenCode plugin; or use --release / --wheel"
fi
if [ ${#plan[@]} -gt 0 ]; then say "will install: ${plan[*]}"; else say "nothing extra to install"; fi
if [ "$check_only" -eq 1 ]; then say "--check: nothing was changed"; exit 0; fi

# --- 2. install what is missing ------------------------------------------------------------------------------
sudo_cmd=""
if [ "$(id -u)" -ne 0 ] && have sudo; then sudo_cmd="sudo"; fi

install_git() {
  say "installing git"
  if have brew; then brew install git
  elif have apt-get; then $sudo_cmd apt-get update -qq && $sudo_cmd apt-get install -y -qq git
  elif have dnf; then $sudo_cmd dnf install -y -q git
  elif have yum; then $sudo_cmd yum install -y -q git
  elif have pacman; then $sudo_cmd pacman -S --noconfirm git
  elif have apk; then $sudo_cmd apk add --no-cache git
  else die "git is missing and no supported package manager was found; install Git and run this script again"; fi
}

install_uv() {
  # The pinned GitHub release, checksum-verified (the same route as glow; no install script is piped to a shell).
  [ -n "$downloader" ] || die "uv is missing and neither curl nor wget is available to install it"
  local arch os target
  case "$(uname -m)" in x86_64|amd64) arch=x86_64 ;; arm64|aarch64) arch=aarch64 ;; *) die "no uv build for $(uname -m); install uv yourself" ;; esac
  case "$(uname -s)" in Linux) os=unknown-linux-gnu ;; Darwin) os=apple-darwin ;; *) die "no uv build for $(uname -s); install uv yourself" ;; esac
  target="uv-${arch}-${os}"
  say "installing uv $UV_VERSION into $LOCAL_BIN"
  fetch "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${target}.tar.gz" "$work/${target}.tar.gz"
  fetch "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${target}.tar.gz.sha256" "$work/${target}.tar.gz.sha256"
  (cd "$work" && sha256_check < "${target}.tar.gz.sha256" >/dev/null) || die "uv checksum mismatch; nothing was installed"
  tar -xzf "$work/${target}.tar.gz" -C "$work"
  mkdir -p "$LOCAL_BIN"
  install -m 0755 "$work/$target/uv" "$work/$target/uvx" "$LOCAL_BIN/"
  have uv || die "uv was installed at $LOCAL_BIN/uv but is not on PATH"
}

install_glow() {
  if have brew; then say "installing glow with Homebrew"; brew install glow; return; fi
  [ -n "$downloader" ] || die "glow is missing and neither curl nor wget is available; use --no-glow to skip it"
  local os arch name base
  case "$(uname -s)" in Linux) os=Linux ;; Darwin) os=Darwin ;; *) die "no glow build for $(uname -s); use --no-glow" ;; esac
  case "$(uname -m)" in x86_64|amd64) arch=x86_64 ;; arm64|aarch64) arch=arm64 ;; *) die "no glow build for $(uname -m); use --no-glow" ;; esac
  name="glow_${GLOW_VERSION}_${os}_${arch}"
  base="https://github.com/charmbracelet/glow/releases/download/v${GLOW_VERSION}"
  say "installing glow $GLOW_VERSION into $LOCAL_BIN"
  fetch "$base/$name.tar.gz" "$work/$name.tar.gz"
  fetch "$base/checksums.txt" "$work/glow-checksums.txt"
  (cd "$work" && grep " $name.tar.gz\$" glow-checksums.txt | sha256_check >/dev/null) || die "glow checksum mismatch; nothing was installed"
  tar -xzf "$work/$name.tar.gz" -C "$work"
  mkdir -p "$LOCAL_BIN"
  install -m 0755 "$work/$name/glow" "$LOCAL_BIN/glow"
  have glow || die "glow was installed at $LOCAL_BIN/glow but is not on PATH"
}

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
for item in ${plan[@]+"${plan[@]}"}; do
  case "$item" in
    git) install_git ;;
    uv) install_uv ;;
    python) say "installing Python $PYTHON_VERSION through uv"; uv python install "$PYTHON_VERSION" >/dev/null ;;
    glow) install_glow ;;
  esac
done

# --- 3. obtain the wheel -------------------------------------------------------------------------------------
case "$source_kind" in
  checkout)
    [ -f "$HERE/dridh/pyproject.toml" ] || die "no checkout next to install.sh; use --release or --wheel"
    say "building the wheel from $HERE/dridh"
    (cd "$HERE/dridh" && uv build --wheel --out-dir "$work" >/dev/null)
    wheel="$(ls "$work"/dridh-*.whl)"
    ;;
  release)
    if [ "$source_value" = latest ]; then
      # The newest release's tag: from the redirect of /releases/latest (public, no token), or through gh / the API.
      tag="$(curl -fsSI "https://github.com/$REPO/releases/latest" 2>/dev/null | tr -d '\r' | sed -n 's#^[Ll]ocation: .*/releases/tag/##p' | tail -1 || true)"
      if [ -z "$tag" ] && have gh; then tag="$(gh release view --repo "$REPO" --json tagName -q .tagName 2>/dev/null || true)"; fi
      if [ -z "$tag" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
        tag="$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" "https://api.github.com/repos/$REPO/releases/latest" \
               | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || true)"
      fi
      [ -n "$tag" ] || die "could not find the latest release of $REPO (none published yet, or the repository is private: sign in with gh or set GITHUB_TOKEN)"
      source_value="$tag"
    fi
    version="${source_value#v}"; asset="dridh-${version}-py3-none-any.whl"
    say "installing Dridh $source_value"
    base="https://github.com/$REPO/releases/download/$source_value"
    if curl -fsSL -o "$work/$asset" "$base/$asset" 2>/dev/null && curl -fsSL -o "$work/$asset.sha256" "$base/$asset.sha256" 2>/dev/null; then
      :  # public repository: no credentials needed
    elif have gh; then
      gh release download "$source_value" --repo "$REPO" --pattern "$asset" --pattern "$asset.sha256" --dir "$work" --clobber
    else
      [ -n "${GITHUB_TOKEN:-}" ] || die "cannot download $asset: the repository is private; sign in with the gh CLI or set GITHUB_TOKEN"
      api="https://api.github.com/repos/$REPO/releases/tags/$source_value"
      for name in "$asset" "$asset.sha256"; do
        id="$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" "$api" \
              | python3 -c "import json,sys; print(next(a['id'] for a in json.load(sys.stdin)['assets'] if a['name']==sys.argv[1]))" "$name")"
        curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/octet-stream" \
          -o "$work/$name" "https://api.github.com/repos/$REPO/releases/assets/$id"
      done
    fi
    (cd "$work" && sha256_check < "$asset.sha256" >/dev/null) || die "checksum mismatch for $asset"
    wheel="$work/$asset"
    ;;
  wheel)
    case "$source_value" in
      http://*|https://*) say "downloading $source_value"; curl -fsSL -o "$work/$(basename "$source_value")" "$source_value"; wheel="$work/$(basename "$source_value")" ;;
      *) [ -f "$source_value" ] || die "no such wheel: $source_value"; wheel="$source_value" ;;
    esac
    ;;
esac

# Refuse a wheel that lacks the OpenCode plugin before anything is installed.
python3 - "$wheel" <<'PY' || die "the wheel has no packaged OpenCode plugin; rebuild it (see dridh/hatch_build.py)"
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
required = ("dridh/assets/plugin/dridh-bridge/dist/index.js", "dridh/assets/plugin/dridh-bridge/tui/index.tsx")
sys.exit(0 if all(r in names for r in required) and any(n.startswith("dridh/assets/agents/dridh/") for n in names) else 1)
PY

# --- 4. install the command (with fastembed unless --no-embeddings) ------------------------------------------
with_args=()
[ "$want_embeddings" -eq 1 ] && with_args=(--with "fastembed==$FASTEMBED_VERSION")
if [ "$want_embeddings" -eq 1 ]; then say "installing $(basename "$wheel") with uv, with fastembed $FASTEMBED_VERSION"; else say "installing $(basename "$wheel") with uv"; fi
uv tool install --force --python "$PYTHON_VERSION" ${with_args[@]+"${with_args[@]}"} "$wheel"
bin_dir="$(uv tool dir --bin)"
dridh_bin="$bin_dir/dridh"
[ -x "$dridh_bin" ] || die "dridh was not installed at $dridh_bin"
case ":$PATH:" in *":$bin_dir:"*) ;; *) say "note: add $bin_dir to PATH to run dridh directly" ;; esac
"$dridh_bin" --version

# --- 5. wire it into OpenCode -----------------------------------------------------------------------------------
if [ "$configure" -eq 1 ]; then
  if [ "$dry_run" -eq 1 ]; then
    say "dry run: changes dridh install would make"
    "$dridh_bin" install ${config_args[@]+"${config_args[@]}"} --dry-run
  else
    say "adding the Dridh plugin and agent profiles to the OpenCode configuration"
    "$dridh_bin" install ${config_args[@]+"${config_args[@]}"} >/dev/null
  fi
fi

if [ "$configure" -eq 0 ]; then
  say "installed the dridh command only (--no-configure). Next: dridh install, then dridh doctor"
  exit 0
fi
say "checking the installation"
if "$dridh_bin" doctor ${config_args[@]+"${config_args[@]}"}; then
  say "done. In a project: dridh init, then start OpenCode and talk to the dridh/coordinator agent."
else
  say "dridh doctor reported a missing prerequisite (above). Fix it and run: dridh doctor"
  exit 3
fi
