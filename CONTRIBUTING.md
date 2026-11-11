# Contributing

Everyone taking part in this project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Licence and the CLA

Dridh is licensed under the AGPL-3.0 and may also be offered under commercial licences. Before your first pull request
is merged, you accept the [Contributor License Agreement](CLA.md): the CLA check on your pull request tells you how (one
comment). You keep the copyright in your work.

## Set up

Requirements: Python 3.13 with [uv](https://docs.astral.sh/uv/), Node.js 24 with npm, Git, and OpenCode 2.2.5 or later.

```bash
git clone https://github.com/rohit5roy/dridh && cd dridh/dridh
uv sync --frozen
(cd opencode/plugins/dridh-bridge && npm ci && npm run build)
uv run python scripts/build_assets.py
```

## Check your change

```bash
uv run ruff check src tests
DRIDH_OPENCODE_BIN="$(command -v opencode)" uv run pytest -m "not live and not docker" -q
(cd opencode/plugins/dridh-bridge && npm test)
```

Every pull request runs the same checks in CI (`.github/workflows/ci.yml`), in stages: security and lint first, then
the tests and the packaged wheel, then the Docker full-system E2E. The `CI gate` job must pass before merging.

## Rules the code keeps

The design notes for the bridge, the plugin, the agent runner and recovery are indexed in [docs/README.md](docs/README.md). Update them in the same change when you alter the behaviour they describe.

- Approvals stay human-only and typed in a terminal; never add a bridge operation or agent tool that approves.
- Every behaviour change comes with a test; a bug fix comes with a test that fails without it.
- The version is set only in `dridh/src/dridh/__init__.py`, and releases move it forward (see below).

## Releasing

Releases need the maintainer's approval and never lower the version.

1. Actions → **Release** → Run workflow on `main`: **rc** publishes the current release candidate (`0.8.0rc2`),
   **stable** publishes the final version (`0.8.0`). Pushing a tag `vX.Y.Z[rcN]` that matches the code works too.
2. The workflow checks that you may release, that CI passed for that commit and that the version is newer than every
   earlier release; then it builds and smoke-tests the wheel and waits for approval.
3. After approval it tags, publishes the GitHub Release (release candidates as pre-releases) and moves `main` to the
   next version: `0.7.9` → `0.8.0rc1` → `0.8.0rc2` … → `0.8.0` → `0.9.0rc1` (`dridh/scripts/version.py`).

Never edit the version by hand except to start a new minor or major line (raise it; CI refuses a lower one).
