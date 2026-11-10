"""The UI view model flags an approved document whose file no longer matches its approved SHA-256."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from dridh.ui.state import _human_action, _mark_stale


def test_edited_approved_document_is_stale_and_becomes_the_next_step(tmp_path: Path) -> None:
    (tmp_path / "tdd.md").write_text("approved text\n", encoding="utf-8")
    approved = hashlib.sha256(b"approved text\n").hexdigest()
    stages = {"design": {"label": "Technical design", "document": "tdd.md", "sha256": approved,
                         "review": {"status": "APPROVED", "ticked": 10, "total": 10}}}
    info = SimpleNamespace(project_root=tmp_path)
    _mark_stale(info, stages)  # type: ignore[arg-type]
    assert stages["design"]["stale"] is False

    (tmp_path / "tdd.md").write_text("edited outside the Designer\n", encoding="utf-8")
    _mark_stale(info, stages)  # type: ignore[arg-type]
    assert stages["design"]["stale"] is True
    action = _human_action({"stage": "TDD_REVIEW"}, stages, [])
    assert action is not None and action["kind"] == "stale" and "Designer regenerate" in action["text"]


def test_documents_without_an_approved_hash_are_not_judged(tmp_path: Path) -> None:
    stages = {"plan": {"label": "Plan", "document": "plan.md", "sha256": None}}
    _mark_stale(SimpleNamespace(project_root=tmp_path), stages)  # type: ignore[arg-type]
    assert "stale" not in stages["plan"]
