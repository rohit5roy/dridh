from __future__ import annotations

from pathlib import Path

import pytest

from dridh.common.errors import DridhError
from dridh.state.store import StateStore
from dridh.state.traceability import TraceabilityRepository
from dridh.workspace.layout import init_workspace


def test_traceability_empty_projections_are_workspace_scoped(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialized, _ = init_workspace(project)
    with StateStore(initialized.state_db, initialized.workspace_id) as store:
        repository = TraceabilityRepository(store.connection, initialized.workspace_id)
        assert repository.use_case_story_map() == []
        assert repository.risks() == []
        assert repository.test_evidence() == []
        assert repository.test_evidence(story_id="ST-1.1") == []


def test_story_trace_rejects_unknown_story(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialized, _ = init_workspace(project)
    with StateStore(initialized.state_db, initialized.workspace_id) as store:
        repository = TraceabilityRepository(store.connection, initialized.workspace_id)
        with pytest.raises(DridhError, match="Unknown story ST-404"):
            repository.story_trace("ST-404")
