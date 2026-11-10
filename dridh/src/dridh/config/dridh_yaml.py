"""Versioned dridh.yaml workspace configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_FILENAME = "dridh.yaml"
CONFIG_SCHEMA_VERSION = 1


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    root: str
    mode: str = "new"  # new | existing
    name: str = ""


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str = "deepseek-v4.1-flash-free"
    provider: str | None = None
    fallback_policy: str = "prompt-user"


class IndexingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    languages: list[str] = Field(default_factory=lambda: ["java", "typescript", "javascript", "python"])
    incremental: bool = True


class ContainersConfig(BaseModel):
    """Where story tests run (plan 15.6-15.8). ``auto``: in a container when Docker is available, else on the host;
    ``docker``: always in a container (refuse without Docker); ``off``: on the host."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "docker", "off"] = "auto"
    image: str | None = None  # default by project type (dridh.executor.containers.DEFAULT_IMAGES)
    setup: list[str] | None = None  # shell lines before the tests; default installs the project's test dependencies
    compose_file: str | None = None  # default: the first of compose.test.yml, docker-compose.test.yml, ...
    memory: str = Field(default="2g", pattern=r"^[0-9]+[kmg]$")
    cpus: float = Field(default=2.0, gt=0, le=64)
    docker_socket: bool = False  # mount the Docker socket so Testcontainers in the tests can start containers


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_parallel_stories: int = Field(default=2, ge=1, le=5)
    max_repair_attempts: int = Field(default=3, ge=0, le=3)
    pathfinder_checkpoint: bool = True
    checkpoint_percentages: list[int] = Field(default_factory=lambda: [25, 50, 100])
    require_testcontainers_when_supported: bool = True  # accepted for older dridh.yaml files; containers.mode decides
    test_command: list[str] | None = None
    test_timeout_seconds: float = Field(default=900.0, gt=0, le=7200)
    agent_timeout_seconds: float = Field(default=900.0, gt=0, le=7200)
    containers: ContainersConfig = Field(default_factory=ContainersConfig)

    @field_validator("test_command")
    @classmethod
    def _test_command(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            from dridh.executor.policy import validate_command

            validate_command(value)
        return value

    @field_validator("checkpoint_percentages")
    @classmethod
    def _percentages(cls, value: list[int]) -> list[int]:
        if not value or value != sorted(set(value)) or value[-1] != 100 or value[0] < 1:
            raise ValueError("checkpoint_percentages must be strictly increasing, start at 1 or more and end at 100")
        return value


class DeliveryConfig(BaseModel):
    """GitHub delivery (Phase 16). Off by default: pushing and opening PRs are outward-facing actions."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    remote: str = "origin"
    target_branch: str = "main"
    required_checks: list[str] = Field(default_factory=list)
    optional_checks: list[str] = Field(default_factory=list)
    poll_seconds: float = Field(default=30.0, gt=0, le=600)
    checks_timeout_seconds: float = Field(default=1800.0, gt=0, le=86400)
    max_ci_repairs: int = Field(default=2, ge=0, le=3)


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Deprecated and ignored: Jev thresholds are per checkpoint under ``jev.thresholds`` (D-P9-04). Kept so existing
    # dridh.yaml files still load.
    readiness_threshold: int = 90
    require_checklist_complete: bool = True
    require_explicit_human_approval: bool = True


# Per-checkpoint Jev readiness thresholds (decision D-P9-04). Below a threshold the checkpoint is flagged for the human
# reviewer; Jev stays advisory and never blocks. Defaults sit just under the lowest readiness Jev gave documents that
# were later approved in real runs (2026-11-09), so only outliers are flagged.
DEFAULT_JEV_THRESHOLDS = {
    "use_case_completeness": 0.60,
    "technical_design_completeness": 0.80,
    "planning_completeness": 0.80,
    "story_readiness": 0.75,
}


class JevConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thresholds: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_JEV_THRESHOLDS))

    @field_validator("thresholds")
    @classmethod
    def _probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = set(value) - set(DEFAULT_JEV_THRESHOLDS)
        if unknown:
            raise ValueError(f"unknown Jev checkpoint(s): {', '.join(sorted(unknown))}")
        if any(not 0.0 <= v <= 1.0 for v in value.values()):
            raise ValueError("Jev thresholds must be between 0 and 1")
        return {**DEFAULT_JEV_THRESHOLDS, **value}


class DridhConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = CONFIG_SCHEMA_VERSION
    workspace: WorkspaceConfig
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    jev: JevConfig = Field(default_factory=JevConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


def load_config(path: Path) -> DridhConfig:
    """Load and validate dridh.yaml from path."""
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("dridh.yaml must be a mapping")
    return DridhConfig.model_validate(raw)


def save_config(path: Path, config: DridhConfig) -> None:
    """Save dridh.yaml atomically."""
    import os
    import tempfile
    payload = config.model_dump(mode="json")
    content = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def create_default_config(project_root: Path, mode: str = "new", workspace_id: str = "") -> DridhConfig:
    """Create a default dridh.yaml for a project."""
    return DridhConfig(
        workspace=WorkspaceConfig(
            id=workspace_id,
            root=str(project_root),
            mode=mode,
            name=project_root.name,
        ),
    )
