"""install.sh checks every prerequisite before changing anything, plans what it will install, and refuses to go on
without OpenCode. Runs offline with stub tools and --check, so nothing is downloaded or installed. The embedding-model
step is tested by running the script's own model functions on archives built here."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
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


# --- the embedding model --------------------------------------------------------------------------------------
# install.sh downloads dridh-embedding-model.tar.gz from the release, checks its .sha256 and unpacks it into the model
# cache. These tests run the script's own model functions (between its "embedding model" markers) on real archives.


def _model_functions() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start, end = text.index("# >>> embedding model"), text.index("# <<< embedding model")
    helpers = "\n".join(line for line in text.splitlines() if line.startswith(("die() ", "say() ", "have() ", "sha256_check() ")))
    return helpers + "\n" + text[start:end]


def _run_model(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/nonexistent")}
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + _model_functions() + "\n" + script],  # noqa: S603, S607
                          env={**base, **(env or {})}, capture_output=True, text=True, timeout=60, check=False)


def _archive(directory: Path, members: list[tuple[str, bytes | None, str | None]], checksum: str | None = None) -> Path:
    """Writes dridh-embedding-model.tar.gz and its .sha256 into DIRECTORY. A member is (name, data, symlink target)."""
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "dridh-embedding-model.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data, target in members:
            info = tarfile.TarInfo(name)
            if target is not None:
                info.type, info.linkname = tarfile.SYMTYPE, target
                tar.addfile(info)
            else:
                payload = data or b""
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    digest = checksum or hashlib.sha256(archive.read_bytes()).hexdigest()
    (directory / "dridh-embedding-model.tar.gz.sha256").write_text(f"{digest}  dridh-embedding-model.tar.gz\n")
    return directory


MODEL_LAYOUT = [
    ("dridh-embedding-model.json", json.dumps({"model": "BAAI/bge-small-en-v1.5"}).encode(), None),
    ("models--Qdrant--bge-small-en-v1.5-onnx-Q/blobs/abc", b"onnx bytes", None),
    ("models--Qdrant--bge-small-en-v1.5-onnx-Q/refs/main", b"aa8f8b0", None),
    ("models--Qdrant--bge-small-en-v1.5-onnx-Q/snapshots/aa8f8b0/model_optimized.onnx", None, "../../blobs/abc"),
]


def test_the_model_is_checked_unpacked_and_not_unpacked_twice(tmp_path: Path) -> None:
    source, cache = _archive(tmp_path / "release", MODEL_LAYOUT), tmp_path / "cache"
    result = _run_model(f'model_is_current "{source}" "{cache}" && exit 9; unpack_model "{source}" "{cache}"; model_is_current "{source}" "{cache}"')
    assert result.returncode == 0, result.stderr
    snapshot = cache / "models--Qdrant--bge-small-en-v1.5-onnx-Q" / "snapshots" / "aa8f8b0" / "model_optimized.onnx"
    assert snapshot.is_symlink() and snapshot.read_bytes() == b"onnx bytes", "the snapshot link resolves inside the cache"
    assert (cache / "dridh-embedding-model.tar.gz.sha256").read_text() == (source / "dridh-embedding-model.tar.gz.sha256").read_text()


def test_a_checksum_mismatch_unpacks_nothing(tmp_path: Path) -> None:
    source, cache = _archive(tmp_path / "release", MODEL_LAYOUT, checksum="0" * 64), tmp_path / "cache"
    result = _run_model(f'unpack_model "{source}" "{cache}"')
    assert result.returncode != 0
    assert "checksum mismatch for dridh-embedding-model.tar.gz; nothing was unpacked" in result.stderr
    assert not cache.exists()


@pytest.mark.parametrize("member", [("../escaped", b"x", None), ("/absolute", b"x", None), ("link", None, "../../outside")])
def test_members_that_would_land_outside_the_cache_are_refused(tmp_path: Path, member: tuple[str, bytes | None, str | None]) -> None:
    source, cache = _archive(tmp_path / "release", [*MODEL_LAYOUT, member]), tmp_path / "cache"
    result = _run_model(f'unpack_model "{source}" "{cache}"')
    assert result.returncode != 0
    assert "refusing archive member" in result.stderr
    assert not (tmp_path / "escaped").exists() and not (cache / "dridh-embedding-model.json").exists(), "nothing was unpacked"


def test_the_model_cache_is_where_dridh_looks(tmp_path: Path) -> None:
    from dridh.code_reacher.embeddings import model_cache_dir

    for env in ({"HOME": str(tmp_path)}, {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
                {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "xdg"), "FASTEMBED_CACHE_PATH": str(tmp_path / "fe")}):
        shell = _run_model("model_cache_dir", env).stdout.strip()
        saved = {key: os.environ.get(key) for key in ("HOME", "XDG_CACHE_HOME", "FASTEMBED_CACHE_PATH")}
        try:
            for key in saved:
                os.environ.pop(key, None)
            os.environ.update(env)
            assert shell == model_cache_dir(), env
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_the_plan_names_where_the_model_comes_from(tmp_path: Path) -> None:
    path = _bin(tmp_path, opencode="opencode v2.2.6", git="git version 2.43.0", curl="curl")
    release = _check(tmp_path, path, "--no-glow", "--release", "v1.2.3")
    assert "model     install  from the release (dridh-embedding-model.tar.gz, checksum-verified) into " in release.stdout
    assert f"{tmp_path / 'home'}/.cache/dridh/fastembed" in release.stdout
    assert "model     skip     --no-embeddings" in _check(tmp_path, path, "--no-glow", "--no-embeddings").stdout
    assert "model     later    FastEmbed downloads it on first use" in _check(tmp_path, path, "--no-glow").stdout  # a checkout
    local = _archive(tmp_path / "offline", MODEL_LAYOUT) / "dridh-embedding-model.tar.gz"
    assert f"model     install  from {local} into " in _check(tmp_path, path, "--no-glow", "--model", str(local)).stdout
