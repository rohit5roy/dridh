""".github/scripts/release_pr.sh pushes the release workflow's version step with RELEASE_BOT_TOKEN only.

actions/checkout leaves the job's GITHUB_TOKEN configured as an extra Authorization header for github.com. If the
script adds its own header on top, git sends both and GitHub rejects the push (HTTP 400, exit 128), which is how every
release failed at "Open the pull request that moves main to the next version". The test runs the real script against
a local HTTP server standing in for github.com and records the headers git sends.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / ".github" / "scripts" / "release_pr.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")


def _basic(token: str) -> str:
    return "basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


@pytest.fixture
def server():
    seen: list[list[str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            seen.append(self.headers.get_all("Authorization") or [])
            self.send_response(404)
            self.end_headers()

        do_GET = do_POST = _record

        def log_message(self, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()


def _checkout(tmp_path: Path, url: str) -> Path:
    """A clone as actions/checkout leaves it: origin on the server, the job token persisted as an extra header."""

    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q", str(repo)],
                 ["-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                  "commit", "-q", "--allow-empty", "-m", "Start 9.9.9rc1"],
                 ["-C", str(repo), "remote", "add", "origin", f"{url}/owner/repo"],
                 ["-C", str(repo), "config", f"http.{url}/.extraheader", f"AUTHORIZATION: {_basic('CHECKOUT_TOKEN')}"]):
        subprocess.run(["git", *args], check=True, env=env)  # noqa: S603, S607
    return repo


def test_the_push_sends_only_the_release_token(tmp_path: Path, server) -> None:
    url, seen = server
    repo = _checkout(tmp_path, url)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GITHUB_SERVER_URL": url, "RELEASE_BOT_TOKEN": "BOT_TOKEN"}
    result = subprocess.run(["bash", str(SCRIPT), "version-9.9.9rc2", "Start 9.9.9rc2"],  # noqa: S603, S607
                            cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode != 0  # the stand-in server has no repository; only the request matters here
    assert seen, result.stderr
    assert all(headers == [_basic("BOT_TOKEN")] for headers in seen), seen


def test_without_the_release_token_it_stops_before_pushing(tmp_path: Path, server) -> None:
    url, seen = server
    repo = _checkout(tmp_path, url)
    env = {k: v for k, v in os.environ.items() if k != "RELEASE_BOT_TOKEN"} | {"GITHUB_SERVER_URL": url}
    result = subprocess.run(["bash", str(SCRIPT), "version-9.9.9rc2", "Start 9.9.9rc2"],  # noqa: S603, S607
                            cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode != 0
    assert "RELEASE_BOT_TOKEN is not set" in result.stderr
    assert seen == []
