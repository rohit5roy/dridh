"""Scenario D (review governance): an incomplete checklist blocks approval; ticking is a separate human act."""

from __future__ import annotations

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.documents import DocumentRepository
from dridh.state.reviews import ReviewRepository
from dridh.state.store import StateStore
from tests.e2e.planner_test_helpers import seed_approved_baseline, tick_review


@pytest.fixture()
def reviewed(tmp_path):
    info, _ = seed_approved_baseline(tmp_path / "project")
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        documents = DocumentRepository(connection, info.workspace_id)
        revision, meta = documents.current_revision("001-USE-CASES.md"), documents.document_metadata("001-USE-CASES.md")
        repository = ReviewRepository(connection, info.workspace_id)
        review_id = repository.create_review(document_id=str(meta["id"]), revision_id=revision.id, reviewer="r", actor="t")
        for item in ("Every flow is covered.", "No scope is invented."):
            repository.set_checklist(review_id, item, False, actor="t")
    return info, review_id, revision.id


def _decide(info, review_id, revision_id):
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        return ReviewRepository(connection, info.workspace_id).decide(
            review_id, decision="APPROVED", reviewer="r", reason="ok", actor="human:r", revision_id=revision_id)


def test_an_incomplete_checklist_blocks_approval_and_lists_what_is_open(reviewed):
    info, review_id, revision_id = reviewed
    with pytest.raises(DridhError) as caught:
        _decide(info, review_id, revision_id)
    assert caught.value.code == ErrorCode.APPROVAL_REQUIRED and "incomplete" in caught.value.message
    assert set(caught.value.details) == {"Every flow is covered.", "No scope is invented."}


def test_one_unchecked_item_is_still_enough_to_block(reviewed):
    info, review_id, revision_id = reviewed
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        ReviewRepository(connection, info.workspace_id).set_checklist(review_id, "Every flow is covered.", True, actor="human:r")
    with pytest.raises(DridhError) as caught:
        _decide(info, review_id, revision_id)
    assert caught.value.details == ("No scope is invented.",)


def test_a_fully_ticked_checklist_allows_the_human_approval(reviewed):
    info, review_id, revision_id = reviewed
    tick_review(info, review_id)
    assert _decide(info, review_id, revision_id)
