"""On the command line, stage sessions belong to a feature and approvals move it, as they do in OpenCode.

Found by a real-model run: after `dridh ideator|designer|planner approve`, `dridh feature status` still said
INITIALISED, because the CLI never bound the Ideator session to the feature; `feature bind` and `feature sync` had to
be run by hand for every stage.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from dridh.cli import main as cli
from dridh.coordinator.lifecycle import FeatureLifecycle
from dridh.ideator.service import IdeatorService
from tests.e2e.planner_test_helpers import DESIGNER_SESSION, IDEATOR_SESSION, seed_approved_baseline


def test_ideator_start_binds_the_session_to_the_only_open_feature(tmp_path, monkeypatch) -> None:
    info, _ = seed_approved_baseline(tmp_path / "project")
    lifecycle = FeatureLifecycle(info)
    lifecycle.create_feature("Library holds", actor="t")
    monkeypatch.setattr(IdeatorService, "start", lambda self, objective, existing_context="": {"session_id": "ideator-0123456789ab"})
    out = CliRunner().invoke(cli.app, ["ideator", "start", "Library holds", "--project", str(info.project_root)])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["feature"] == "library-holds"
    assert lifecycle.status("library-holds")["sessions"]["ideator_session_id"] == "ideator-0123456789ab"


def test_ideator_start_without_a_feature_says_the_lifecycle_cannot_advance(tmp_path, monkeypatch) -> None:
    info, _ = seed_approved_baseline(tmp_path / "project")
    monkeypatch.setattr(IdeatorService, "start", lambda self, objective, existing_context="": {"session_id": "ideator-0123456789ab"})
    out = CliRunner().invoke(cli.app, ["ideator", "start", "x", "--project", str(info.project_root)])
    assert out.exit_code == 0, out.output
    assert "cannot advance" in json.loads(out.output)["warning"]


def test_an_approval_moves_the_feature_it_belongs_to(tmp_path) -> None:
    info, _ = seed_approved_baseline(tmp_path / "project")  # approved Use Cases and technical design
    lifecycle = FeatureLifecycle(info)
    lifecycle.create_feature("Order intake", actor="t")
    lifecycle.bind_session("order-intake", "ideator_session_id", IDEATOR_SESSION, actor="t")
    result = cli._advance_after_approval(str(info.project_root), "designer_session_id", DESIGNER_SESSION, "r", {"approved": True})
    assert result["approved"] is True
    assert result["feature"]["slug"] == "order-intake" and result["feature"]["stage"] == "PLANNING"
    assert "TDD_REVIEW → TECHNICAL_DISCOVERY" not in result["feature"]["moves"] and result["feature"]["moves"][-1].endswith("PLANNING")
    assert lifecycle.status("order-intake")["feature"]["stage"] == "PLANNING"


def test_an_approval_of_an_unbound_session_says_so_and_keeps_the_approval(tmp_path) -> None:
    info, _ = seed_approved_baseline(tmp_path / "project")
    result = cli._advance_after_approval(str(info.project_root), "designer_session_id", DESIGNER_SESSION, "r", {"approved": True})
    assert result["approved"] is True and result["feature"] is None and "No feature is bound" in result["feature_note"]
