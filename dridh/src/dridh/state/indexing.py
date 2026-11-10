from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from dridh.common.errors import DridhError, ErrorCode
from dridh.state.execution import CodeIndexRepository
from dridh.state.store import utc_now


class CodeIndexWriter(CodeIndexRepository):
    """Transactional writers for parser-produced file/entity/summary facts."""
# ruff: noqa: E701,E702,F401,I001 — compact SQL persistence declarations and focused integration fixtures
    def add_file(self, *, file_id: str, run_id: str, path: str, language: str,
                 content: bytes, source_sha: str) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_file(id,workspace_id,index_run_id,path,language,content_sha256,source_sha,size_bytes,indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (file_id,self.workspace_id,run_id,path,language,hashlib.sha256(content).hexdigest(),source_sha,len(content),utc_now()))

    def add_entity(self, *, entity_id: str, file_id: str, entity_type: str, name: str,
                    qualified_name: str, start_line: int, end_line: int, source_sha: str,
                    signature: str | None = None, parameters: list[dict] | None = None) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_entity(id,workspace_id,file_id,entity_type,name,qualified_name,signature,start_line,end_line,source_sha) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (entity_id,self.workspace_id,file_id,entity_type,name,qualified_name,signature,start_line,end_line,source_sha))
        for i,p in enumerate(parameters or [],1):
            self.connection.execute("INSERT INTO code_entity_parameter(id,entity_id,ordinal,name,type_name,default_value) VALUES(?,?,?,?,?,?)",
                (p.get("id") or f"{entity_id}:param:{i}",entity_id,i,p["name"],p.get("type"),p.get("default")))

    def add_relation(self, *, relation_id: str, source_entity_id: str, relation_type: str,
                     target_entity_id: str | None = None, target_name: str | None = None,
                     evidence_file_id: str | None = None, line: int | None = None, source_sha: str) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_relation(id,workspace_id,source_entity_id,target_entity_id,relation_type,target_name,evidence_file_id,line,source_sha) VALUES(?,?,?,?,?,?,?,?,?)",
            (relation_id,self.workspace_id,source_entity_id,target_entity_id,relation_type,target_name,evidence_file_id,line,source_sha))

    def add_endpoint(self, *, endpoint_id: str, entity_id: str | None, method: str, path: str,
                     framework: str, source_file_id: str, line: int, source_sha: str) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_api_endpoint(id,workspace_id,entity_id,method,path,framework,source_file_id,line,source_sha) VALUES(?,?,?,?,?,?,?,?,?)",
            (endpoint_id,self.workspace_id,entity_id,method,path,framework,source_file_id,line,source_sha))

    def add_summary(self, *, summary_id: str, entity_id: str, summary: str, model: str, source_sha: str) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_summary(id,workspace_id,entity_id,summary,model,source_sha,created_at) VALUES(?,?,?,?,?,?,?)",
            (summary_id,self.workspace_id,entity_id,summary,model,source_sha,utc_now()))
        entity=self.connection.execute("SELECT qualified_name FROM code_entity WHERE id=? AND workspace_id=?",(entity_id,self.workspace_id)).fetchone()
        if entity is None: raise DridhError(ErrorCode.VALIDATION_FAILED,f"Unknown code entity {entity_id}")
        self.connection.execute("INSERT INTO code_search_fts(entity_id,qualified_name,summary) VALUES(?,?,?)",(entity_id,entity["qualified_name"],summary))

    def add_embedding(self, *, embedding_id: str, entity_id: str, model: str, vector: list[float], source_sha: str) -> None:
        self._write()
        self.connection.execute("INSERT INTO code_embedding(id,workspace_id,entity_id,model,dimensions,vector_json,source_sha,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (embedding_id,self.workspace_id,entity_id,model,len(vector),json.dumps(vector,separators=(",",":")),source_sha,utc_now()))
