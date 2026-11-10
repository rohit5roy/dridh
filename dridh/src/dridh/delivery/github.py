"""GitHub access behind a Python adapter (plan 16.3). Only REST endpoints are used, through the ``gh`` CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any, Protocol

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact

_REMOTE = re.compile(r"(?:github\.com[:/])(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")


def parse_remote(url: str) -> tuple[str, str]:
    match = _REMOTE.search(url.strip())
    if not match:
        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The git remote is not a GitHub repository.", details=(url.split("@")[-1][:120],))
    return match["owner"], match["repo"]


class GitHubAdapter(Protocol):
    def create_pr(self, *, title: str, body: str, head: str, base: str, draft: bool = False) -> dict[str, Any]: ...
    def pr_view(self, number: int) -> dict[str, Any]: ...
    def check_runs(self, sha: str) -> list[dict[str, Any]]: ...


Runner = Callable[[list[str], str | None], str]


def _run_gh(argv: list[str], stdin: str | None) -> str:
    done = subprocess.run(["gh", *argv], input=stdin, capture_output=True, text=True, check=False, timeout=120)
    if done.returncode != 0:
        raise DridhError(ErrorCode.PROCESS_EXECUTION_FAILED, "A GitHub CLI call failed.", details=(str(redact(done.stderr.strip()))[:300],),
                         retryable=True)
    return done.stdout


class GhCli:
    """``gh api`` based adapter. ``runner`` is injectable so tests never touch the network."""

    def __init__(self, owner: str, repo: str, runner: Runner = _run_gh) -> None:
        self.owner, self.repo, self._run = owner, repo, runner

    def _api(self, path: str, *fields: str, method: str = "GET", stdin: str | None = None) -> Any:
        argv = ["api", "-X", method, f"repos/{self.owner}/{self.repo}/{path}", *fields]
        out = self._run(argv, stdin)
        return json.loads(out) if out.strip() else {}

    def create_pr(self, *, title: str, body: str, head: str, base: str, draft: bool = False) -> dict[str, Any]:
        payload = json.dumps({"title": title, "body": body, "head": head, "base": base, "draft": draft})
        data = self._api("pulls", "--input", "-", method="POST", stdin=payload)
        return {"number": data["number"], "url": data["html_url"], "head_sha": data["head"]["sha"], "base_sha": data["base"]["sha"]}

    def pr_view(self, number: int) -> dict[str, Any]:
        data = self._api(f"pulls/{number}")
        return {"state": "MERGED" if data.get("merged") else data["state"].upper(), "head_sha": data["head"]["sha"],
                "base_ref": data["base"]["ref"], "merged": bool(data.get("merged"))}

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        data = self._api(f"commits/{sha}/check-runs?per_page=100")
        return [{"name": run["name"], "status": run["status"], "conclusion": run.get("conclusion"), "url": run.get("html_url"),
                 "run_id": str(run["id"]), "started_at": run.get("started_at"), "completed_at": run.get("completed_at"),
                 "summary": " ".join(str((run.get("output") or {}).get(key) or "") for key in ("title", "summary")).strip()[:500]}
                for run in data.get("check_runs", [])]
