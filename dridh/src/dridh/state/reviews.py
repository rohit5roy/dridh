# ruff: noqa: E701,E702,E401,F401,I001,A
"""Review, revision-bound human approval and append-only audit persistence."""
from __future__ import annotations
import hashlib, sqlite3, uuid
from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import canonical_json, utc_now

REVIEW_STATEMENTS = (
"""CREATE TABLE IF NOT EXISTS review (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,document_id TEXT NOT NULL,revision_id TEXT NOT NULL,reviewer TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('OPEN','IN_REVIEW','APPROVED','REJECTED','CHANGES_REQUESTED')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(document_id,workspace_id) REFERENCES document(id,workspace_id) ON DELETE RESTRICT,FOREIGN KEY(revision_id,document_id) REFERENCES document_revision(id,document_id) ON DELETE RESTRICT)""",
"""CREATE TABLE IF NOT EXISTS review_checklist_item (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,review_id TEXT NOT NULL REFERENCES review(id) ON DELETE RESTRICT,item TEXT NOT NULL,checked INTEGER NOT NULL CHECK(checked IN(0,1)),checked_by TEXT,checked_at TEXT,UNIQUE(review_id,item))""",
"""CREATE TABLE IF NOT EXISTS review_comment (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,review_id TEXT NOT NULL REFERENCES review(id) ON DELETE RESTRICT,actor TEXT NOT NULL,comment TEXT NOT NULL CHECK(length(comment)>0),created_at TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS human_approval (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,review_id TEXT NOT NULL REFERENCES review(id) ON DELETE RESTRICT,revision_id TEXT NOT NULL REFERENCES document_revision(id) ON DELETE RESTRICT,decision TEXT NOT NULL CHECK(decision IN('APPROVED','REJECTED','CHANGES_REQUESTED')),reviewer TEXT NOT NULL,reason TEXT NOT NULL,auth_method TEXT NOT NULL CHECK(auth_method='terminal-confirmation'),created_at TEXT NOT NULL,UNIQUE(review_id,revision_id))""",
"""CREATE TABLE IF NOT EXISTS audit_event (seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,workspace_id TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,payload TEXT NOT NULL,occurred_at TEXT NOT NULL,prev_hash TEXT NOT NULL,hash TEXT NOT NULL)""",
"""CREATE TRIGGER IF NOT EXISTS review_workspace_guard BEFORE INSERT ON review WHEN NEW.workspace_id!=(SELECT value FROM meta WHERE key='workspace_id') BEGIN SELECT RAISE(ABORT,'review belongs to another workspace'); END""",
"""CREATE TRIGGER IF NOT EXISTS review_no_delete BEFORE DELETE ON review BEGIN SELECT RAISE(ABORT,'reviews are retained'); END""",
"""CREATE TRIGGER IF NOT EXISTS approval_no_update BEFORE UPDATE ON human_approval BEGIN SELECT RAISE(ABORT,'approvals are immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS approval_no_delete BEFORE DELETE ON human_approval BEGIN SELECT RAISE(ABORT,'approvals are immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_event BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END""",
"""CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_event BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END"""
)
def _audit(conn, workspace, actor, action, kind, entity, payload):
    body=canonical_json(payload);stamp=utc_now();row=conn.execute("SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1").fetchone();prev=row["hash"] if row else "0"*64
    digest=hashlib.sha256("\\n".join((prev,workspace,actor,action,kind,entity,body,stamp)).encode()).hexdigest()
    cur=conn.execute("INSERT INTO audit_event(event_id,workspace_id,actor,action,entity_type,entity_id,payload,occurred_at,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),workspace,actor,action,kind,entity,body,stamp,prev,digest));return int(cur.lastrowid)
class ReviewRepository:
    def __init__(self,connection:sqlite3.Connection,workspace_id:str): self.connection=connection;self.workspace_id=workspace_id
    def _tx(self):
        if not self.connection.in_transaction: raise DridhError(ErrorCode.INTERNAL_ERROR,"Review writes require a transaction.")
    def create_review(self,*,document_id,revision_id,reviewer,actor):
        self._tx()
        if not reviewer.strip(): raise DridhError(ErrorCode.VALIDATION_FAILED,"Reviewer is required.")
        rid=str(uuid.uuid4());now=utc_now();self.connection.execute("INSERT INTO review VALUES(?,?,?,?,?,'OPEN',?,?)",(rid,self.workspace_id,document_id,revision_id,reviewer.strip(),now,now));_audit(self.connection,self.workspace_id,actor,"REVIEW_CREATED","review",rid,{"revision_id":revision_id,"reviewer":reviewer});return rid
    def get_review(self,rid):
        row=self.connection.execute("SELECT * FROM review WHERE id=? AND workspace_id=?",(rid,self.workspace_id)).fetchone()
        if not row: raise DridhError(ErrorCode.VALIDATION_FAILED,f"Unknown review {rid}")
        out=dict(row);out["checklist"]=[dict(x) for x in self.connection.execute("SELECT item,checked,checked_by,checked_at FROM review_checklist_item WHERE review_id=? ORDER BY item",(rid,))];out["comments"]=[dict(x) for x in self.connection.execute("SELECT actor,comment,created_at FROM review_comment WHERE review_id=? ORDER BY created_at",(rid,))];out["approvals"]=[dict(x) for x in self.connection.execute("SELECT decision,reviewer,reason,revision_id,created_at FROM human_approval WHERE review_id=?",(rid,))];return out
    def set_checklist(self,rid,item,checked,*,actor):
        self._tx()
        if not item.strip(): raise DridhError(ErrorCode.VALIDATION_FAILED,"Checklist item cannot be empty.")
        self.connection.execute("INSERT INTO review_checklist_item VALUES(?,?,?,?,?,?,?) ON CONFLICT(review_id,item) DO UPDATE SET checked=excluded.checked,checked_by=excluded.checked_by,checked_at=excluded.checked_at",(str(uuid.uuid4()),self.workspace_id,rid,item.strip(),int(checked),actor,utc_now()));_audit(self.connection,self.workspace_id,actor,"CHECKLIST_CHANGED","review",rid,{"item":item,"checked":checked})
    def add_comment(self,rid,*,actor,comment):
        self._tx()
        if not comment.strip(): raise DridhError(ErrorCode.VALIDATION_FAILED,"Comment cannot be empty.")
        cid=str(uuid.uuid4());self.connection.execute("INSERT INTO review_comment VALUES(?,?,?,?,?,?)",(cid,self.workspace_id,rid,actor,comment.strip(),utc_now()));_audit(self.connection,self.workspace_id,actor,"REVIEW_COMMENT","review",rid,{"comment_id":cid});return cid
    def decide(self,rid,*,decision,reviewer,reason,actor,revision_id):
        self._tx()
        if decision not in ("APPROVED","REJECTED","CHANGES_REQUESTED"): raise DridhError(ErrorCode.VALIDATION_FAILED,"Invalid decision.")
        if not reviewer.strip(): raise DridhError(ErrorCode.VALIDATION_FAILED,"Reviewer is required.")
        row=self.connection.execute("SELECT revision_id,status,document_id FROM review WHERE id=? AND workspace_id=?",(rid,self.workspace_id)).fetchone()
        if not row: raise DridhError(ErrorCode.VALIDATION_FAILED,f"Unknown review {rid}")
        if row["revision_id"]!=revision_id: raise DridhError(ErrorCode.WORKFLOW_CONFLICT,"Revision mismatch; approval refused.")
        if row["status"] not in ("OPEN","IN_REVIEW"): raise DridhError(ErrorCode.INVALID_TRANSITION,"Review already decided.")
        if decision=="APPROVED":
            open_items=[r["item"] for r in self.connection.execute("SELECT item FROM review_checklist_item WHERE review_id=? AND checked=0 ORDER BY item",(rid,))]
            if open_items: raise DridhError(ErrorCode.APPROVAL_REQUIRED,"The review checklist is incomplete; approval refused.",details=tuple(open_items[:10]),recovery="A human ticks each item with `dridh reviews checklist <review_id> <item>` (or `--all`) before approving.")
        aid=str(uuid.uuid4());now=utc_now();self.connection.execute("INSERT INTO human_approval VALUES(?,?,?,?,?,?,?,?,?)",(aid,self.workspace_id,rid,revision_id,decision,reviewer.strip(),reason,"terminal-confirmation",now));self.connection.execute("UPDATE review SET status=?,updated_at=? WHERE id=?",(decision,now,rid));self.connection.execute("UPDATE document SET lifecycle_state=?,updated_at=? WHERE id=?",( "APPROVED" if decision=="APPROVED" else "REJECTED",now,row["document_id"]));_audit(self.connection,self.workspace_id,actor,"REVIEW_"+decision,"review",rid,{"approval_id":aid,"revision_id":revision_id,"reviewer":reviewer,"reason":reason});return aid
