"""`dridh init` never silently guesses whether a project is new or existing (plan section 3.2)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from dridh.cli import main as cli
from tests.contract.test_orchestration_bridge_contract import COORDINATOR, _call
from tests.integration.test_cli import run_module


def _plain(text: str) -> str:
    """Usage errors are rendered with colour and a box in CI: drop ANSI, box drawing and wrapping."""
    return re.sub(r"\s+", "", re.sub(r"[│╭╮╰╯─]", "", re.sub(r"\x1b\[[0-9;]*m", "", text)))


def _project(tmp_path: Path, *, git: bool) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if git:
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def _descriptor(project: Path) -> dict:
    return json.loads((project / ".opencode" / "dridh" / "workspace.json").read_text(encoding="utf-8"))


def test_non_interactive_init_without_a_mode_is_a_usage_error_and_creates_nothing(tmp_path):
    project = _project(tmp_path, git=True)  # a Git folder must not imply "existing"
    result = run_module("init", "--project", str(project), stdin="")
    assert result.returncode == 2
    assert "--modenew|existing" in _plain(result.stderr)
    assert not (project / ".opencode").exists()


@pytest.mark.parametrize("git", [True, False])
@pytest.mark.parametrize("mode", ["new", "existing"])
def test_explicit_mode_always_wins_over_the_folder_hint(tmp_path, git, mode):
    project = _project(tmp_path, git=git)
    result = run_module("init", "--project", str(project), "--mode", mode, stdin="")
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["created"] is True and document["mode_source"] == "flag"
    assert _descriptor(project)["mode"] == mode == document["workspace"]["mode"]


def test_invalid_mode_is_rejected(tmp_path):
    project = _project(tmp_path, git=False)
    result = run_module("init", "--project", str(project), "--mode", "maybe", stdin="")
    assert result.returncode == 2 and "mustbeoneof:new,existing" in _plain(result.stderr)
    assert not (project / ".opencode").exists()


def test_rerunning_init_on_an_existing_workspace_needs_no_mode_and_changes_nothing(tmp_path):
    project = _project(tmp_path, git=False)
    assert run_module("init", "--project", str(project), "--mode", "new", stdin="").returncode == 0
    again = run_module("init", "--project", str(project), stdin="")
    assert again.returncode == 0, again.stderr
    document = json.loads(again.stdout)
    assert document["created"] is False and document["mode_source"] == "existing-workspace"
    assert _descriptor(project)["mode"] == "new"


def test_conflicting_mode_on_an_existing_workspace_is_refused_not_applied(tmp_path):
    project = _project(tmp_path, git=False)
    run_module("init", "--project", str(project), "--mode", "new", stdin="")
    result = run_module("init", "--project", str(project), "--mode", "existing", stdin="")
    assert result.returncode != 0 and "was not changed" in result.stderr
    assert _descriptor(project)["mode"] == "new"


def test_interactive_init_prompts_and_offers_the_folder_hint_only_as_a_default(tmp_path, monkeypatch):
    project = _project(tmp_path, git=True)
    seen = {}

    def fake_prompt(text, **kwargs):
        seen.update(text=text, default=kwargs["default"], parse=kwargs["value_proc"])
        return kwargs["value_proc"]("  NEW ")  # the human overrides the suggestion

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.typer, "prompt", fake_prompt)
    assert cli._resolve_init_mode(project, None) == ("new", "prompt")
    assert seen["default"] == "existing" and "suggested" in seen["text"] and "new/existing" in seen["text"]
    with pytest.raises(cli.typer.BadParameter):
        seen["parse"]("maybe")


def test_bridge_init_uses_the_given_mode_and_reports_when_it_had_to_infer(tmp_path):
    asked = tmp_path / "asked"
    asked.mkdir()
    given = _call("workspace.init", asked, {"mode": "new"}, caller=COORDINATOR)
    assert given["ok"] and given["data"]["mode_inferred"] is False and given["data"]["workspace"]["mode"] == "new"

    guessed = tmp_path / "guessed"
    guessed.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=guessed, check=True)
    inferred = _call("workspace.init", guessed, caller=COORDINATOR)
    assert inferred["ok"] and inferred["data"]["mode_inferred"] is True and inferred["data"]["workspace"]["mode"] == "existing"

    bad = tmp_path / "bad"
    bad.mkdir()
    rejected = _call("workspace.init", bad, {"mode": "maybe"}, caller=COORDINATOR)
    assert rejected["ok"] is False and not (bad / ".opencode").exists()
