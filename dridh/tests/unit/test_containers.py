"""Container plans for story tests: names, labels, setup, Compose detection, the script (no Docker needed)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dridh.config.dridh_yaml import ExecutionConfig
from dridh.executor import containers as c


def test_names_are_deterministic_sanitised_and_distinct_per_story():
    a = c.environment_names("ws-ABCDEF123456", "run-1234567890", "ST-001 / Login!")
    assert a == c.environment_names("ws-ABCDEF123456", "run-1234567890", "ST-001 / Login!")
    b = c.environment_names("ws-ABCDEF123456", "run-1234567890", "ST-002")
    assert a["id"] != b["id"] and a["network"] != b["network"] and a["project"] != b["project"]
    for value in a.values():
        assert value.startswith(("dridh-wsabcdef", "dridh_wsabcdef"))
        assert all(ch.isalnum() or ch in "-_" for ch in value) and value == value.lower()
    assert a["project"] == a["id"].replace("-", "_") and "-" not in a["project"]


def test_labels_identify_workspace_run_and_story():
    assert c.labels("w", "r", "s") == {"io.dridh": "1", "io.dridh.workspace": "w", "io.dridh.run": "r", "io.dridh.story": "s"}


@pytest.mark.parametrize(("command", "kind"), [
    (["python3", "-m", "unittest"], "python"), (["pytest", "-q"], "python"), (["uv", "run", "pytest"], "python"),
    (["npm", "test"], "node"), (["mvn", "-q", "test"], "maven"), (["./gradlew", "test"], "gradle"),
    (["go", "test", "./..."], "go"), (["make", "test"], None), ([], None)])
def test_project_kind(command, kind):
    assert c.project_kind(command) == kind


def test_default_setup_installs_the_project_test_dependencies(tmp_path):
    assert c.default_setup("python", tmp_path, ["python3", "-m", "unittest"]) == ["python3 -m venv /tmp/venv", ". /tmp/venv/bin/activate"]
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    lines = c.default_setup("python", tmp_path, ["pytest", "-q"])
    assert lines[2:] == ["pip install -q -r requirements.txt", "pip install -q -e .", "pip install -q pytest"]
    (tmp_path / "requirements-test.txt").write_text("pytest\n")
    assert "pip install -q -r requirements-test.txt" in c.default_setup("python", tmp_path, ["pytest"])
    assert c.default_setup("node", tmp_path, ["npm", "test"]) == ["npm install --no-audit --no-fund"]
    (tmp_path / "package-lock.json").write_text("{}")
    assert c.default_setup("node", tmp_path, ["npm", "test"]) == ["npm ci --no-audit --no-fund"]
    assert c.default_setup("go", tmp_path, ["go", "test"]) == []


def test_compose_file_detection_stays_inside_the_worktree(tmp_path):
    assert c.find_compose_file(tmp_path, None) is None
    (tmp_path / "docker-compose.test.yml").write_text("services: {}\n")
    assert c.find_compose_file(tmp_path, None) == (tmp_path / "docker-compose.test.yml").resolve()
    (tmp_path / "compose.test.yml").write_text("services: {}\n")
    assert c.find_compose_file(tmp_path, None).name == "compose.test.yml"
    assert c.find_compose_file(tmp_path, "missing.yml") is None
    outside = tmp_path.parent / "outside-compose.yml"
    outside.write_text("services: {}\n")
    assert c.find_compose_file(tmp_path, "../outside-compose.yml") is None


def test_script_runs_setup_then_exactly_the_validated_command():
    plan = c.ContainerPlan(image="python:3.13-slim", setup=["echo hi"], command=["python3", "-m", "unittest", "a b; rm -rf /"], compose_file=None)
    assert plan.script() == ("set -e\necho hi || { echo 'dridh: setup step failed: echo hi' >&2; exit 97; }\n"
                             "exec python3 -m unittest 'a b; rm -rf /'")
    quoted = c.ContainerPlan(image="i", setup=["echo 'x'"], command=["true"], compose_file=None).script()
    assert "echo 'dridh: setup step failed: echo '\"'\"'x'\"'\"''" in quoted


def test_container_settings_are_validated():
    config = ExecutionConfig()
    assert config.containers.mode == "auto" and config.containers.memory == "2g" and config.containers.docker_socket is False
    for bad in ({"mode": "always"}, {"memory": "lots"}, {"cpus": 0}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            ExecutionConfig(containers=bad)


def test_run_tag_is_part_of_every_name_so_stale_cleanup_can_narrow_to_one_run():
    names = c.environment_names("ws", "3F2A-91bc-0000", "ST-1")
    assert c.run_tag("3F2A-91bc-0000") == "3f2a91bc"
    assert "_3f2a91bc_" in names["project"] and "-3f2a91bc-" in names["network"]
