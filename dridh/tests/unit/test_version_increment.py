"""Release versions only move forward, automatically (scripts/version.py, used by the release workflow)."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "version.py"
v = runpy.run_path(str(SCRIPT))


def test_after_a_release_main_moves_to_the_next_candidate():
    assert v["next_after"]("0.7.9") == "0.8.0rc1"      # the first public release, then internal 0.8.0-rc.1
    assert v["next_after"]("0.8.0rc1") == "0.8.0rc2"
    assert v["next_after"]("0.8.0") == "0.9.0rc1"
    assert v["next_after"]("1.9.3rc12") == "1.9.3rc13"


def test_a_stable_release_of_a_candidate_drops_the_rc():
    assert v["release_of"]("0.8.0rc3", "stable") == "0.8.0"
    assert v["release_of"]("0.7.9", "stable") == "0.7.9"
    assert v["release_of"]("0.8.0rc3", "rc") == "0.8.0rc3"
    with pytest.raises(ValueError, match="not a release candidate"):
        v["release_of"]("0.7.9", "rc")


def test_the_whole_sequence_only_increases():
    released, current = [], "0.7.9"
    for kind in ("stable", "rc", "rc", "stable", "rc"):
        version = v["release_of"](current, kind)
        assert v["check_newer"](version, released) is None
        released.append(f"v{version}")
        current = v["next_after"](version)
    assert released == ["v0.7.9", "v0.8.0rc1", "v0.8.0rc2", "v0.8.0", "v0.9.0rc1"]
    assert sorted(released, key=v["order_key"]) == released


def test_check_newer_refuses_a_lower_or_equal_version():
    assert v["check_newer"]("0.8.0rc1", ["v0.7.9", "not-a-version"]) is None
    assert "not newer" in v["check_newer"]("0.8.0rc1", ["v0.8.0rc1"])
    assert "not newer" in v["check_newer"]("0.8.0rc2", ["v0.8.0"])  # a candidate is older than its release
    assert v["check_newer"]("0.1.0", []) is None


def test_semver_and_pep440_forms():
    assert v["semver"]("0.8.0rc1") == "0.8.0-rc.1" and v["semver"]("0.7.9") == "0.7.9"
    assert v["parse"]("v0.8.0rc1") == (0, 8, 0, 1)
    for bad in ("0.8", "0.8.0-rc.1", "0.8.0.RC-1", "1.0.0b1"):
        with pytest.raises(ValueError):
            v["parse"](bad)


def test_the_command_line(tmp_path):
    run = lambda *args: subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)  # noqa: E731
    current = run().stdout.strip()
    assert run("next", "0.7.9").stdout.strip() == "0.8.0rc1"
    assert run("check-newer", "v0.0.1").returncode == 0
    refused = run("check-newer", f"v{current}")
    assert refused.returncode == 1 and "not newer" in refused.stderr
