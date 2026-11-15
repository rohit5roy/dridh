#!/usr/bin/env python3
"""Drive the real OpenCode terminal UI with Dridh installed, check what it shows, and save screenshots.

The scenario is a person's first run in a project:

  1. home screen of a project with no Dridh workspace: "dridh · type /dridh to set up"
  2. /dridh: "Set up Dridh" (new or existing codebase)
  3. choosing "New codebase": the overview shows the setup steps (step 2 of 2: start a feature)
  4. back on the home screen: "dridh · setup 2/2"
  5. /cd into another folder (code, no workspace): the TUI follows OpenCode there: "type /dridh to set up"
  6. /dridh there, "Existing codebase": indexing starts in the background, its progress in the status line
     behind the working spinner, and the spinner below the setup steps in the panel
  7. "Code indexed (N files)": the setup moves to step 3 of 3, OpenCode stays in that folder; then, as after an upgrade
     that changes the search model, the stored vectors are made stale and the panel re-indexes on its own; then a file
     is added and another deleted outside Dridh, and the panel updates the index on its own
  8. a real Dridh error (the state database is corrupted): its message, never raw JSON
  9. in the populated Phase 13 workspace: overview, Use Case and design documents, partial review checklist, metrics,
     completion matrix and execution-lot review

Dridh is installed into a throwaway HOME with `dridh install`, and OpenCode is started with a PATH that does NOT
contain dridh, so the plugin must find it through the installer's record. No model or credential is used.

Each required screen waits for exact text (polling, not fixed sleeps) and fails after a timeout. Captures are saved as
.txt, .ansi and .png; intermediate indexing screens are optional captures because indexing may finish before they appear.

Usage: tui_smoke.py --opencode PATH --dridh PATH --out DIR [--feature-project DIR] [--first-run-only]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from tui_smoke_contract import (
    FIRST_RUN_REQUIRED_SCREENSHOTS,
    FULL_REQUIRED_SCREENSHOTS,
)

WIDTH, HEIGHT = 320, 87
SESSION = "dridh-tui-smoke"
OPENCODE_SESSION = "ses_0123456789abcdef0123456789"
OTHER_FILES = 1500
PANEL_HEADER = re.compile(r"Dridh\s+Overview\s+Document\s+Checklist\s+Lots\s+Coverage\s+Metrics")

# --------------------------------------------------------------------------- rendering


def _palette256() -> list[tuple[int, int, int]]:
    base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188), (17, 168, 205),
            (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67), (59, 142, 234),
            (214, 112, 214), (41, 184, 219), (255, 255, 255)]
    steps = [0, 95, 135, 175, 215, 255]
    cube = [(steps[r], steps[g], steps[b]) for r in range(6) for g in range(6) for b in range(6)]
    grey = [(8 + 10 * i,) * 3 for i in range(24)]
    return base + cube + grey  # type: ignore[return-value]


PALETTE = _palette256()
DEFAULT_FG, DEFAULT_BG = (220, 220, 220), (10, 10, 10)
SGR = re.compile(r"\x1b\[([0-9;:]*)m")


def parse_ansi(text: str) -> list[list[tuple[str, tuple[int, int, int], tuple[int, int, int], bool]]]:
    """Turn tmux's `capture-pane -e` output into rows of (char, fg, bg, bold) cells."""

    rows = []
    fg, bg, bold, inverse = DEFAULT_FG, DEFAULT_BG, False, False
    for line in text.split("\n")[:HEIGHT]:
        row = []
        pos = 0
        for match in list(SGR.finditer(line)) + [None]:
            end = match.start() if match else len(line)
            for ch in line[pos:end]:
                f, b = (bg, fg) if inverse else (fg, bg)
                row.append((ch, f, b, bold))
            if not match:
                break
            pos = match.end()
            codes = [int(c) if c else 0 for c in re.split("[;:]", match.group(1))] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg, bg, bold, inverse = DEFAULT_FG, DEFAULT_BG, False, False
                elif c == 1:
                    bold = True
                elif c == 22:
                    bold = False
                elif c == 7:
                    inverse = True
                elif c == 27:
                    inverse = False
                elif 30 <= c <= 37 or 90 <= c <= 97:
                    fg = PALETTE[c - 30 if c < 90 else c - 90 + 8]
                elif 40 <= c <= 47 or 100 <= c <= 107:
                    bg = PALETTE[c - 40 if c < 100 else c - 100 + 8]
                elif c == 39:
                    fg = DEFAULT_FG
                elif c == 49:
                    bg = DEFAULT_BG
                elif c in (38, 48) and i + 1 < len(codes):
                    if codes[i + 1] == 5 and i + 2 < len(codes):
                        colour = PALETTE[codes[i + 2] % 256]
                        i += 2
                    elif codes[i + 1] == 2 and i + 4 < len(codes):
                        colour = (codes[i + 2], codes[i + 3], codes[i + 4])
                        i += 4
                    else:
                        colour = DEFAULT_FG if c == 38 else DEFAULT_BG
                    if c == 38:
                        fg = colour
                    else:
                        bg = colour
                i += 1
        rows.append(row)
    return rows


def render_png(ansi: str, path: Path) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    fonts = [p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")
             if Path(p).is_file()]
    if not fonts:
        return False
    size = 28  # drawn at 2x for a sharp image
    regular = ImageFont.truetype(fonts[0], size)
    sans = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    braille = ImageFont.truetype(str(sans), size) if sans.is_file() else regular  # DejaVu Sans Mono has no braille (the spinner)
    bold = ImageFont.truetype(fonts[-1], size)
    cell_w = round(regular.getlength("M"))
    cell_h = round(size * 1.25)
    pad_x = 24
    image_width = WIDTH * cell_w + 2 * pad_x
    image_height = round(image_width * 9 / 16)
    content_height = HEIGHT * cell_h
    pad_y = (image_height - content_height) // 2
    if pad_y < 0:
        raise ValueError("the captured terminal is too tall for a 16:9 screenshot")
    image = Image.new("RGB", (image_width, image_height), DEFAULT_BG)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(parse_ansi(ansi)):
        for x, (ch, fg, bg, is_bold) in enumerate(row[:WIDTH]):
            left, top = pad_x + x * cell_w, pad_y + y * cell_h
            if bg != DEFAULT_BG:
                draw.rectangle([left, top, left + cell_w - 1, top + cell_h - 1], fill=bg)
            if ch == "█":
                draw.rectangle([left, top, left + cell_w - 1, top + cell_h - 1], fill=fg)
            elif ch == "▀":
                draw.rectangle([left, top, left + cell_w - 1, top + cell_h // 2 - 1], fill=fg)
            elif ch == "▄":
                draw.rectangle([left, top + cell_h // 2, left + cell_w - 1, top + cell_h - 1], fill=fg)
            elif not ch.isspace():
                face = braille if "\u2800" <= ch <= "\u28ff" else bold if is_bold else regular
                draw.text((left, top + (cell_h - size) // 2), ch, font=face, fill=fg)
    image.save(path)
    return True


# --------------------------------------------------------------------------- tmux


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


def screen(ansi: bool = False) -> str:
    return tmux("capture-pane", "-t", SESSION, "-p", *(["-e"] if ansi else []))


class StepFailed(AssertionError):
    pass


class Smoke:
    def __init__(self, out: Path, *, require_side_by_side: bool = True, required_screenshots: tuple[str, ...] = FULL_REQUIRED_SCREENSHOTS) -> None:
        self.out = out
        self.require_side_by_side = require_side_by_side
        self.required_screenshots = required_screenshots
        self.steps: list[dict[str, str]] = []

    def save(self, name: str) -> None:
        text, ansi = screen(), screen(ansi=True)
        if self.require_side_by_side:
            self._require_side_by_side(name, text)
        self._write(name, text, ansi)

    @staticmethod
    def _require_side_by_side(name: str, text: str) -> None:
        if not PANEL_HEADER.search(text):
            excerpt = " / ".join(line.strip() for line in text.splitlines() if line.strip())[:500]
            raise StepFailed(f"{name}: refusing a screenshot without OpenCode on the left and the Dridh panel on the right; screen: {excerpt}")
        if "Start this TUI screenshot session." not in text:
            raise StepFailed(f"{name}: refusing a screenshot without the OpenCode conversation visible beside Dridh")

    def _write(self, name: str, text: str, ansi: str) -> None:
        (self.out / f"{name}.txt").write_text(text, encoding="utf-8")
        (self.out / f"{name}.ansi").write_text(ansi, encoding="utf-8")
        render_png(ansi, self.out / f"{name}.png")

    def save_failure(self, name: str, text: str) -> str | None:
        """Best-effort raw failure capture; never applies the successful screenshot layout guard."""
        errors = []
        try:
            ansi = screen(ansi=True)
        except Exception as exc:  # noqa: BLE001 - preserve the original assertion failure
            ansi = text
            errors.append(f"ANSI capture failed: {type(exc).__name__}: {exc}")
        try:
            (self.out / f"{name}.txt").write_text(text, encoding="utf-8")
            (self.out / f"{name}.ansi").write_text(ansi, encoding="utf-8")
            render_png(ansi, self.out / f"{name}.png")
        except Exception as exc:  # noqa: BLE001 - keep assertion/timeout as the primary failure
            errors.append(f"Could not write failure screenshot: {type(exc).__name__}: {exc}")
        return "; ".join(errors) or None

    def glimpse(self, name: str, needle: str, timeout: float = 20) -> bool:
        """Optionally save a short-lived state; a missed or unusable glimpse never fails the run."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = screen()
            if re.search(needle, text):
                try:
                    if self.require_side_by_side:
                        self._require_side_by_side(name, text)
                    self._write(name, text, screen(ansi=True))
                except Exception as exc:  # noqa: BLE001 - optional screenshots never fail the smoke
                    print(f"--  {name}: state seen, screenshot skipped: {type(exc).__name__}: {exc}", flush=True)
                    return False
                self.steps.append({"step": name, "saw": needle})
                print(f"ok  {name}: {needle} (seen)", flush=True)
                return True
            time.sleep(0.1)
        print(f"--  {name}: {needle} was not seen (it may have finished first)", flush=True)
        return False

    def wait(self, name: str, needle: str, timeout: float = 60, forbid: tuple[str, ...] = (), capture: bool = True) -> None:
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = screen()
            if needle in text:
                for bad in forbid:
                    if bad in text:
                        save_error = self.save_failure(f"{name}-FAILED", text)
                        suffix = f"\n{save_error}" if save_error else ""
                        raise StepFailed(f"{name}: the screen shows {bad!r}\nLast screen:\n{text[-1500:]}{suffix}")
                if capture:
                    try:
                        self.save(name)
                    except StepFailed as exc:
                        save_error = self.save_failure(f"{name}-FAILED", text)
                        suffix = f"\n{save_error}" if save_error else ""
                        raise StepFailed(f"{exc}\nLast screen:\n{text[-1500:]}{suffix}") from exc
                self.steps.append({"step": name, "saw": needle})
                print(f"ok  {name}: {needle}", flush=True)
                return
            time.sleep(0.5)
        if not text:
            try:
                text = screen()
            except Exception as exc:  # noqa: BLE001 - preserve the timeout as the primary failure
                text = f"<screen capture failed: {type(exc).__name__}: {exc}>"
        save_error = self.save_failure(f"{name}-FAILED", text)
        suffix = f"\n{save_error}" if save_error else ""
        raise StepFailed(f"{name}: {needle!r} did not appear within {timeout:.0f} s\nLast screen:\n{text[-1500:]}{suffix}")


def keys(*sequence: str) -> None:
    for key in sequence:
        tmux("send-keys", "-t", SESSION, key)
        time.sleep(0.4)


# --------------------------------------------------------------------------- scenario


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--opencode", required=True, help="OpenCode executable")
    parser.add_argument("--dridh", required=True, help="dridh executable (its directory is kept off OpenCode's PATH)")
    parser.add_argument("--out", required=True, help="directory for the screenshots and screen text")
    parser.add_argument("--feature-project", help="optional deterministic, fully-populated project for lifecycle-view captures")
    parser.add_argument("--first-run-only", action="store_true", help="exercise the first-run home route without a model or session panel")
    args = parser.parse_args()

    opencode, dridh = Path(args.opencode).resolve(), Path(args.dridh).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if shutil.which("tmux") is None:
        print("tmux is required", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="dridh-tui-smoke-") as tmp:
        home, project, other = Path(tmp) / "home", Path(tmp) / "project", Path(tmp) / "other"
        home.mkdir()
        project.mkdir()
        other.mkdir()
        (other / "app.py").write_text("def greet(name):\n    return f'Hello, {name}'\n")
        for i in range(OTHER_FILES - 1):  # enough files that indexing progress is visible for a moment
            (other / f"mod{i:04d}.py").write_text(f"def f{i}(value):\n    return value + {i}\n")
        install_env = {**os.environ, "HOME": str(home)}
        # The parent E2E process points OpenCode at its shared config directory. This nested smoke needs a fresh installer
        # config under its throwaway HOME, both to avoid touching the E2E install and to verify the installer record.
        install_env.pop("OPENCODE_CONFIG_DIR", None)
        install = subprocess.run([str(dridh), "install"], env=install_env,  # noqa: S603
                                 capture_output=True, text=True, timeout=300, check=False)
        if install.returncode != 0:
            print(install.stdout + install.stderr, file=sys.stderr)
            return 1
        record = home / ".config" / "opencode" / "dridh" / "plugin" / "dridh-bridge" / "dridh-bin"
        print(f"installed; dridh-bin records {record.read_text().strip()}")

        # Give the fresh OpenCode config the same local-only model boundary used by the parent Docker E2E. A real user
        # message switches OpenCode from its empty home route into a session route, which is where the side panel lives.
        config_dir = home / ".config" / "opencode"
        config_path = config_dir / "opencode.json"
        opencode_config = json.loads(config_path.read_text(encoding="utf-8"))
        if not args.first_run_only:
            opencode_config.update({
                "model": "dridhtest/scripted",
                "providers": {
                    "dridhtest": {
                        "name": "E2E local stub",
                        "package": "@opencode/ai/providers/openai-compatible",
                        "settings": {"baseURL": "http://127.0.0.1:18090/v1", "apiKey": "e2e-placeholder-not-a-credential"},
                        "models": {"scripted": {"name": "Scripted"}},
                    }
                },
            })
        config_path.write_text(json.dumps(opencode_config, indent=2) + "\n", encoding="utf-8")

        path = os.pathsep.join(p for p in ("/usr/local/bin", "/usr/bin", "/bin") if Path(p) != dridh.parent)
        env = (f"env -i HOME={home} OPENCODE_CONFIG_DIR={config_dir} PATH={path} TERM=xterm-256color "
               "COLORTERM=truecolor LANG=C.UTF-8 OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1")
        subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True, check=False)  # noqa: S603, S607
        route = f"--session {OPENCODE_SESSION} {project}" if not args.first_run_only else str(project)
        tmux("new-session", "-d", "-s", SESSION, "-x", str(WIDTH), "-y", str(HEIGHT),
             f"{env} {opencode} --standalone {route} 2>{Path(tmp) / 'opencode.err'}")
        required = FIRST_RUN_REQUIRED_SCREENSHOTS if args.first_run_only else FULL_REQUIRED_SCREENSHOTS
        smoke = Smoke(out, require_side_by_side=not args.first_run_only, required_screenshots=required)
        raw_json = ('{"ok"', '"error":')
        try:
            if args.first_run_only:
                smoke.wait("1-first-run", "dridh · type /dridh to set up", timeout=120)
                keys("/dridh")
                time.sleep(1)
                keys("Enter")
                smoke.wait("2-setup-choice", "Set up Dridh", capture=False)
                keys("Enter")
                smoke.wait("3-set-up", "Setup · step 2 of 2")
                keys("Escape")
                smoke.wait("4-ready", "dridh · setup 2/2", forbid=raw_json)
                keys("Escape")
                keys(f"/cd {other}")
                time.sleep(1)
                keys("Enter", "Enter")
                smoke.wait("5-cd-status", "dridh · type /dridh to set up", capture=False)
                keys("/dridh")
                time.sleep(1)
                keys("Enter")
                smoke.wait("6a-setup-choice-existing", "Set up Dridh", capture=False)
                keys("Down", "Enter")
                smoke.wait("7-indexed", f"Code indexed ({OTHER_FILES} files)", timeout=300)
                first_run_db = other / ".opencode" / "dridh" / "state" / "dridh.sqlite"
                corrupt = first_run_db.with_name("dridh.sqlite.corrupt")
                corrupt.write_text("not a database, written by the smoke test to cause a real Dridh error")
                os.replace(corrupt, first_run_db)
                for suffix in ("-wal", "-shm"):
                    Path(f"{first_run_db}{suffix}").unlink(missing_ok=True)
                smoke.wait("8-error-message", "dridh · The workflow state database could not be opened", forbid=raw_json)
                return 0

            # OpenCode is launched directly into a session, so the plugin's views live in its right panel, leaving the
            # conversation visible on the left. Setup-choice dialogs are asserted but intentionally not saved.
            smoke.wait("0-session-route", "Ask anything", timeout=120, capture=False)
            keys("Start this TUI screenshot session.", "Enter")
            smoke.wait("0-session-active", "NO_SCRIPT_FOR_ROLE", timeout=120, capture=False)

            keys("/dridh")
            time.sleep(1)
            keys("Enter")
            smoke.wait("2-setup-choice", "Set up Dridh", capture=False)

            keys("Enter")  # the first option: New codebase
            smoke.wait("3-set-up", "Setup · step 2 of 2")
            workspace = json.loads((project / ".opencode" / "dridh" / "workspace.json").read_text())
            if workspace.get("mode") != "new":
                raise StepFailed(f"3-set-up: workspace mode is {workspace.get('mode')!r}, expected 'new'")

            smoke.wait("4-ready", "dridh · setup 2/2", forbid=raw_json)

            # /cd: OpenCode moves to another folder and the TUI must follow it, not keep the one it started in.
            keys("Escape")  # close only the Dridh panel; the OpenCode session remains active
            keys(f"/cd {other}")
            time.sleep(1)
            keys("Enter", "Enter")
            # In an active session the short status is "not set up"; the first-run home route checks the full prompt.
            smoke.wait("5-cd-status", "dridh · not set up", capture=False)

            keys("/dridh")
            time.sleep(1)
            keys("Enter")
            smoke.wait("6a-setup-choice-existing", "Set up Dridh", capture=False)
            keys("Down", "Enter")  # the second option: Existing codebase
            smoke.glimpse("6b-indexing-in-the-background", rf"[\u2800-\u28ff] .*indexing \d+/{OTHER_FILES} files")  # spinner + progress
            smoke.glimpse("6c-working-in-the-panel", r"[\u2800-\u28ff] indexing the code", timeout=5)  # the panel's spinner, below the steps
            smoke.wait("7-indexed", f"Code indexed ({OTHER_FILES} files)", timeout=240, forbid=raw_json)
            smoke.wait("7a-setup-step-3", "Setup · step 3 of 3")
            workspace = json.loads((other / ".opencode" / "dridh" / "workspace.json").read_text())
            if workspace.get("mode") != "existing":
                raise StepFailed(f"7-indexed: workspace mode is {workspace.get('mode')!r}, expected 'existing'")
            smoke.wait("7b-ready-in-other-folder", "dridh · setup 3/3", forbid=raw_json)
            if f"/{other.name} " not in screen():
                last_screen = screen()
                smoke.save_failure("7b-FAILED-folder", last_screen)
                raise StepFailed(f"7b-ready-in-other-folder: OpenCode is no longer in the folder /cd moved to\nLast screen:\n{last_screen[-1500:]}")

            # An upgrade changes the search model or the embedded text: the stored vectors are then stale (made as an
            # older release made them), and the panel re-indexes in the background on its own.
            database = other / ".opencode" / "dridh" / "state" / "dridh.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE code_embedding SET model='BAAI/bge-small-en-v1.5'")
            smoke.wait("7c-search-model-changed", "Code search was improved", forbid=raw_json)
            smoke.glimpse("7d-re-indexing", r"[\u2800-\u28ff] .*indexing", timeout=5)
            smoke.wait("7e-re-indexed", "Code search is up to date", timeout=240, forbid=raw_json)
            with closing(sqlite3.connect(database)) as connection:
                stored = {row[0] for row in connection.execute("SELECT DISTINCT model FROM code_embedding")}
            if not stored or any(model.startswith("BAAI/") for model in stored):
                raise StepFailed(f"7e-re-indexed: the vectors were not rebuilt ({sorted(stored)})")

            # Code changed outside Dridh (an editor, git pull): once the files hold still, the panel updates the index
            # in the background, without being asked and without a toast.
            (other / "added_outside.py").write_text("def refund_order(order_id):\n    return order_id\n")
            (other / "app.py").unlink()
            deadline = time.monotonic() + 90
            while True:
                with closing(sqlite3.connect(database)) as connection:
                    names = {row[0] for row in connection.execute(
                        "SELECT e.name FROM code_entity e JOIN code_file f ON f.id=e.file_id WHERE f.index_run_id=("
                        "SELECT id FROM code_index_run WHERE status='SUCCEEDED' ORDER BY started_at DESC, rowid DESC LIMIT 1)")}
                # ... and the background run has finished (no spinner), so step 8 never corrupts the database under it
                if "refund_order" in names and "greet" not in names and not re.search(r"[\u2800-\u28ff] .*indexing", screen()):
                    smoke.save("7f-outside-change-indexed")
                    smoke.steps.append({"step": "7f-outside-change-indexed", "saw": "refund_order indexed, greet removed"})
                    print("ok  7f-outside-change-indexed: the new file is indexed and the deleted one is gone", flush=True)
                    break
                if time.monotonic() > deadline:
                    last_screen = screen()
                    smoke.save_failure("7f-outside-change-indexed-FAILED", last_screen)
                    raise StepFailed(f"7f-outside-change-indexed: the change made outside Dridh was not indexed within 90 s\nLast screen:\n{last_screen[-1500:]}")
                time.sleep(1)

            # A new file, not the old one overwritten: the panel refreshes every 5 s, and a `dridh ui state` that still has
            # the database open checkpoints into the file it opened when it closes. Overwritten in place, that file got its
            # pages back and the "corrupted" database worked again (an intermittent CI failure of this step).
            corrupt = database.with_name("dridh.sqlite.corrupt")
            corrupt.write_text("not a database, written by the smoke test to cause a real Dridh error")
            os.replace(corrupt, database)
            for suffix in ("-wal", "-shm"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)
            smoke.wait("8-error-message", "dridh · The workflow state database could not be opened", forbid=raw_json)
            if args.feature_project:
                keys("Escape")  # return focus to the OpenCode conversation before changing project roots
                capture_feature_views(smoke, Path(args.feature_project).resolve())
        except StepFailed as failure:
            print(f"FAIL {failure}", file=sys.stderr)
            err = Path(tmp) / "opencode.err"
            if err.is_file():
                print(err.read_text()[-2000:], file=sys.stderr)
            return 1
        finally:
            subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True, check=False)  # noqa: S603, S607
            (out / "steps.json").write_text(json.dumps({
                "required_screenshots": list(smoke.required_screenshots),
                "steps": smoke.steps,
            }, indent=2) + "\n")

    print(f"all {len(smoke.steps)} steps passed; screens in {out}")
    return 0


def capture_feature_views(smoke: Smoke, project: Path) -> None:
    """Capture the real document, checklist, coverage, metrics and execution views from seeded E2E state."""
    if not project.is_dir():
        raise StepFailed(f"feature project does not exist: {project}")
    keys(f"/cd {project}")
    time.sleep(1)
    keys("Enter", "Enter")
    keys("/dridh")
    time.sleep(1)
    keys("Enter")
    smoke.wait("9-feature-overview", "DOCUMENTS", forbid=('"ok"', '"error":'))

    keys("1")
    smoke.wait("10-use-cases-document", "OUTLINE", forbid=('"ok"', '"error":'))
    keys("o", "2")  # overview tab, then the design document
    smoke.wait("11-technical-design-document", "OUTLINE", forbid=('"ok"', '"error":'))
    keys("h")
    smoke.wait("12-review-checklist", "checklist", forbid=('"ok"', '"error":'))
    keys("m")
    smoke.wait("13-observability-metrics", "Model usage by stage", forbid=('"ok"', '"error":'))
    keys("v")
    smoke.wait("14-completion-matrix-deviation", "LDG-", forbid=('"ok"', '"error":'))
    keys("l")
    smoke.wait("15-execution-lot-review", "lot", forbid=('"ok"', '"error":'))


if __name__ == "__main__":
    sys.exit(main())
