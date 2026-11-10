"""Phase 5 execution, indexing and evaluation persistence repositories.

All write operations require the caller's StateStore transaction. These repositories
persist verified facts; they do not execute Git, CI, Docker, Code Reacher or Jev.
"""
# ruff: noqa: E701,E702,F401,I001 — compact SQL persistence declarations and focused integration fixtures
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import utc_now


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    row = connection.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
    if row is None or row[0] != workspace_id:
        raise DridhError(
            ErrorCode.AUTHORIZATION_DENIED,
            "The repository workspace ID does not match the connected state database.",
        )


class ExecutionRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        _validate_workspace(connection, workspace_id)
        self.connection, self.workspace_id = connection, workspace_id

    def _write(self) -> None:
        if not self.connection.in_transaction:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "Execution writes require a StateStore transaction.")

    def record_branch(self, *, branch_id: str, name: str, story_id: str | None = None,
                      base_ref: str | None = None, head_sha: str | None = None, status: str = "ACTIVE") -> None:
        self._write()
        now = utc_now()
        self.connection.execute(
            """INSERT INTO git_branch(id,workspace_id,story_id,branch_name,base_ref,head_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET head_sha=excluded.head_sha,status=excluded.status,updated_at=excluded.updated_at""",
            (branch_id,self.workspace_id,story_id,name,base_ref,head_sha,status,now,now))

    def record_worktree(self, *, worktree_id: str, path: str, story_id: str | None = None,
                        branch_id: str | None = None, base_sha: str | None = None,
                        head_sha: str | None = None, status: str = "ACTIVE") -> None:
        self._write()
        now=utc_now()
        self.connection.execute(
            """INSERT INTO worktree(id,workspace_id,story_id,branch_id,path,base_sha,head_sha,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,head_sha=excluded.head_sha,status=excluded.status,updated_at=excluded.updated_at""",
            (worktree_id,self.workspace_id,story_id,branch_id,path,base_sha,head_sha,status,now,now))

    def record_pull_request(self, *, pr_id: str, title: str, head_ref: str, head_sha: str,
                            base_ref: str, story_id: str | None = None, branch_id: str | None = None,
                            number: int | None = None, url: str | None = None, base_sha: str | None = None,
                            state: str = "OPEN", draft: bool = False) -> None:
        self._write()
        now=utc_now()
        self.connection.execute(
            """INSERT INTO pull_request(id,workspace_id,story_id,branch_id,number,url,title,base_ref,head_ref,base_sha,head_sha,state,draft,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,base_sha=excluded.base_sha,head_sha=excluded.head_sha,state=excluded.state,draft=excluded.draft,updated_at=excluded.updated_at""",
            (pr_id,self.workspace_id,story_id,branch_id,number,url,title,base_ref,head_ref,base_sha,head_sha,state,int(draft),now,now))

    def record_check(self, *, check_id: str, pr_id: str, name: str, head_sha: str,
                     status: str, conclusion: str | None = None, run_id: str | None = None,
                     url: str | None = None, started_at: str | None = None,
                     completed_at: str | None = None) -> None:
        self._write()
        self.connection.execute(
            """INSERT INTO pull_request_check(id,pull_request_id,run_id,check_name,status,conclusion,url,head_sha,started_at,completed_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,conclusion=excluded.conclusion,head_sha=excluded.head_sha,completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
            (check_id,pr_id,run_id,name,status,conclusion,url,head_sha,started_at,completed_at,utc_now()))

    def record_gate(self, *, gate_id: str, story_id: str, gate: str, status: str,
                    evidence: dict[str, Any] | None = None, revision_sha: str | None = None) -> None:
        self._write()
        self.connection.execute(
            """INSERT INTO story_gate(id,workspace_id,story_id,gate,status,evidence_json,revision_sha,recorded_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id,story_id,gate) DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json,revision_sha=excluded.revision_sha,recorded_at=excluded.recorded_at""",
            (gate_id,self.workspace_id,story_id,gate,status,_json(evidence or {}),revision_sha,utc_now()))

    def record_integration(self, *, integration_id: str, branch_name: str, commit_sha: str,
                            feature_id: str | None = None, remote_ref: str | None = None,
                            source_pr_id: str | None = None, predecessor_story_ids: list[str] | None = None,
                            verification: dict[str, Any] | None = None, index_revision: str | None = None) -> None:
        self._write()
        self.connection.execute(
            """INSERT INTO integration_commit(id,workspace_id,feature_id,branch_name,commit_sha,remote_ref,source_pr_id,predecessor_story_ids,verification_json,index_revision,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id,commit_sha) DO UPDATE SET remote_ref=excluded.remote_ref,verification_json=excluded.verification_json,index_revision=excluded.index_revision""",
            (integration_id,self.workspace_id,feature_id,branch_name,commit_sha,remote_ref,source_pr_id,_json(predecessor_story_ids or []),_json(verification or {}),index_revision,utc_now()))

    def record_docker_environment(self, *, environment_id: str, compose_project: str, network_name: str,
                                  story_id: str | None = None, worktree_id: str | None = None,
                                  status: str = "CREATED", image_digest: str | None = None,
                                  evidence: dict[str, Any] | None = None) -> None:
        self._write()
        now=utc_now()
        self.connection.execute(
            """INSERT INTO docker_test_environment(id,workspace_id,story_id,worktree_id,compose_project,network_name,status,image_digest,evidence_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,image_digest=excluded.image_digest,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
            (environment_id,self.workspace_id,story_id,worktree_id,compose_project,network_name,status,image_digest,_json(evidence or {}),now,now))

    def story_delivery(self, story_id: str) -> dict[str, Any]:
        gates=self.connection.execute("SELECT gate,status,evidence_json,revision_sha,recorded_at FROM story_gate WHERE workspace_id=? AND story_id=? ORDER BY recorded_at",(self.workspace_id,story_id)).fetchall()
        prs=self.connection.execute("SELECT id,number,url,state,head_sha,base_ref,head_ref FROM pull_request WHERE workspace_id=? AND story_id=? ORDER BY updated_at",(self.workspace_id,story_id)).fetchall()
        return {"story_id":story_id,"gates":[dict(x) for x in gates],"pull_requests":[dict(x) for x in prs]}

    def list_worktrees(self, *, status: str | None = None) -> list[dict[str, Any]]:
        sql="SELECT * FROM worktree WHERE workspace_id=?"; args=[self.workspace_id]
        if status: sql+=" AND status=?"; args.append(status)
        return [dict(x) for x in self.connection.execute(sql+" ORDER BY created_at",args).fetchall()]


class CodeIndexRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        _validate_workspace(connection, workspace_id)
        self.connection,self.workspace_id=connection,workspace_id

    def _write(self) -> None:
        if not self.connection.in_transaction: raise DridhError(ErrorCode.INTERNAL_ERROR,"Code index writes require a StateStore transaction.")

    def start_run(self, *, run_id: str, repository_path: str, source_sha: str, worktree_id: str | None = None) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_index_run(id,workspace_id,repository_path,worktree_id,source_sha,status,started_at) VALUES(?,?,?,?,?,'RUNNING',?) ON CONFLICT(id) DO NOTHING",(run_id,self.workspace_id,repository_path,worktree_id,source_sha,utc_now()))

    def finish_run(self, run_id: str, *, status: str, files_seen: int, files_indexed: int, error: str | None = None) -> None:
        self._write()
        cur=self.connection.execute("UPDATE code_index_run SET status=?,files_seen=?,files_indexed=?,error=?,completed_at=? WHERE workspace_id=? AND id=? AND status IN ('QUEUED','RUNNING')",(status,files_seen,files_indexed,error,utc_now(),self.workspace_id,run_id))
        if cur.rowcount!=1: raise DridhError(ErrorCode.VALIDATION_FAILED,f"Index run {run_id} is missing or terminal.")

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows=self.connection.execute("""SELECT f.path,e.name,e.qualified_name,e.entity_type,e.start_line,e.end_line,e.source_sha
            FROM code_search_fts x JOIN code_entity e ON e.id=x.entity_id JOIN code_file f ON f.id=e.file_id
            WHERE code_search_fts MATCH ? AND e.workspace_id=? ORDER BY rank LIMIT ?""",(query,self.workspace_id,limit)).fetchall()
        return [dict(x) for x in rows]


class JevRepository:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        _validate_workspace(connection, workspace_id)
        self.connection,self.workspace_id=connection,workspace_id

    def _write(self) -> None:
        if not self.connection.in_transaction: raise DridhError(ErrorCode.INTERNAL_ERROR,"Jev writes require a StateStore transaction.")

    def record_evaluation(self, *, evaluation_id: str, subject_type: str, subject_id: str,
                           provider: str, model: str, input_sha: str, status: str,
                           decision: str | None = None, score: float | None = None,
                           rationale: str | None = None, output: dict[str, Any] | None = None,
                           started_at: str | None = None, completed_at: str | None = None) -> None:
        self._write()
        self.connection.execute("""INSERT INTO jev_evaluation(id,workspace_id,subject_type,subject_id,provider,model,status,decision,score,rationale,input_sha,output_json,started_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,decision=excluded.decision,score=excluded.score,rationale=excluded.rationale,output_json=excluded.output_json,completed_at=excluded.completed_at""",
            (evaluation_id,self.workspace_id,subject_type,subject_id,provider,model,status,decision,score,rationale,input_sha,_json(output or {}),started_at or utc_now(),completed_at))

    def record_criterion(self, *, criterion_id: str, evaluation_id: str, criterion: str,
                         verdict: str, score: float | None = None, rationale: str | None = None,
                         evidence: dict[str, Any] | None = None) -> None:
        self._write()
        self.connection.execute("INSERT INTO jev_evaluation_criterion(id,evaluation_id,criterion,verdict,score,rationale,evidence_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(evaluation_id,criterion) DO UPDATE SET verdict=excluded.verdict,score=excluded.score,rationale=excluded.rationale,evidence_json=excluded.evidence_json",(criterion_id,evaluation_id,criterion,verdict,score,rationale,_json(evidence or {})))

    def record_repair_feedback(self, *, evaluation_id: str, feedback: list[str]) -> None:
        """Persist the exact corrective instructions associated with an evaluation."""
        self._write()
        row = self.connection.execute(
            "SELECT output_json FROM jev_evaluation WHERE id=? AND workspace_id=?",
            (evaluation_id, self.workspace_id),
        ).fetchone()
        if row is None:
            raise DridhError(ErrorCode.VALIDATION_FAILED, "Jev evaluation does not exist in this workspace.")
        try:
            output = json.loads(row["output_json"])
        except (TypeError, ValueError):
            output = {}
        output["repair_feedback"] = [str(item)[:1000] for item in feedback[:20]]
        self.connection.execute(
            "UPDATE jev_evaluation SET output_json=? WHERE id=? AND workspace_id=?",
            (_json(output), evaluation_id, self.workspace_id),
        )

    def provider_health(self, *, health_id: str, provider: str, status: str, model: str | None = None,
                        latency_ms: int | None = None, error_code: str | None = None,
                        metadata: dict[str, Any] | None = None) -> None:
        self._write()
        self.connection.execute("INSERT INTO jev_provider_health(id,workspace_id,provider,model,status,latency_ms,error_code,checked_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",(health_id,self.workspace_id,provider,model,status,latency_ms,error_code,utc_now(),_json(metadata or {})))
