"""OpenCode configuration preservation tests (hermetic, pinned CLI).

Each test runs the OpenCode CLI named by DRIDH_OPENCODE_BIN with a temporary HOME
and, where relevant, a temporary OPENCODE_CONFIG_DIR. Results therefore never depend
on a developer's global configuration, and nothing is written outside the temporary
directories. The only test that reads the real global configuration is the opt-in
``local_only`` check at the end of this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

MINIMUM_OPENCODE_VERSION = (2, 2, 5)  # Dridh supports this V2 release and later
OPENCODE = os.environ.get("DRIDH_OPENCODE_BIN", "")

pytestmark = [
    pytest.mark.opencode,
    pytest.mark.skipif(
        not OPENCODE or not Path(OPENCODE).is_file(),
        reason="Category A prerequisite missing: set DRIDH_OPENCODE_BIN to the pinned OpenCode CLI to run these tests",
    ),
]


def _hermetic_env(home: Path, config_dir: Path | None = None, content: str | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CI": "1", "NO_COLOR": "1"}
    if config_dir is not None:
        env["OPENCODE_CONFIG_DIR"] = str(config_dir)
    if content is not None:
        env["OPENCODE_CONFIG_CONTENT"] = content
    return env


def _run(
    args: list[str],
    *,
    home: Path,
    config_dir: Path | None = None,
    content: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [OPENCODE, "--standalone", *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(home),
        env=_hermetic_env(home, config_dir, content),
    )


def _json(result: subprocess.CompletedProcess) -> list:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pinned_cli_reports_expected_version(tmp_path: Path) -> None:
    result = subprocess.run(
        [OPENCODE, "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
        env=_hermetic_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    reported = result.stdout.strip()
    assert reported.startswith("opencode v2."), reported
    assert tuple(int(part) for part in reported.removeprefix("opencode v").split(".")[:3]) >= MINIMUM_OPENCODE_VERSION, reported


def test_global_configuration_is_read_without_modification(tmp_path: Path) -> None:
    home = tmp_path / "home"
    global_dir = home / ".config" / "opencode"
    global_dir.mkdir(parents=True)
    global_file = global_dir / "opencode.json"
    global_file.write_text('{"$schema":"https://opencode.ai/config.json"}\n')
    before = _sha256(global_file)

    sources = _json(_run(["debug", "config"], home=home))
    agents = _json(_run(["debug", "agents"], home=home))

    assert _sha256(global_file) == before
    assert not any("default_agent" in (source.get("info") or {}) for source in sources)
    agent_ids = [agent["id"] for agent in agents]
    assert "build" in agent_ids
    assert not [agent_id for agent_id in agent_ids if agent_id.startswith("dridh/")]


def test_custom_config_dir_discovers_only_its_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, agent in ((first, "coordinator"), (second, "other")):
        (root / "agents" / "dridh").mkdir(parents=True)
        (root / "opencode.json").write_text('{"$schema":"https://opencode.ai/config.json"}\n')
        (root / "agents" / "dridh" / f"{agent}.md").write_text(
            f"---\ndescription: test {agent}\nmode: primary\n---\nTest agent.\n"
        )

    one = [agent["id"] for agent in _json(_run(["debug", "agents"], home=home, config_dir=first))]
    two = [agent["id"] for agent in _json(_run(["debug", "agents"], home=home, config_dir=second))]

    assert "dridh/coordinator" in one
    assert "dridh/coordinator" not in two
    assert "dridh/other" in two


def test_inline_configuration_is_process_local(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_dir = tmp_path / "config"
    (config_dir / "agents" / "dridh").mkdir(parents=True)
    file = config_dir / "opencode.json"
    file.write_text('{"$schema":"https://opencode.ai/config.json"}\n')
    before = _sha256(file)
    marker = "INLINE_ONLY_DESCRIPTION"
    content = json.dumps({"agents": {"dridh/test-worker": {"description": marker}}})

    def has_marker(sources: list) -> bool:
        return any(
            (((source.get("info") or {}).get("agents") or {}).get("dridh/test-worker") or {}).get("description")
            == marker
            for source in sources
        )

    inline = _json(_run(["debug", "config"], home=home, config_dir=config_dir, content=content))
    baseline = _json(_run(["debug", "config"], home=home, config_dir=config_dir))

    assert has_marker(inline)
    assert not has_marker(baseline)
    assert _sha256(file) == before


@pytest.mark.local_only
@pytest.mark.skipif(
    os.environ.get("DRIDH_CHECK_REAL_OPENCODE_CONFIG") != "1",
    reason="Local-only (developer machine): set DRIDH_CHECK_REAL_OPENCODE_CONFIG=1 to inspect the real global configuration read-only",
)
def test_real_global_configuration_has_no_default_agent_override() -> None:
    env = {key: value for key, value in os.environ.items() if key != "OPENCODE_CONFIG_CONTENT"}
    env.update({"CI": "1", "NO_COLOR": "1"})
    sources = json.loads(
        subprocess.run(
            [OPENCODE, "--standalone", "debug", "config"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        ).stdout
    )
    agents = json.loads(
        subprocess.run(
            [OPENCODE, "--standalone", "debug", "agents"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        ).stdout
    )
    assert not any("default_agent" in (source.get("info") or {}) for source in sources)
    assert not [agent["id"] for agent in agents if agent["id"].startswith("dridh/")]
