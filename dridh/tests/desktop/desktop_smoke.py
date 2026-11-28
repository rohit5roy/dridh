#!/usr/bin/env python3
"""Drive the real OpenCode desktop app with Dridh installed, check what Dridh records, and save screenshots.

Runs the packaged app (an extracted AppImage) on a virtual screen (Xvfb), with a throwaway HOME:

1. `dridh install` puts the plugin, including its desktop part, into the HOME's OpenCode configuration.
2. A project in a feature folder is seeded: approved use cases, a technical design waiting for review with 8 of its 10
   checklist items ticked (the HOME itself is the project, so the app's own sessions open in it).
3. The app opens the session; the Dridh tab, the reading view, Request changes, the checklist, Approve with the typed
   confirmation, and the All documents tree are driven with the mouse and keyboard.

The panel cannot be read from outside the app, so each step that changes something is checked against what Dridh
recorded: both missing checklist items are ticked, and the design is approved under the reviewer typed in the dialog.
Screenshots of every step go to --out with steps.json. Exit code 0 only when every check passed.

Usage: desktop_smoke.py --app DIR --dridh PATH --out DIR [--repo DIR]
  --app    the extracted AppImage (the folder holding ai.opencode)
  --dridh  the dridh command of the environment to test (its Python seeds the project)
  --repo   the dridh/ folder of the checkout (the seed helper lives in tests/e2e), default: two levels up
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WIDTH, HEIGHT = 1280, 800
DISPLAY = ":97"
REVIEWER = "CI Reviewer"
FEATURE = "order-intake"

# Screen positions in a 1280x800 window of OpenCode 2.4.2 (the app fills the screen; there is no window manager).
SESSIONS = (67, 21)            # the sessions list button
FIRST_SESSION = (518, 215)     # the first session in the list
SIDE_PANEL = (1245, 67)        # toggles the side panel; the Dridh tab opens by itself
REVIEW_BUTTON = (618, 303)     # "Review the technical design" on the overview
REQUEST_CHANGES = (1076, 766)
PUT_IN_MESSAGE_BOX = (769, 539)
CHECKLIST_LINK = (590, 766)    # "2 checklist items left to check" scrolls to the checklist
UNTICKED = ((523, 516), (523, 651))
APPROVE = (1208, 766)
BACK = (496, 124)


class Failed(Exception):
    pass


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)  # noqa: S603


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--dridh", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    app, dridh, out, repo = Path(args.app).resolve(), Path(args.dridh).resolve(), Path(args.out).resolve(), Path(args.repo).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for tool in ("Xvfb", "xdotool", "import"):
        if shutil.which(tool) is None:
            print(f"{tool} is required", file=sys.stderr)
            return 2

    steps: list[dict[str, object]] = []
    home = Path(tempfile.mkdtemp(prefix="dridh-desktop-"))
    env = {**{k: v for k, v in os.environ.items() if k not in ("OPENCODE_CONFIG_DIR", "NODE_OPTIONS")},
           "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"), "DISPLAY": DISPLAY}
    xvfb = app_process = None
    code = 1

    def step(name: str, ok: bool = True, detail: object = "") -> None:
        shot = out / f"{name}.png"
        run(["import", "-window", "root", str(shot)], env=env)
        steps.append({"step": name, "passed": ok, "detail": detail, "screenshot": shot.name})
        print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
        if not ok:
            raise Failed(name)

    def click(point: tuple[int, int], wait: float = 2.0) -> None:
        run(["xdotool", "mousemove", str(point[0]), str(point[1]), "click", "1"], env=env)
        time.sleep(wait)

    def keys(*names: str, wait: float = 0.5) -> None:
        run(["xdotool", "key", *names], env=env)
        time.sleep(wait)

    def type_text(text: str, wait: float = 0.8) -> None:
        run(["xdotool", "type", "--delay", "30", text], env=env)
        time.sleep(wait)

    def state() -> dict:
        result = run([str(dridh), "ui", "state", "--json", "--project", str(home)], env=env)
        return json.loads(result.stdout)

    try:
        installed = run([str(dridh), "install"], env=env, timeout=300)
        if installed.returncode != 0 or not (home / ".config/opencode/dridh/plugin/dridh-bridge/gui/gui-experimental.cjs").is_file():
            raise Failed(f"dridh install did not place the desktop part: {installed.stdout[-400:]}{installed.stderr[-400:]}")
        seed = run([str(dridh.with_name("python")), "-c",
                    "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[2]);"
                    "from tests.e2e.planner_test_helpers import seed_approved_baseline;"
                    "seed_approved_baseline(Path(sys.argv[1]), feature='Order intake', design_approved=False, design_ticked=8)",
                    str(home), str(repo)], env=env, timeout=300)
        if seed.returncode != 0:
            raise Failed(f"seeding failed: {seed.stderr[-800:]}")
        run([str(dridh), "feature", "sync", FEATURE, "--json", "--project", str(home)], env=env)
        before = state()
        if before.get("human_action", {}).get("kind") != "checklist":
            raise Failed(f"the seeded project is not waiting for review: {before.get('human_action')}")

        xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-ac"],  # noqa: S603
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        log = (out / "app.log").open("w")
        app_process = subprocess.Popen([str(app / "ai.opencode"), "--no-sandbox", "--disable-gpu", "--force-device-scale-factor=1"],  # noqa: S603
                                       cwd=app, env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 120
        while time.time() < deadline and not run(["xdotool", "search", "--name", "OpenCode"], env=env).stdout.strip():
            time.sleep(2)
        time.sleep(25)  # the app starts its background server and loads the plugin's desktop part
        step("1-app-started")

        # A session in the project (the HOME), created through the app's own server, then opened in the app.
        cli = app / "resources" / "opencode-cli"
        created = None
        for _ in range(10):
            created = run([str(cli), "api", "POST", "/api/session", "-d", json.dumps({"title": "Order intake design review"})],
                          env=env, cwd=home, timeout=60)
            if created.returncode == 0:
                break
            time.sleep(3)
        if created is None or created.returncode != 0:
            raise Failed(f"could not create a session: {created.stderr[-400:] if created else ''}")
        click(SESSIONS, 3)
        click(FIRST_SESSION, 6)
        click(SIDE_PANEL, 8)
        step("2-dridh-tab-overview")

        click(REVIEW_BUTTON, 5)
        step("3-design-reading-view")

        click(REQUEST_CHANGES, 2)
        type_text("Payment retries are not covered. Add what happens when the payment provider times out.")
        click(PUT_IN_MESSAGE_BOX, 3)
        step("4-request-changes-in-message-box")
        keys("Escape")

        click(CHECKLIST_LINK, 3)
        step("5-review-checklist")
        for point in UNTICKED:
            click(point, 4)
        review = state()["documents"]["design"]["review"]
        step("6-checklist-ticked", review["ticked"] == review["total"] == 10, f"{review['ticked']} of {review['total']} ticked")

        challenge = run([str(dridh.with_name("python")), "-c",
                         "import sys; from pathlib import Path; from dridh.workspace.layout import open_workspace;"
                         "from dridh.designer.service import DesignerService; s=DesignerService(open_workspace(Path(sys.argv[1])));"
                         "print(s.approval_challenge(sys.argv[2])['confirmation'])",
                         str(home), str(before["documents"]["design"]["session_id"])], env=env).stdout.strip()
        click(APPROVE, 3)
        keys("ctrl+a")
        type_text(REVIEWER)
        step("7-approve-name")
        keys("Return", wait=7)
        step("8-approve-typed-confirmation")
        type_text(challenge, wait=1.5)
        keys("Return", wait=7)
        after = state()
        approved_by = after["documents"]["design"]["review"].get("approved_by")
        step("9-approved", after["documents"]["design"]["status"] == "APPROVED" and approved_by == REVIEWER,
             f"status {after['documents']['design']['status']}, approved by {approved_by}, feature {after['feature']['stage']}")

        click(BACK, 5)
        tree = json.loads(run([str(dridh), "ui", "documents", "--project", str(home)], env=env).stdout)
        names = [node["name"] for node in tree["features"][0]["tree"]]
        step("10-all-documents-tree", names[:2] == ["001-USE-CASES.md", "002-TECHNICAL-DESIGN.md"], f"{tree['features'][0]['folder']}: {names}")
        code = 0
    except Failed as failure:
        print(f"FAIL {failure}", file=sys.stderr)
        if not steps or steps[-1]["passed"]:
            run(["import", "-window", "root", str(out / "failure.png")], env=env)
        code = 1
    finally:
        (out / "steps.json").write_text(json.dumps({"steps": steps, "passed": code == 0}, indent=2) + "\n")
        for process in (app_process, xvfb):
            if process is not None:
                process.terminate()
        run(["pkill", "-f", str(app / "resources" / "opencode-cli")])
        shutil.rmtree(home, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
