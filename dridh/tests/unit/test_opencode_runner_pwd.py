"""OpenCode takes its working directory from $PWD. The runner must point it at the worktree, never the caller's directory.

Found by a real-model run: the Executor's edits landed in the directory Dridh was started from, outside every worktree.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from dridh.integrations.opencode import OpenCodeRunner

FAKE = """#!{python}
import json, os
with open(os.environ["DRIDH_TEST_PWD_LOG"], "w") as handle:  # a POSIX shell would reset PWD itself; Python, like OpenCode, does not
    handle.write(os.environ.get("PWD", "") + "\\n" + os.getcwd() + "\\n")
for event in ({{"type": "step_start", "sessionID": "ses_0123456789abcdefghijklmnop", "part": {{"type": "step-start"}}}},
              {{"type": "text", "sessionID": "ses_0123456789abcdefghijklmnop", "part": {{"type": "text", "text": "ok"}}}}):
    print(json.dumps(event))
"""


def test_the_agent_runs_with_pwd_set_to_its_worktree_not_the_callers_directory(tmp_path: Path, monkeypatch) -> None:
    fake = tmp_path / "opencode"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    project, worktree, elsewhere = tmp_path / "project", tmp_path / "project" / "wt", tmp_path / "elsewhere"
    for path in (project, worktree, elsewhere):
        path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "pwd.log"
    monkeypatch.setenv("PWD", str(elsewhere))  # what an inherited shell PWD looks like
    monkeypatch.setenv("DRIDH_TEST_PWD_LOG", str(log))
    runner = OpenCodeRunner(executable=str(fake), workspace_root=project, timeout=30)
    for target in (None, worktree):
        try:
            runner.run_sync(agent="dridh/executor", prompt="go", worktree_path=target) if target else runner.run_sync(agent="dridh/executor", prompt="go")
        except Exception:  # noqa: BLE001 - only the environment the child saw matters here
            pass
        pwd, real = log.read_text().splitlines()
        expected = str((target or project).resolve())
        assert pwd == expected and os.path.realpath(real) == expected, (pwd, real)
