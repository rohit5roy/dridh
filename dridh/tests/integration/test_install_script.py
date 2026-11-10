"""install.sh checks every prerequisite before changing anything, plans what it will install, and refuses to go on
without OpenCode. Runs offline with stub tools and --check, so nothing is downloaded or installed."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "install.sh"
BASICS = ("bash", "sh", "env", "dirname", "grep", "head", "awk", "sed", "printf", "uname", "id", "cat", "mkdir", "mktemp", "rm", "tr", "sort")

pytestmark = pytest.mark.skipif(not SCRIPT.is_file() or shutil.which("bash") is None, reason="install.sh needs bash")


def _stub(directory: Path, name: str, output: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _bin(tmp_path: Path, **stubs: str) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    for tool in BASICS:
        found = shutil.which(tool)
        if found:
            (directory / tool).symlink_to(found)
    for name, output in stubs.items():
        _stub(directory, name.replace("_", "."), output)
    return directory


def _check(tmp_path: Path, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(["bash", str(SCRIPT), "--check", *args], env={"PATH": str(path), "HOME": str(home)},  # noqa: S603, S607
                          capture_output=True, text=True, timeout=60, check=False)


def test_missing_opencode_stops_before_anything_changes(tmp_path: Path) -> None:
    result = _check(tmp_path, _bin(tmp_path, git="git version 2.43.0", curl="curl"))
    assert result.returncode == 1
    assert "opencode  MISSING" in result.stdout
    assert "Dridh is a plugin for OpenCode, so nothing was changed" in result.stderr
    assert not (tmp_path / "home" / ".local").exists()


def test_everything_missing_is_planned_in_order(tmp_path: Path) -> None:
    result = _check(tmp_path, _bin(tmp_path, opencode="opencode v2.2.5", git="git version 2.43.0", curl="curl"))
    assert result.returncode == 0, result.stderr
    assert "uv        missing" in result.stdout and "python    missing" in result.stdout and "glow      missing" in result.stdout
    assert "will install: uv python glow" in result.stdout
    assert "--check: nothing was changed" in result.stdout
    assert not (tmp_path / "home" / ".local" / "bin").exists()


def test_present_tools_are_not_reinstalled_and_skips_are_honoured(tmp_path: Path) -> None:
    path = _bin(tmp_path, opencode="opencode v2.2.6", git="git version 2.43.0", curl="curl", uv="uv 0.11.32",
                python3_13="Python 3.13.1", glow="glow version 2.1.1")
    result = _check(tmp_path, path, "--no-embeddings")
    assert result.returncode == 0, result.stderr
    assert "opencode  ok       2.2.6" in result.stdout
    assert "fastembed skip" in result.stdout
    assert "nothing extra to install" in result.stdout


def test_missing_git_is_planned_and_no_downloader_is_reported(tmp_path: Path) -> None:
    result = _check(tmp_path, _bin(tmp_path, opencode="opencode v2.2.5"), "--no-glow")
    assert result.returncode == 0, result.stderr
    assert "git       missing" in result.stdout and "download  missing" in result.stdout
    assert "glow      skip" in result.stdout
    assert "will install: git uv python" in result.stdout


def test_help_lists_the_new_options() -> None:
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, check=False,  # noqa: S603, S607
                            env={**os.environ})
    assert result.returncode == 0
    for option in ("--check", "--no-embeddings", "--no-glow"):
        assert option in result.stdout


@pytest.mark.parametrize("version", ["2.2.5", "2.2.6", "2.3.0", "2.10.0"])
def test_opencode_2_2_5_or_later_is_accepted(tmp_path: Path, version: str) -> None:
    result = _check(tmp_path, _bin(tmp_path, opencode=f"opencode v{version}", git="git version 2.43.0", curl="curl"), "--no-glow")
    assert result.returncode == 0, result.stderr
    assert f"opencode  ok       {version}" in result.stdout


def test_opencode_older_than_2_2_5_stops_before_anything_changes(tmp_path: Path) -> None:
    result = _check(tmp_path, _bin(tmp_path, opencode="opencode v2.2.4", git="git version 2.43.0", curl="curl"))
    assert result.returncode == 1
    assert "opencode  OLD      2.2.4 (Dridh needs 2.2.5 or later)" in result.stdout
    assert "older than 2.2.5, so nothing was changed" in result.stderr
    assert not (tmp_path / "home" / ".local").exists()


def test_inside_a_checkout_it_builds_from_the_checkout(tmp_path: Path) -> None:
    result = _check(tmp_path, _bin(tmp_path, opencode="opencode v2.2.6", git="git version 2.43.0", curl="curl"), "--no-glow")
    assert "source    ok       build from the checkout at" in result.stdout


def test_piped_without_a_checkout_it_installs_the_latest_release(tmp_path: Path) -> None:
    """curl ... | bash: no script file, so the newest release is the source."""
    home = tmp_path / "home"
    home.mkdir()
    path = _bin(tmp_path, opencode="opencode v2.2.6", git="git version 2.43.0", curl="curl")
    result = subprocess.run(["bash", "-s", "--", "--check", "--no-glow"], input=SCRIPT.read_text(encoding="utf-8"),  # noqa: S603, S607
                            env={"PATH": str(path), "HOME": str(home)}, cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert "source    ok       release latest of rohit5roy/dridh" in result.stdout


def test_help_shows_the_one_line_install() -> None:
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603, S607
    assert "releases/latest/download/install.sh | bash" in result.stdout
