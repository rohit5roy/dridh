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

Dridh is installed into a throwaway HOME with `dridh install`, and OpenCode is started with a PATH that does NOT
contain dridh, so the plugin must find it through the installer's record. No model or credential is used.

Each step waits for exact text on the screen (polling, not fixed sleeps) and fails after a timeout. Every step saves
the screen as .txt, .ansi and, when Pillow is available, .png, also on failure.

Usage: tui_smoke.py --opencode PATH --dridh PATH --out DIR
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

WIDTH, HEIGHT = 160, 45
SESSION = "dridh-tui-smoke"
OTHER_FILES = 1500

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
    pad = 24
    image = Image.new("RGB", (WIDTH * cell_w + 2 * pad, HEIGHT * cell_h + 2 * pad), DEFAULT_BG)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(parse_ansi(ansi)):
        for x, (ch, fg, bg, is_bold) in enumerate(row[:WIDTH]):
            left, top = pad + x * cell_w, pad + y * cell_h
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
    def __init__(self, out: Path) -> None:
        self.out = out
        self.steps: list[dict[str, str]] = []

    def save(self, name: str) -> None:
        text, ansi = screen(), screen(ansi=True)
        (self.out / f"{name}.txt").write_text(text, encoding="utf-8")
        (self.out / f"{name}.ansi").write_text(ansi, encoding="utf-8")
        render_png(ansi, self.out / f"{name}.png")

    def glimpse(self, name: str, needle: str, timeout: float = 20) -> bool:
        """Saves the screen when the regular expression NEEDLE matches, for screenshots of short-lived states; never fails the run."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if re.search(needle, screen()):
                self.save(name)
                print(f"ok  {name}: {needle} (seen)", flush=True)
                return True
            time.sleep(0.1)
        print(f"--  {name}: {needle} was not seen (it may have finished first)", flush=True)
        return False

    def wait(self, name: str, needle: str, timeout: float = 60, forbid: tuple[str, ...] = ()) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = screen()
            if needle in text:
                for bad in forbid:
                    if bad in text:
                        self.save(f"{name}-FAILED")
                        raise StepFailed(f"{name}: the screen shows {bad!r}")
                self.save(name)
                self.steps.append({"step": name, "saw": needle})
                print(f"ok  {name}: {needle}", flush=True)
                return
            time.sleep(0.5)
        self.save(f"{name}-FAILED")
        raise StepFailed(f"{name}: {needle!r} did not appear within {timeout:.0f} s")


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
        install = subprocess.run([str(dridh), "install"], env={**os.environ, "HOME": str(home)},  # noqa: S603
                                 capture_output=True, text=True, timeout=300, check=False)
        if install.returncode != 0:
            print(install.stdout + install.stderr, file=sys.stderr)
            return 1
        record = home / ".config" / "opencode" / "dridh" / "plugin" / "dridh-bridge" / "dridh-bin"
        print(f"installed; dridh-bin records {record.read_text().strip()}")

        path = os.pathsep.join(p for p in ("/usr/local/bin", "/usr/bin", "/bin") if Path(p) != dridh.parent)
        env = f"env -i HOME={home} PATH={path} TERM=xterm-256color COLORTERM=truecolor LANG=C.UTF-8"
        subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True, check=False)  # noqa: S603, S607
        tmux("new-session", "-d", "-s", SESSION, "-x", str(WIDTH), "-y", str(HEIGHT),
             f"{env} {opencode} --standalone {project} 2>{Path(tmp) / 'opencode.err'}")
        smoke = Smoke(out)
        raw_json = ('{"ok"', '"error":')
        try:
            smoke.wait("1-first-run", "dridh · type /dridh to set up", timeout=120, forbid=raw_json)

            keys("/dridh")
            time.sleep(1)
            keys("Enter")
            smoke.wait("2-setup-choice", "Set up Dridh")

            keys("Enter")  # the first option: New codebase
            smoke.wait("3-set-up", "Setup · step 2 of 2")
            workspace = json.loads((project / ".opencode" / "dridh" / "workspace.json").read_text())
            if workspace.get("mode") != "new":
                raise StepFailed(f"3-set-up: workspace mode is {workspace.get('mode')!r}, expected 'new'")

            keys("Escape")
            smoke.wait("4-ready", "dridh · setup 2/2", forbid=raw_json)

            # /cd: OpenCode moves to another folder and the TUI must follow it, not keep the one it started in.
            keys(f"/cd {other}")
            time.sleep(1)
            keys("Enter", "Enter")
            smoke.wait("5-cd-other-folder", "dridh · type /dridh to set up", forbid=raw_json)

            keys("/dridh")
            time.sleep(1)
            keys("Enter")
            smoke.wait("6a-setup-choice-existing", "Set up Dridh")
            keys("Down", "Enter")  # the second option: Existing codebase
            smoke.glimpse("6b-indexing-in-the-background", rf"[\u2800-\u28ff] .*indexing \d+/{OTHER_FILES} files")  # spinner + progress
            smoke.glimpse("6c-working-in-the-panel", r"[\u2800-\u28ff] indexing the code", timeout=5)  # the panel's spinner, below the steps
            smoke.wait("7-indexed", f"Code indexed ({OTHER_FILES} files)", timeout=240, forbid=raw_json)
            smoke.wait("7a-setup-step-3", "Setup · step 3 of 3")
            workspace = json.loads((other / ".opencode" / "dridh" / "workspace.json").read_text())
            if workspace.get("mode") != "existing":
                raise StepFailed(f"7-indexed: workspace mode is {workspace.get('mode')!r}, expected 'existing'")
            keys("Escape")
            smoke.wait("7b-ready-in-other-folder", "dridh · setup 3/3", forbid=raw_json)
            if f"/{other.name} " not in screen():
                smoke.save("7b-FAILED-folder")
                raise StepFailed("7b-ready-in-other-folder: OpenCode is no longer in the folder /cd moved to")

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
                    smoke.save("7f-outside-change-indexed-FAILED")
                    raise StepFailed("7f-outside-change-indexed: the change made outside Dridh was not indexed within 90 s")
                time.sleep(1)

            for suffix in ("-wal", "-shm"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)
            database.write_text("not a database, written by the smoke test to cause a real Dridh error")
            smoke.wait("8-error-message", "dridh · The workflow state database could not be opened", forbid=raw_json)
        except StepFailed as failure:
            print(f"FAIL {failure}", file=sys.stderr)
            err = Path(tmp) / "opencode.err"
            if err.is_file():
                print(err.read_text()[-2000:], file=sys.stderr)
            return 1
        finally:
            subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True, check=False)  # noqa: S603, S607
            (out / "steps.json").write_text(json.dumps(smoke.steps, indent=2) + "\n")

    print(f"all {len(smoke.steps)} steps passed; screens in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
