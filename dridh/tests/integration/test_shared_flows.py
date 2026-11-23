"""A flow several stories own is proven by any of them; an honest "not built here" from another is not a deviation.

Found by a real-model run: the plan gave every flow of a use case to a store, a service and a command-line story. The
store story correctly reported the flows NOT_DONE, each became a ledger draft, and the drafts hid the other stories'
passing proof: 0 of 29 flows counted, and delivery waited on 29 approvals of deviations that did not happen.
"""

from __future__ import annotations

import sqlite3
import uuid

from dridh.coordinator.coverage import coverage
from dridh.coordinator.delivery_check import delivery_readiness
from dridh.ledger.drafts import add_drafts, ledger_file
from tests.integration.test_flow_coverage import _agent, _drive, _set_status, env  # noqa: F401 - env is a fixture

SHARED = ("SF-01.1", "EF-01.1", "BE-01.1")


def _share(info) -> None:
    """ST-1.2 also owns the flows ST-1.1 owns, as a command-line story owns the flows its store story serves."""
    connection = sqlite3.connect(info.state_db)
    for flow in SHARED:
        connection.execute("INSERT INTO story_use_case(id,workspace_id,document_id,source_revision_id,story_id,use_case_id,flow_id) "
                           "VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), info.workspace_id, "d", "r", "ST-1.2", "UC-01", flow))
    connection.commit()
    connection.close()


def _run(info, feature, rest):
    reports = {"ST-1.1": [{"id": f, "status": "NOT_DONE", "note": "the store does not handle requests"} for f in SHARED],
               "ST-1.2": [{"id": f, "status": "IMPLEMENTED"} for f in (*SHARED, *rest)]}
    names = [f"test_{f.lower().replace('-', '_').replace('.', '_')}_x" for f in (*SHARED, *rest)]
    _drive(info, feature, _agent(reports, {"ST-1.1": ["test_store"], "ST-1.2": names}), ["ST-1.1", "ST-1.2"])
    _set_status(info, ST_1_1="COMPLETED", ST_1_2="COMPLETED")


def test_not_built_here_is_not_drafted_while_another_owner_can_still_build_it(env):  # noqa: F811
    info, feature, rest = env
    _share(info)
    _run(info, feature, rest)
    assert not ledger_file(info).is_file() or "BE-01.1" not in ledger_file(info).read_text()  # no draft for a shared flow
    readiness = delivery_readiness(info)
    assert readiness["ready"] is True, readiness["blockers"]
    assert all(outcome.startswith("proven") for outcome in readiness["flows"].values())
    assert coverage(info)["summary"].startswith("8 of 8 flows proven or explained")


def test_drafts_written_before_another_owner_proved_the_flow_are_superseded_not_awaiting(env):  # noqa: F811
    info, feature, rest = env
    _share(info)
    _run(info, feature, rest)
    # a workspace from before this rule: the store story's NOT_DONE drafts are already in the ledger
    add_drafts(info, [{"flow": f, "status": "NOT_DONE", "story": "ST-1.1", "actual": "Not built in this story.",
                       "reason": "the store does not handle requests"} for f in SHARED])
    readiness = delivery_readiness(info)
    assert readiness["ready"] is True, readiness["blockers"]
    assert readiness["awaiting_approval"] == [] and len(readiness["superseded"]) == 3
    rows = {row["flow"]: row for g in coverage(info)["use_cases"] for row in g["rows"]}
    assert rows["BE-01.1"]["state"] == "proven" and "not needed: proven here" in rows["BE-01.1"]["proof"]
    assert rows["BE-01.1"]["detail"]["owners"] == ["ST-1.1", "ST-1.2"]


def test_a_behaviour_change_still_needs_a_decision_even_when_another_owner_proves_the_flow(env):  # noqa: F811
    info, feature, rest = env
    _share(info)
    _run(info, feature, rest)
    add_drafts(info, [{"flow": "EF-01.1", "status": "DEVIATED", "story": "ST-1.1", "actual": "Declines are retried once.",
                       "reason": "Reported by the Executor; to be confirmed by a human."}])
    readiness = delivery_readiness(info)
    assert readiness["ready"] is False and readiness["awaiting_approval"] and readiness["superseded"] == []
