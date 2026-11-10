from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dridh.state.migration import migrate_v7_to_v8, schema_version
from dridh.state.reviews import ReviewRepository
from dridh.state.store import StateStore
from dridh.workspace.layout import init_workspace


def test_review_approval_audit_persistence(tmp_path: Path):
    p = tmp_path / "p"
    p.mkdir()
    i, _ = init_workspace(p)
    with StateStore(i.state_db, i.workspace_id) as s:
        with s.transaction() as c:
            c.execute(
                "INSERT INTO document(id,workspace_id,relative_path,document_type,standards_version,created_at,updated_at) VALUES('d',?,'a.md','tdd','2.1.0','n','n')",
                (i.workspace_id,),
            )
            c.execute(
                "INSERT INTO document_revision(id,document_id,revision_number,content_sha256,content,byte_size,line_count,standards_version,source_path,created_at) VALUES('r','d',1,?,'',0,0,'2.1.0','a.md','n')",
                ("a" * 64,),
            )
            x = ReviewRepository(c, i.workspace_id)
            rid = x.create_review(document_id="d", revision_id="r", reviewer="Reviewer", actor="cli:test")
            x.set_checklist(rid, "QA checked", True, actor="cli:test")
            x.add_comment(rid, actor="cli:test", comment="Looks good")
            aid = x.decide(rid, decision="APPROVED", reviewer="Human", reason="", actor="cli:test", revision_id="r")
        assert x.get_review(rid)["status"] == "APPROVED"
        assert len(x.get_review(rid)["approvals"]) == 1
        assert c.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM human_approval WHERE id=?", (aid,))


def test_v7_v8_migration(tmp_path: Path):
    p = tmp_path / "p"
    p.mkdir()
    i, _ = init_workspace(p)
    with StateStore(i.state_db, i.workspace_id):
        pass
    c = sqlite3.connect(i.state_db)
    c.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    c.commit()
    c.close()
    r = migrate_v7_to_v8(i.state_db)
    assert r["migrated"] and Path(r["backup_path"]).exists() and schema_version(i.state_db) == 8
    assert not migrate_v7_to_v8(i.state_db)["migrated"]
