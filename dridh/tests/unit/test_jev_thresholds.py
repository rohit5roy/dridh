"""Per-checkpoint Jev readiness thresholds."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dridh.config.dridh_yaml import CONFIG_FILENAME, DEFAULT_JEV_THRESHOLDS, JevConfig
from dridh.jev.checkpoints import STAGE_TASK_TYPES, checkpoint_threshold
from dridh.workspace.layout import init_workspace

# Readiness Jev gave documents that were later human-approved and whose code passed its tests (real runs, 2026-11-09).
APPROVED_READINESS = {
    "use_case_completeness": [0.59, 0.66, 0.67, 0.69, 0.71],
    "technical_design_completeness": [0.85, 0.86, 0.86],
    "planning_completeness": [0.87, 0.88, 0.89],
    "story_readiness": [0.74, 0.79, 0.81, 0.81, 0.82, 0.84, 0.86],
}


def test_every_stage_checkpoint_has_a_default_threshold() -> None:
    assert set(STAGE_TASK_TYPES.values()) == set(DEFAULT_JEV_THRESHOLDS)


def test_defaults_flag_only_outliers_of_the_observed_approved_documents() -> None:
    flagged = {task: [s for s in scores if s < DEFAULT_JEV_THRESHOLDS[task]] for task, scores in APPROVED_READINESS.items()}
    total = sum(len(v) for v in APPROVED_READINESS.values())
    assert sum(len(v) for v in flagged.values()) <= 2 < total  # the old single 0.90 flagged all of them
    assert all(s < 0.90 for v in APPROVED_READINESS.values() for s in v)


def test_a_project_can_override_one_checkpoint(tmp_path: Path) -> None:
    info, _ = init_workspace(tmp_path, mode="new")
    path = info.root / CONFIG_FILENAME
    document = yaml.safe_load(path.read_text())
    document["jev"] = {"thresholds": {"story_readiness": 0.9}}
    path.write_text(yaml.safe_dump(document))
    assert checkpoint_threshold(info, "story_readiness") == 0.9
    assert checkpoint_threshold(info, "use_case_completeness") == DEFAULT_JEV_THRESHOLDS["use_case_completeness"]


def test_a_missing_or_old_config_falls_back_to_the_defaults(tmp_path: Path) -> None:
    info, _ = init_workspace(tmp_path, mode="new")
    (info.root / CONFIG_FILENAME).unlink()
    assert checkpoint_threshold(info, "planning_completeness") == DEFAULT_JEV_THRESHOLDS["planning_completeness"]


@pytest.mark.parametrize("bad", [{"unknown_checkpoint": 0.5}, {"story_readiness": 1.5}])
def test_invalid_thresholds_are_refused(bad: dict) -> None:
    with pytest.raises(ValueError):
        JevConfig(thresholds=bad)
