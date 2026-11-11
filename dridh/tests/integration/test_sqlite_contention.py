"""SQLite lock contention is reported as a retryable, explained condition, never as a crash or as corruption."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import StateStore, busy_timeout_ms
from tests.e2e.planner_test_helpers import seed_approved_baseline


@pytest.fixture()
def info(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIDH_SQLITE_BUSY_TIMEOUT_MS", "300")
    workspace, _ = seed_approved_baseline(tmp_path / "project")
    return workspace


_HOLDERS: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_holders():
    yield
    while _HOLDERS:
        _HOLDERS.pop().close()


def hold_write_lock(info, exclusive=False):
    holder = sqlite3.connect(info.state_db, isolation_level=None)
    _HOLDERS.append(holder)
    if exclusive:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN IMMEDIATE")
    if exclusive:
        holder.execute("INSERT INTO meta(key,value) VALUES('lock-probe','1')")
    return holder


def test_the_wait_is_configurable_and_bounded(monkeypatch):
    for raw, expected in (("300", 300), ("5", 100), ("999999", 120_000), ("junk", 10_000)):
        monkeypatch.setenv("DRIDH_SQLITE_BUSY_TIMEOUT_MS", raw)
        assert busy_timeout_ms() == expected


def test_a_blocked_writer_gets_a_retryable_error_quickly_and_changes_nothing(info):
    holder = hold_write_lock(info)
    started = time.monotonic()
    with pytest.raises(DridhError) as caught, StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        connection.execute("INSERT INTO meta(key,value) VALUES('should-not-exist','1')")
    assert 0.2 < time.monotonic() - started < 5
    error = caught.value
    assert error.code == ErrorCode.DEPENDENCY_UNAVAILABLE and error.retryable is True and "busy" in error.message
    assert "Retry" in (error.recovery or "") and error.details == ("waited 300 ms",)
    holder.execute("ROLLBACK")
    with StateStore(info.state_db, info.workspace_id) as store:
        assert store.read().execute("SELECT COUNT(*) FROM meta WHERE key='should-not-exist'").fetchone()[0] == 0


def test_the_same_write_succeeds_once_the_other_writer_is_done(info):
    holder = hold_write_lock(info)
    holder.execute("ROLLBACK")
    with StateStore(info.state_db, info.workspace_id) as store, store.transaction() as connection:
        connection.execute("INSERT INTO meta(key,value) VALUES('after-release','1')")
    with StateStore(info.state_db, info.workspace_id) as store:
        assert store.read().execute("SELECT value FROM meta WHERE key='after-release'").fetchone()[0] == "1"


def test_readers_are_not_blocked_by_a_writer(info):
    holder = hold_write_lock(info)
    with StateStore(info.state_db, info.workspace_id) as store:  # WAL: reads proceed while a writer holds the lock
        assert store.read().execute("SELECT COUNT(*) FROM meta").fetchone()[0] >= 2
    holder.execute("ROLLBACK")


def test_a_lock_while_opening_is_not_reported_as_corruption(info):
    holder = hold_write_lock(info, exclusive=True)
    with pytest.raises(DridhError) as caught, StateStore(info.state_db, info.workspace_id):
        pass
    assert caught.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE and "corrupt" not in caught.value.message.lower()
    holder.execute("ROLLBACK")
    holder.close()


def test_the_cli_exits_with_the_prerequisite_code_and_a_retry_hint(info):
    holder = hold_write_lock(info, exclusive=True)
    done = subprocess.run([sys.executable, "-m", "dridh", "feature", "list", "--project", str(info.project_root)], capture_output=True, text=True,
                          env={**os.environ, "DRIDH_SQLITE_BUSY_TIMEOUT_MS": "300"}, stdin=subprocess.DEVNULL, check=False)
    holder.execute("ROLLBACK")
    assert done.returncode == 3, done.stderr
    error = json.loads(done.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE" and error["retryable"] is True and "Retry" in error["recovery"]
