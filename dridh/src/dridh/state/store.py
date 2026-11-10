"""SQLite state store for one Dridh workspace.

The database is the authoritative machine-readable state. Every connection
enables foreign keys and WAL, waits for locks, and writes inside immediate
transactions so concurrent CLI and bridge processes cannot interleave a state
change. The schema version is recorded in ``meta`` and gates opening.

The ledger is append-only and hash-chained: each row stores the SHA-256 of its
previous row and its own canonical content. ``verify_ledger`` recomputes the
chain, so an edited or removed row is detected.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.common.redaction import redact

STATE_SCHEMA_VERSION = 15
GENESIS_HASH = "0" * 64

# Phase 5 increments 6-8: execution/Git, Code Reacher and Jev canonical records.
_EXECUTION_STATEMENTS: tuple[str, ...] = (
"""CREATE TABLE IF NOT EXISTS git_branch (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, story_id TEXT, branch_name TEXT NOT NULL, base_ref TEXT, head_sha TEXT, status TEXT NOT NULL CHECK(status IN ('ACTIVE','MERGED','CLOSED','DELETED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(workspace_id,branch_name))""",
"""CREATE TABLE IF NOT EXISTS worktree (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, story_id TEXT, branch_id TEXT REFERENCES git_branch(id), path TEXT NOT NULL, base_sha TEXT, head_sha TEXT, status TEXT NOT NULL CHECK(status IN ('CREATING','ACTIVE','DIRTY','INTEGRATED','CLEANED','FAILED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, cleaned_at TEXT, UNIQUE(workspace_id,path))""",
"""CREATE TABLE IF NOT EXISTS pull_request (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, story_id TEXT, branch_id TEXT REFERENCES git_branch(id), number INTEGER, url TEXT, title TEXT NOT NULL, base_ref TEXT NOT NULL, head_ref TEXT NOT NULL, base_sha TEXT, head_sha TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED','MERGED')), draft INTEGER NOT NULL DEFAULT 0 CHECK(draft IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, merged_at TEXT, UNIQUE(workspace_id,number))""",
"""CREATE TABLE IF NOT EXISTS pull_request_check (id TEXT PRIMARY KEY, pull_request_id TEXT NOT NULL REFERENCES pull_request(id) ON DELETE CASCADE, run_id TEXT, check_name TEXT NOT NULL, status TEXT NOT NULL, conclusion TEXT, url TEXT, head_sha TEXT NOT NULL, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, UNIQUE(pull_request_id,run_id,check_name))""",
"""CREATE TABLE IF NOT EXISTS story_gate (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, story_id TEXT NOT NULL, gate TEXT NOT NULL CHECK(gate IN ('NOT_READY','TESTS_PASSED','PR_CREATED','GHA_PASSED','LOCALLY_INTEGRATED','INTEGRATION_PUSHED','INDEXED','DEPENDENTS_UNLOCKED','REMOTE_MERGED')), status TEXT NOT NULL CHECK(status IN ('PENDING','PASS','FAIL','BLOCKED')), evidence_json TEXT NOT NULL DEFAULT '{}', revision_sha TEXT, recorded_at TEXT NOT NULL, UNIQUE(workspace_id,story_id,gate))""",
"""CREATE TABLE IF NOT EXISTS integration_commit (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, feature_id TEXT, branch_name TEXT NOT NULL, commit_sha TEXT NOT NULL, remote_ref TEXT, source_pr_id TEXT REFERENCES pull_request(id), predecessor_story_ids TEXT NOT NULL DEFAULT '[]', verification_json TEXT NOT NULL DEFAULT '{}', index_revision TEXT, created_at TEXT NOT NULL, UNIQUE(workspace_id,commit_sha))""",
"""CREATE TABLE IF NOT EXISTS docker_test_environment (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, story_id TEXT, worktree_id TEXT REFERENCES worktree(id), compose_project TEXT NOT NULL, network_name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('CREATED','RUNNING','PASSED','FAILED','STOPPED','CLEANED')), image_digest TEXT, evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(workspace_id,compose_project))""",
"CREATE INDEX IF NOT EXISTS worktree_story_status ON worktree(workspace_id,story_id,status)",
"CREATE INDEX IF NOT EXISTS pr_story_state ON pull_request(workspace_id,story_id,state)",
"CREATE INDEX IF NOT EXISTS pr_check_head ON pull_request_check(head_sha,status,conclusion)",
"""CREATE TABLE IF NOT EXISTS code_index_run (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, repository_path TEXT NOT NULL, worktree_id TEXT REFERENCES worktree(id), source_sha TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')), started_at TEXT NOT NULL, completed_at TEXT, files_seen INTEGER NOT NULL DEFAULT 0, files_indexed INTEGER NOT NULL DEFAULT 0, error TEXT, UNIQUE(workspace_id,repository_path,source_sha))""",
"""CREATE TABLE IF NOT EXISTS code_file (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, index_run_id TEXT NOT NULL REFERENCES code_index_run(id) ON DELETE CASCADE, path TEXT NOT NULL, language TEXT NOT NULL, content_sha256 TEXT NOT NULL, source_sha TEXT NOT NULL, size_bytes INTEGER NOT NULL, indexed_at TEXT NOT NULL, UNIQUE(index_run_id,path))""",
"""CREATE TABLE IF NOT EXISTS code_entity (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, file_id TEXT NOT NULL REFERENCES code_file(id) ON DELETE CASCADE, entity_type TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL, signature TEXT, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, source_sha TEXT NOT NULL, UNIQUE(file_id,qualified_name,start_line))""",
"""CREATE TABLE IF NOT EXISTS code_entity_parameter (id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, name TEXT NOT NULL, type_name TEXT, default_value TEXT, UNIQUE(entity_id,ordinal))""",
"""CREATE TABLE IF NOT EXISTS code_relation (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, source_entity_id TEXT NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE, target_entity_id TEXT REFERENCES code_entity(id) ON DELETE SET NULL, relation_type TEXT NOT NULL, target_name TEXT, evidence_file_id TEXT REFERENCES code_file(id) ON DELETE CASCADE, line INTEGER, source_sha TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS code_api_endpoint (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, entity_id TEXT REFERENCES code_entity(id) ON DELETE CASCADE, method TEXT NOT NULL, path TEXT NOT NULL, framework TEXT, source_file_id TEXT NOT NULL REFERENCES code_file(id) ON DELETE CASCADE, line INTEGER NOT NULL, source_sha TEXT NOT NULL, UNIQUE(source_file_id,method,path,line))""",
"""CREATE TABLE IF NOT EXISTS code_repository_mapping (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE, target_type TEXT NOT NULL CHECK(target_type IN ('USE_CASE','FLOW','COMPONENT','STORY')), target_id TEXT NOT NULL, mapping_type TEXT NOT NULL, evidence TEXT NOT NULL, source_sha TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(entity_id,target_type,target_id,mapping_type))""",
"""CREATE TABLE IF NOT EXISTS code_summary (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE, summary TEXT NOT NULL, model TEXT NOT NULL, source_sha TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(entity_id,model,source_sha))""",
"""CREATE VIRTUAL TABLE IF NOT EXISTS code_search_fts USING fts5(entity_id UNINDEXED, qualified_name, summary, tokenize='unicode61')""",
"""CREATE TABLE IF NOT EXISTS code_embedding (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE, model TEXT NOT NULL, dimensions INTEGER NOT NULL CHECK(dimensions>0), vector_json TEXT NOT NULL, source_sha TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(entity_id,model,source_sha))""",
"""CREATE TABLE IF NOT EXISTS jev_evaluation (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','FALLBACK')), decision TEXT, score REAL, rationale TEXT, input_sha TEXT NOT NULL, output_json TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, completed_at TEXT)""",
"""CREATE TABLE IF NOT EXISTS jev_evaluation_criterion (id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES jev_evaluation(id) ON DELETE CASCADE, criterion TEXT NOT NULL, verdict TEXT NOT NULL, score REAL, rationale TEXT, evidence_json TEXT NOT NULL DEFAULT '{}', UNIQUE(evaluation_id,criterion))""",
"""CREATE TABLE IF NOT EXISTS jev_provider_health (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, status TEXT NOT NULL CHECK(status IN ('HEALTHY','DEGRADED','UNAVAILABLE')), latency_ms INTEGER, error_code TEXT, checked_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')""",
"CREATE INDEX IF NOT EXISTS code_entity_name ON code_entity(workspace_id,qualified_name)",
"CREATE INDEX IF NOT EXISTS code_relation_source ON code_relation(source_entity_id,relation_type)",
"CREATE INDEX IF NOT EXISTS jev_subject_history ON jev_evaluation(workspace_id,subject_type,subject_id,started_at)"
)

# Document persistence (Phase 5 document domain). Each entry is one complete statement:
# trigger bodies contain semicolons, so these are executed individually, never split.
_DOCUMENT_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS document (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      relative_path TEXT NOT NULL,
      document_type TEXT NOT NULL,
      standards_version TEXT NOT NULL,
      lifecycle_state TEXT NOT NULL DEFAULT 'DRAFT' CHECK (lifecycle_state IN ('DRAFT', 'READY_FOR_REVIEW', 'IN_REVIEW', 'CHANGES_REQUESTED', 'APPROVED', 'REJECTED', 'STALE')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (workspace_id, relative_path)
    )""",
    """CREATE TABLE IF NOT EXISTS document_revision (
      id TEXT PRIMARY KEY,
      document_id TEXT NOT NULL REFERENCES document(id) ON DELETE RESTRICT,
      revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
      content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
      content TEXT NOT NULL,
      byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
      line_count INTEGER NOT NULL CHECK (line_count >= 0),
      standards_version TEXT NOT NULL,
      source_path TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE (document_id, revision_number)
    )""",
    """CREATE TABLE IF NOT EXISTS document_section (
      revision_id TEXT NOT NULL REFERENCES document_revision(id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
      level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 6),
      heading TEXT NOT NULL,
      start_line INTEGER NOT NULL CHECK (start_line >= 1),
      end_line INTEGER NOT NULL CHECK (end_line >= start_line),
      section_sha256 TEXT NOT NULL CHECK (length(section_sha256) = 64),
      PRIMARY KEY (revision_id, ordinal)
    )""",
    """CREATE TABLE IF NOT EXISTS document_validation (
      id TEXT PRIMARY KEY,
      revision_id TEXT NOT NULL REFERENCES document_revision(id) ON DELETE RESTRICT,
      validator TEXT NOT NULL,
      validator_version TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'WARNING')),
      error_count INTEGER NOT NULL CHECK (error_count >= 0),
      warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
      validated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS document_validation_by_revision ON document_validation(revision_id, validator, validated_at)",
    """CREATE TABLE IF NOT EXISTS document_validation_finding (
      validation_id TEXT NOT NULL REFERENCES document_validation(id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
      code TEXT NOT NULL,
      severity TEXT NOT NULL CHECK (severity IN ('ERROR', 'WARNING', 'INFO')),
      line INTEGER,
      entity_id TEXT,
      message TEXT NOT NULL,
      expected TEXT,
      actual TEXT,
      repair_hint TEXT,
      PRIMARY KEY (validation_id, ordinal)
    )""",
    """CREATE TRIGGER IF NOT EXISTS document_workspace_guard BEFORE INSERT ON document
      WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key = 'workspace_id')
      BEGIN SELECT RAISE(ABORT, 'document belongs to another workspace'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_no_delete BEFORE DELETE ON document
      BEGIN SELECT RAISE(ABORT, 'documents are retained and cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_revision_immutable_update BEFORE UPDATE ON document_revision
      BEGIN SELECT RAISE(ABORT, 'document revisions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_revision_immutable_delete BEFORE DELETE ON document_revision
      BEGIN SELECT RAISE(ABORT, 'document revisions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_section_immutable_update BEFORE UPDATE ON document_section
      BEGIN SELECT RAISE(ABORT, 'document sections are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_section_immutable_delete BEFORE DELETE ON document_section
      BEGIN SELECT RAISE(ABORT, 'document sections are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_validation_immutable_update BEFORE UPDATE ON document_validation
      BEGIN SELECT RAISE(ABORT, 'validation records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_validation_immutable_delete BEFORE DELETE ON document_validation
      BEGIN SELECT RAISE(ABORT, 'validation records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_finding_immutable_update BEFORE UPDATE ON document_validation_finding
      BEGIN SELECT RAISE(ABORT, 'validation findings are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_finding_immutable_delete BEFORE DELETE ON document_validation_finding
      BEGIN SELECT RAISE(ABORT, 'validation findings are immutable'); END""",
)

# Use-case domain (Phase 5 increment 1): actors, use cases and flows as a projection of an
# approved Use Case document revision. Rows carry the canonical IDs of 001-USE-CASES.md.
# Projection rows are replaced by import, never updated in place.
_USECASE_STATEMENTS: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS document_owner_key ON document(id, workspace_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS document_revision_owner_key ON document_revision(id, document_id)",
    """CREATE TABLE IF NOT EXISTS actor (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL REFERENCES document(id) ON DELETE RESTRICT,
      source_revision_id TEXT NOT NULL REFERENCES document_revision(id) ON DELETE RESTRICT,
      actor_id TEXT NOT NULL CHECK (actor_id GLOB 'ACT-[0-9][0-9]' AND length(actor_id) = 6),
      actor_name TEXT NOT NULL,
      actor_type TEXT NOT NULL CHECK (actor_type IN ('USER', 'SYSTEM', 'SERVICE')),
      actor_status TEXT NOT NULL CHECK (actor_status IN ('EXISTING', 'NEW')),
      description TEXT NOT NULL,
      position INTEGER NOT NULL CHECK (position >= 1),
      UNIQUE (workspace_id, actor_id),
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS use_case (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL REFERENCES document(id) ON DELETE RESTRICT,
      source_revision_id TEXT NOT NULL REFERENCES document_revision(id) ON DELETE RESTRICT,
      use_case_id TEXT NOT NULL CHECK (use_case_id GLOB 'UC-[0-9][0-9]' AND length(use_case_id) = 5),
      title TEXT NOT NULL,
      primary_actor_id TEXT NOT NULL,
      trigger_text TEXT NOT NULL,
      category TEXT NOT NULL,
      position INTEGER NOT NULL CHECK (position >= 1),
      UNIQUE (workspace_id, use_case_id),
      FOREIGN KEY (workspace_id, primary_actor_id) REFERENCES actor(workspace_id, actor_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS use_case_flow (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL REFERENCES document(id) ON DELETE RESTRICT,
      source_revision_id TEXT NOT NULL REFERENCES document_revision(id) ON DELETE RESTRICT,
      flow_id TEXT NOT NULL,
      parent_use_case_id TEXT NOT NULL,
      flow_type TEXT NOT NULL CHECK (flow_type IN ('SUCCESS', 'ALTERNATIVE', 'EXCEPTION', 'BOUNDARY', 'BUSINESS_RULE')),
      step_order INTEGER NOT NULL CHECK (step_order >= 1),
      origin_step_flow_id TEXT,
      subject TEXT NOT NULL,
      actor_id TEXT NOT NULL,
      action_text TEXT NOT NULL,
      result_text TEXT NOT NULL,
      priority TEXT NOT NULL,
      UNIQUE (workspace_id, flow_id),
      CHECK (
        (flow_type = 'SUCCESS' AND flow_id GLOB 'SF-[0-9][0-9].*' AND origin_step_flow_id IS NULL) OR
        (flow_type = 'ALTERNATIVE' AND flow_id GLOB 'AF-[0-9][0-9].*' AND origin_step_flow_id IS NOT NULL) OR
        (flow_type = 'EXCEPTION' AND flow_id GLOB 'EF-[0-9][0-9].*' AND origin_step_flow_id IS NOT NULL) OR
        (flow_type = 'BOUNDARY' AND flow_id GLOB 'BE-[0-9][0-9].*' AND origin_step_flow_id IS NOT NULL) OR
        (flow_type = 'BUSINESS_RULE' AND flow_id GLOB 'BR-[0-9][0-9].*' AND origin_step_flow_id IS NOT NULL)
      ),
      CHECK (
        length(flow_id) >= 7 AND substr(flow_id, 6, 1) = '.' AND substr(flow_id, 7) NOT GLOB '*[^0-9]*'
      ),
      FOREIGN KEY (workspace_id, parent_use_case_id) REFERENCES use_case(workspace_id, use_case_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, actor_id) REFERENCES actor(workspace_id, actor_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, origin_step_flow_id) REFERENCES use_case_flow(workspace_id, flow_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS use_case_import (
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      actor_count INTEGER NOT NULL CHECK (actor_count >= 0),
      use_case_count INTEGER NOT NULL CHECK (use_case_count >= 0),
      flow_count INTEGER NOT NULL CHECK (flow_count >= 0),
      imported_at TEXT NOT NULL,
      PRIMARY KEY (workspace_id, document_id),
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX IF NOT EXISTS use_case_flow_by_parent ON use_case_flow(workspace_id, parent_use_case_id, step_order)",
    """CREATE TRIGGER IF NOT EXISTS actor_workspace_guard BEFORE INSERT ON actor
      WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key = 'workspace_id')
        OR NEW.document_id NOT IN (SELECT id FROM document WHERE workspace_id = NEW.workspace_id)
      BEGIN SELECT RAISE(ABORT, 'actor must belong to this workspace and its document'); END""",
    """CREATE TRIGGER IF NOT EXISTS use_case_workspace_guard BEFORE INSERT ON use_case
      WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key = 'workspace_id')
        OR NEW.document_id NOT IN (SELECT id FROM document WHERE workspace_id = NEW.workspace_id)
      BEGIN SELECT RAISE(ABORT, 'use case must belong to this workspace and its document'); END""",
    """CREATE TRIGGER IF NOT EXISTS use_case_flow_workspace_guard BEFORE INSERT ON use_case_flow
      WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key = 'workspace_id')
        OR NEW.document_id NOT IN (SELECT id FROM document WHERE workspace_id = NEW.workspace_id)
      BEGIN SELECT RAISE(ABORT, 'flow must belong to this workspace and its document'); END""",
    """CREATE TRIGGER IF NOT EXISTS use_case_flow_parent_prefix BEFORE INSERT ON use_case_flow
      WHEN substr(NEW.flow_id, 4, 2) != substr(NEW.parent_use_case_id, 4, 2)
      BEGIN SELECT RAISE(ABORT, 'flow ID number must match its parent use case'); END""",
    """CREATE TRIGGER IF NOT EXISTS use_case_flow_origin_check BEFORE INSERT ON use_case_flow
      WHEN NEW.origin_step_flow_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM use_case_flow o
        WHERE o.workspace_id = NEW.workspace_id AND o.flow_id = NEW.origin_step_flow_id
          AND o.flow_type = 'SUCCESS' AND o.parent_use_case_id = NEW.parent_use_case_id)
      BEGIN SELECT RAISE(ABORT, 'origin step must be an SF row of the same use case'); END""",
    """CREATE TRIGGER IF NOT EXISTS actor_immutable_update BEFORE UPDATE ON actor
      BEGIN SELECT RAISE(ABORT, 'actor rows are replaced by import, not updated'); END""",
    """CREATE TRIGGER IF NOT EXISTS use_case_immutable_update BEFORE UPDATE ON use_case
      BEGIN SELECT RAISE(ABORT, 'use case rows are replaced by import, not updated'); END""",
    """CREATE TRIGGER IF NOT EXISTS use_case_flow_immutable_update BEFORE UPDATE ON use_case_flow
      BEGIN SELECT RAISE(ABORT, 'flow rows are replaced by import, not updated'); END""",
)

# Design domain (Phase 5 increment 2, scope rules corrected in schema v6).
#
# Uniqueness (TABLE-STANDARDS v2.1.0 sections 2 and 13.1):
#   - Feature DD-N is feature-wide: unique per workspace (one feature per workspace).
#   - Component CDD-N is per-component: unique per (workspace, component, decision_id). Two
#     components may each define CDD-1; one component may not define CDD-1 twice.
#
# Rows are projections of approved TDD and component-master revisions, replaced by import.
# Component references from CDD rows are canonical IDs without a foreign key, so a TDD
# re-import can replace its components while a component master still names them.

_COMPONENT_TABLE = """CREATE TABLE IF NOT EXISTS component (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  component_id TEXT NOT NULL CHECK (component_id GLOB 'CMP-[0-9][0-9]' AND length(component_id) = 6),
  component_name TEXT NOT NULL,
  definition_text TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 1),
  UNIQUE (workspace_id, component_id),
  FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
  FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
)"""

_DECISION_CHECK = """
  CHECK (
    (scope = 'FEATURE' AND substr(decision_id, 1, 3) = 'DD-' AND length(decision_id) >= 4
      AND substr(decision_id, 4) NOT GLOB '*[^0-9]*' AND component_id IS NULL)
    OR
    (scope = 'COMPONENT' AND substr(decision_id, 1, 4) = 'CDD-' AND length(decision_id) >= 5
      AND substr(decision_id, 5) NOT GLOB '*[^0-9]*' AND component_id IS NOT NULL)
  ),
  FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
  FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
)"""

_DECISION_TABLE_V5 = """CREATE TABLE IF NOT EXISTS design_decision (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('FEATURE', 'COMPONENT')),
  component_id TEXT,
  title TEXT NOT NULL,
  definition_text TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 1),
  UNIQUE (workspace_id, decision_id),""" + _DECISION_CHECK

_DECISION_TABLE_V6 = """CREATE TABLE IF NOT EXISTS design_decision (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('FEATURE', 'COMPONENT')),
  component_id TEXT,
  title TEXT NOT NULL,
  definition_text TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 1),
  feature_decision_key TEXT GENERATED ALWAYS AS (CASE WHEN scope = 'FEATURE' THEN decision_id END) STORED,
  UNIQUE (workspace_id, feature_decision_key),
  UNIQUE (workspace_id, component_id, decision_id),""" + _DECISION_CHECK

_DECISION_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS design_decision_workspace_guard BEFORE INSERT ON design_decision
      WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key = 'workspace_id')
      BEGIN SELECT RAISE(ABORT, 'design decision must belong to this workspace'); END""",
    """CREATE TRIGGER IF NOT EXISTS design_decision_immutable_update BEFORE UPDATE ON design_decision
      BEGIN SELECT RAISE(ABORT, 'design decision rows are replaced by import, not updated'); END""",
)

_MAPPING_TABLE_V5 = """CREATE TABLE IF NOT EXISTS use_case_component_mapping (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  use_case_id TEXT NOT NULL,
  flow_id TEXT,
  component_id TEXT NOT NULL,
  design_decision_id TEXT,
  position INTEGER NOT NULL CHECK (position >= 1),
  FOREIGN KEY (workspace_id, use_case_id) REFERENCES use_case(workspace_id, use_case_id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, flow_id) REFERENCES use_case_flow(workspace_id, flow_id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, component_id) REFERENCES component(workspace_id, component_id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, design_decision_id) REFERENCES design_decision(workspace_id, decision_id) ON DELETE RESTRICT,
  FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
  FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
)"""

_MAPPING_TABLE_V6 = _MAPPING_TABLE_V5.replace(
    "FOREIGN KEY (workspace_id, design_decision_id) REFERENCES design_decision(workspace_id, decision_id)",
    "FOREIGN KEY (workspace_id, design_decision_id) REFERENCES design_decision(workspace_id, feature_decision_key)",
)

_MAPPING_INDEX_AND_TRIGGERS: tuple[str, ...] = (
    """CREATE UNIQUE INDEX IF NOT EXISTS use_case_component_mapping_key ON use_case_component_mapping(
      workspace_id, document_id, use_case_id, IFNULL(flow_id, ''), component_id, IFNULL(design_decision_id, ''))""",
    """CREATE TRIGGER IF NOT EXISTS mapping_flow_belongs_to_use_case BEFORE INSERT ON use_case_component_mapping
      WHEN NEW.flow_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM use_case_flow f WHERE f.workspace_id = NEW.workspace_id AND f.flow_id = NEW.flow_id AND f.parent_use_case_id = NEW.use_case_id)
      BEGIN SELECT RAISE(ABORT, 'mapped flow must belong to the mapped use case'); END""",
    """CREATE TRIGGER IF NOT EXISTS mapping_decision_is_feature_scope BEFORE INSERT ON use_case_component_mapping
      WHEN NEW.design_decision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM design_decision d WHERE d.workspace_id = NEW.workspace_id AND d.decision_id = NEW.design_decision_id AND d.scope = 'FEATURE')
      BEGIN SELECT RAISE(ABORT, 'Table 13 may reference only feature-level DD-N decisions'); END""",
    """CREATE TRIGGER IF NOT EXISTS mapping_immutable_update BEFORE UPDATE ON use_case_component_mapping
      BEGIN SELECT RAISE(ABORT, 'mapping rows are replaced by import, not updated'); END""",
)

_API_EVENT_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS api_event_mapping (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      use_case_id TEXT NOT NULL,
      flow_id TEXT,
      component_id TEXT NOT NULL,
      reference_kind TEXT NOT NULL CHECK (reference_kind IN ('API', 'EVENT')),
      reference_name TEXT NOT NULL CHECK (length(reference_name) > 0),
      position INTEGER NOT NULL CHECK (position >= 1),
      FOREIGN KEY (workspace_id, use_case_id) REFERENCES use_case(workspace_id, use_case_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, flow_id) REFERENCES use_case_flow(workspace_id, flow_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, component_id) REFERENCES component(workspace_id, component_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS api_event_mapping_key ON api_event_mapping(
      workspace_id, document_id, use_case_id, IFNULL(flow_id, ''), component_id, reference_kind, reference_name)""",
    """CREATE TRIGGER IF NOT EXISTS api_event_flow_belongs_to_use_case BEFORE INSERT ON api_event_mapping
      WHEN NEW.flow_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM use_case_flow f WHERE f.workspace_id = NEW.workspace_id AND f.flow_id = NEW.flow_id AND f.parent_use_case_id = NEW.use_case_id)
      BEGIN SELECT RAISE(ABORT, 'referenced flow must belong to the use case'); END""",
    """CREATE TRIGGER IF NOT EXISTS api_event_immutable_update BEFORE UPDATE ON api_event_mapping
      BEGIN SELECT RAISE(ABORT, 'API/event rows are replaced by import, not updated'); END""",
)

_IMPORT_TABLE = """CREATE TABLE IF NOT EXISTS design_import (
  workspace_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  document_kind TEXT NOT NULL CHECK (document_kind IN ('TDD', 'COMPONENT_MASTER')),
  component_count INTEGER NOT NULL CHECK (component_count >= 0),
  decision_count INTEGER NOT NULL CHECK (decision_count >= 0),
  mapping_count INTEGER NOT NULL CHECK (mapping_count >= 0),
  api_event_count INTEGER NOT NULL CHECK (api_event_count >= 0),
  component_id TEXT,
  imported_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, document_id),
  FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
  FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
)"""

_COMPONENT_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS component_workspace_guard BEFORE INSERT ON component
      WHEN NEW.workspace_id != (SELECT value FROM meta WHERE key = 'workspace_id')
      BEGIN SELECT RAISE(ABORT, 'component must belong to this workspace'); END""",
    """CREATE TRIGGER IF NOT EXISTS component_immutable_update BEFORE UPDATE ON component
      BEGIN SELECT RAISE(ABORT, 'component rows are replaced by import, not updated'); END""",
)

# Fresh databases (schema v6).
_DESIGN_STATEMENTS: tuple[str, ...] = (
    _COMPONENT_TABLE,
    *_COMPONENT_TRIGGERS,
    _DECISION_TABLE_V6,
    *_DECISION_TRIGGERS,
    _MAPPING_TABLE_V6,
    *_MAPPING_INDEX_AND_TRIGGERS,
    *_API_EVENT_STATEMENTS,
    _IMPORT_TABLE,
)

# The schema as shipped in v5, kept so the v4 -> v5 step builds exactly what v5 contained.
_DESIGN_V5_STATEMENTS: tuple[str, ...] = tuple(
    {_DECISION_TABLE_V6: _DECISION_TABLE_V5, _MAPPING_TABLE_V6: _MAPPING_TABLE_V5}.get(statement, statement)
    for statement in _DESIGN_STATEMENTS
)

# Planning domain (Phase 5 increment 3): stories, dependency DAG, parallel groups, component
# phases, milestones, risks, test phases and evidence. Projections of approved central plan,
# phase-file and test-plan revisions are replaced by import. Evidence is append-only.
#
# Structural rules live in the database: self-dependency and cycles (recursive trigger), phase
# ordering (a prerequisite may not sit in a later phase), declared parallel pairs and group
# members must be mutually independent in the dependency graph, and every link is workspace-scoped.
_PLANNING_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS component_phase (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      component_id TEXT NOT NULL,
      phase_number INTEGER NOT NULL CHECK (phase_number >= 1),
      UNIQUE (workspace_id, component_id, phase_number),
      FOREIGN KEY (workspace_id, component_id) REFERENCES component(workspace_id, component_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS story (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      story_id TEXT NOT NULL CHECK (
        (story_id GLOB 'ST-[0-9]*' OR story_id GLOB 'PRE-[0-9]*' OR story_id GLOB 'X[0-9]*'
         OR story_id GLOB '[A-Z][A-Z]-[0-9]*' OR story_id GLOB '[A-Z][A-Z][A-Z]-[0-9]*')
        AND story_id NOT GLOB 'UC-*' AND story_id NOT GLOB 'ACT-*' AND story_id NOT GLOB 'CMP-*'
        AND story_id NOT GLOB 'LDG-*' AND story_id NOT GLOB 'DD-*' AND story_id NOT GLOB 'CDD-*'
        AND story_id NOT GLOB 'SF-*' AND story_id NOT GLOB 'AF-*' AND story_id NOT GLOB 'EF-*'
        AND story_id NOT GLOB 'BE-*' AND story_id NOT GLOB 'BR-*'),
      title TEXT NOT NULL,
      component_id TEXT NOT NULL,
      phase_number INTEGER NOT NULL CHECK (phase_number >= 1),
      status TEXT NOT NULL CHECK (status IN ('NOT_STARTED', 'IN_PROGRESS', 'IN_REVIEW', 'COMPLETED', 'BLOCKED')),
      position INTEGER NOT NULL CHECK (position >= 1),
      UNIQUE (workspace_id, story_id),
      FOREIGN KEY (workspace_id, component_id, phase_number) REFERENCES component_phase(workspace_id, component_id, phase_number) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS story_use_case (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      story_id TEXT NOT NULL,
      use_case_id TEXT NOT NULL,
      flow_id TEXT,
      FOREIGN KEY (workspace_id, story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, use_case_id) REFERENCES use_case(workspace_id, use_case_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, flow_id) REFERENCES use_case_flow(workspace_id, flow_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS story_use_case_key ON story_use_case(
      workspace_id, story_id, use_case_id, IFNULL(flow_id, ''))""",
    """CREATE TRIGGER IF NOT EXISTS story_use_case_flow_parent BEFORE INSERT ON story_use_case
      WHEN NEW.flow_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM use_case_flow f WHERE f.workspace_id = NEW.workspace_id AND f.flow_id = NEW.flow_id AND f.parent_use_case_id = NEW.use_case_id)
      BEGIN SELECT RAISE(ABORT, 'story flow must belong to the story use case'); END""",
    """CREATE TABLE IF NOT EXISTS story_dependency (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      story_id TEXT NOT NULL,
      depends_on_story_id TEXT NOT NULL,
      rationale TEXT,
      CHECK (story_id <> depends_on_story_id),
      UNIQUE (workspace_id, story_id, depends_on_story_id),
      FOREIGN KEY (workspace_id, story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, depends_on_story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER IF NOT EXISTS story_dependency_phase_order BEFORE INSERT ON story_dependency
      WHEN (SELECT phase_number FROM story WHERE workspace_id = NEW.workspace_id AND story_id = NEW.depends_on_story_id)
         > (SELECT phase_number FROM story WHERE workspace_id = NEW.workspace_id AND story_id = NEW.story_id)
      BEGIN SELECT RAISE(ABORT, 'a prerequisite story may not be in a later phase'); END""",
    """CREATE TRIGGER IF NOT EXISTS story_dependency_acyclic AFTER INSERT ON story_dependency
      WHEN EXISTS (
        WITH RECURSIVE reach(s) AS (
          SELECT NEW.depends_on_story_id
          UNION
          SELECT d.depends_on_story_id FROM story_dependency d JOIN reach ON d.story_id = reach.s AND d.workspace_id = NEW.workspace_id)
        SELECT 1 FROM reach WHERE s = NEW.story_id)
      BEGIN SELECT RAISE(ABORT, 'dependency would create a cycle'); END""",
    """CREATE TABLE IF NOT EXISTS story_parallel_pair (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      story_id TEXT NOT NULL,
      other_story_id TEXT NOT NULL,
      CHECK (story_id < other_story_id),
      UNIQUE (workspace_id, story_id, other_story_id),
      FOREIGN KEY (workspace_id, story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, other_story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT,
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER IF NOT EXISTS story_parallel_pair_independent BEFORE INSERT ON story_parallel_pair
      WHEN EXISTS (
        WITH RECURSIVE r(x) AS (SELECT NEW.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id)
        SELECT 1 FROM r WHERE x = NEW.other_story_id)
        OR EXISTS (
        WITH RECURSIVE r(x) AS (SELECT NEW.other_story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id)
        SELECT 1 FROM r WHERE x = NEW.story_id)
      BEGIN SELECT RAISE(ABORT, 'parallel stories must be independent in the dependency graph'); END""",
    """CREATE TABLE IF NOT EXISTS story_parallel_group (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      group_name TEXT NOT NULL CHECK (length(group_name) > 0),
      created_at TEXT NOT NULL,
      UNIQUE (workspace_id, id),
      UNIQUE (workspace_id, group_name)
    )""",
    """CREATE TABLE IF NOT EXISTS story_parallel_member (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      group_id TEXT NOT NULL,
      story_id TEXT NOT NULL,
      UNIQUE (group_id, story_id),
      FOREIGN KEY (workspace_id, group_id) REFERENCES story_parallel_group(workspace_id, id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER IF NOT EXISTS story_parallel_member_independent BEFORE INSERT ON story_parallel_member
      WHEN EXISTS (
        SELECT 1 FROM story_parallel_member m
        WHERE m.group_id = NEW.group_id AND m.story_id <> NEW.story_id AND (
          EXISTS (WITH RECURSIVE r(x) AS (SELECT NEW.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id)
                  SELECT 1 FROM r WHERE x = m.story_id)
          OR EXISTS (WITH RECURSIVE r(x) AS (SELECT m.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id)
                  SELECT 1 FROM r WHERE x = NEW.story_id)))
      BEGIN SELECT RAISE(ABORT, 'a parallel group may contain only mutually independent stories'); END""",
    """CREATE TRIGGER IF NOT EXISTS story_dependency_checks_parallel_pair BEFORE INSERT ON story_dependency
      WHEN EXISTS (
        SELECT 1 FROM story_parallel_pair p
        WHERE p.workspace_id = NEW.workspace_id AND (
          (EXISTS (WITH RECURSIVE r(x) AS (SELECT p.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id) SELECT 1 FROM r WHERE x = NEW.depends_on_story_id)
           AND EXISTS (WITH RECURSIVE r(x) AS (SELECT NEW.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id) SELECT 1 FROM r WHERE x = p.other_story_id))
          OR
          (EXISTS (WITH RECURSIVE r(x) AS (SELECT p.other_story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id) SELECT 1 FROM r WHERE x = NEW.depends_on_story_id)
           AND EXISTS (WITH RECURSIVE r(x) AS (SELECT NEW.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id) SELECT 1 FROM r WHERE x = p.story_id))
        )
      )
      BEGIN SELECT RAISE(ABORT, 'dependency would invalidate a parallel pair'); END""",
    """CREATE TRIGGER IF NOT EXISTS story_dependency_checks_parallel_group BEFORE INSERT ON story_dependency
      WHEN EXISTS (
        SELECT 1 FROM story_parallel_member m1
        JOIN story_parallel_member m2 ON m1.group_id = m2.group_id AND m1.story_id <> m2.story_id
        WHERE m1.workspace_id = NEW.workspace_id
        AND EXISTS (WITH RECURSIVE r(x) AS (SELECT m1.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id) SELECT 1 FROM r WHERE x = NEW.depends_on_story_id)
        AND EXISTS (WITH RECURSIVE r(x) AS (SELECT NEW.story_id UNION SELECT d.depends_on_story_id FROM story_dependency d JOIN r ON d.story_id = r.x AND d.workspace_id = NEW.workspace_id) SELECT 1 FROM r WHERE x = m2.story_id)
      )
      BEGIN SELECT RAISE(ABORT, 'dependency would invalidate a parallel group'); END""",
    """CREATE TABLE IF NOT EXISTS milestone (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      milestone_id TEXT NOT NULL CHECK (milestone_id GLOB 'M[0-9]*' AND length(milestone_id) >= 2),
      title TEXT NOT NULL,
      criteria TEXT NOT NULL CHECK (length(criteria) > 0),
      target_date TEXT CHECK (target_date IS NULL OR target_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
      status TEXT NOT NULL CHECK (status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED')),
      position INTEGER NOT NULL CHECK (position >= 1),
      UNIQUE (workspace_id, milestone_id),
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS milestone_story (
      workspace_id TEXT NOT NULL,
      milestone_id TEXT NOT NULL,
      story_id TEXT NOT NULL,
      PRIMARY KEY (workspace_id, milestone_id, story_id),
      FOREIGN KEY (workspace_id, milestone_id) REFERENCES milestone(workspace_id, milestone_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS risk (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      risk_id TEXT NOT NULL CHECK (risk_id GLOB 'R[0-9]*' AND length(risk_id) >= 2),
      title TEXT NOT NULL,
      likelihood TEXT NOT NULL CHECK (likelihood IN ('Low', 'Medium', 'High')),
      impact TEXT NOT NULL CHECK (impact IN ('Low', 'Medium', 'High')),
      mitigation TEXT NOT NULL CHECK (length(mitigation) > 0),
      owner TEXT NOT NULL,
      position INTEGER NOT NULL CHECK (position >= 1),
      UNIQUE (workspace_id, risk_id),
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS test_phase (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      test_phase_id TEXT NOT NULL CHECK (test_phase_id GLOB '[SDX][0-9]*' AND length(test_phase_id) >= 2),
      test_scope TEXT NOT NULL,
      dependency_mode TEXT NOT NULL CHECK (dependency_mode IN ('TESTCONTAINERS', 'DOCKER_COMPOSE', 'REAL_QA_ENDPOINT', 'WIREMOCK_EXCEPTION')),
      environment TEXT NOT NULL,
      execution_command TEXT NOT NULL,
      evidence_expectation TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('NOT_STARTED', 'IN_PROGRESS', 'PASSED', 'FAILED', 'BLOCKED', 'NOT_APPLICABLE')),
      position INTEGER NOT NULL CHECK (position >= 1),
      UNIQUE (workspace_id, test_phase_id),
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS test_phase_use_case (
      workspace_id TEXT NOT NULL,
      test_phase_id TEXT NOT NULL,
      use_case_id TEXT NOT NULL,
      PRIMARY KEY (workspace_id, test_phase_id, use_case_id),
      FOREIGN KEY (workspace_id, test_phase_id) REFERENCES test_phase(workspace_id, test_phase_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, use_case_id) REFERENCES use_case(workspace_id, use_case_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS test_phase_flow (
      workspace_id TEXT NOT NULL,
      test_phase_id TEXT NOT NULL,
      flow_id TEXT NOT NULL,
      PRIMARY KEY (workspace_id, test_phase_id, flow_id),
      FOREIGN KEY (workspace_id, test_phase_id) REFERENCES test_phase(workspace_id, test_phase_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, flow_id) REFERENCES use_case_flow(workspace_id, flow_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS test_phase_component (
      workspace_id TEXT NOT NULL,
      test_phase_id TEXT NOT NULL,
      component_id TEXT NOT NULL,
      PRIMARY KEY (workspace_id, test_phase_id, component_id),
      FOREIGN KEY (workspace_id, test_phase_id) REFERENCES test_phase(workspace_id, test_phase_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, component_id) REFERENCES component(workspace_id, component_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS story_test_evidence (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      story_id TEXT NOT NULL,
      test_phase_id TEXT NOT NULL,
      evidence_status TEXT NOT NULL CHECK (evidence_status IN ('NOT_STARTED', 'IN_PROGRESS', 'PASSED', 'FAILED', 'BLOCKED', 'NOT_APPLICABLE')),
      evidence_ref TEXT NOT NULL CHECK (length(evidence_ref) > 0),
      recorded_by TEXT NOT NULL CHECK (length(recorded_by) > 0),
      recorded_at TEXT NOT NULL,
      FOREIGN KEY (workspace_id, story_id) REFERENCES story(workspace_id, story_id) ON DELETE RESTRICT,
      FOREIGN KEY (workspace_id, test_phase_id) REFERENCES test_phase(workspace_id, test_phase_id) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER IF NOT EXISTS story_test_evidence_immutable_update BEFORE UPDATE ON story_test_evidence
      BEGIN SELECT RAISE(ABORT, 'test evidence is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS story_test_evidence_immutable_delete BEFORE DELETE ON story_test_evidence
      BEGIN SELECT RAISE(ABORT, 'test evidence is append-only'); END""",
    """CREATE TABLE IF NOT EXISTS planning_import (
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      source_revision_id TEXT NOT NULL,
      document_kind TEXT NOT NULL CHECK (document_kind IN ('CENTRAL_PLAN', 'PHASE_FILE', 'TEST_PLAN')),
      imported_at TEXT NOT NULL,
      PRIMARY KEY (workspace_id, document_id),
      FOREIGN KEY (document_id, workspace_id) REFERENCES document(id, workspace_id) ON DELETE RESTRICT,
      FOREIGN KEY (source_revision_id, document_id) REFERENCES document_revision(id, document_id) ON DELETE RESTRICT
    )""",    *(
        f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_update BEFORE UPDATE ON {table}
          BEGIN SELECT RAISE(ABORT, '{table} rows are replaced by import, not updated'); END"""
        for table in (
            "component_phase", "story", "story_use_case", "story_dependency", "story_parallel_pair",
            "milestone", "risk", "test_phase", "test_phase_use_case", "test_phase_flow", "test_phase_component",
        )
    ),
)

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE workflows (
  id TEXT PRIMARY KEY,
  objective TEXT NOT NULL,
  state TEXT NOT NULL,
  plan_sha256 TEXT,
  plan_revision INTEGER NOT NULL DEFAULT 0,
  plan_acceptance TEXT NOT NULL DEFAULT '[]',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  failure_code TEXT,
  failure_message TEXT,
  report_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE stages (
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY (workflow_id, id)
);
CREATE TABLE tasks (
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  id TEXT NOT NULL,
  stage_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  agent TEXT NOT NULL,
  instructions TEXT NOT NULL,
  allowed_paths TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  acceptance TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  lease_token TEXT,
  lease_expires_at TEXT,
  result TEXT,
  failure_code TEXT,
  failure_message TEXT,
  parent_task_id TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, id)
);
CREATE INDEX tasks_by_order ON tasks(workflow_id, ordinal);
CREATE TABLE approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  decision TEXT NOT NULL,
  actor TEXT NOT NULL,
  method TEXT NOT NULL,
  plan_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE worker_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT,
  purpose TEXT NOT NULL,
  agent TEXT NOT NULL,
  session_id TEXT,
  status TEXT NOT NULL,
  exit_code INTEGER,
  duration_seconds REAL,
  output_sha256 TEXT,
  error_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE acceptance_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT,
  check_index INTEGER NOT NULL,
  check_type TEXT NOT NULL,
  passed INTEGER NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  workflow_id TEXT,
  purpose TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);
CREATE TABLE ledger (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (workflow_id, task_id, depends_on)
);
CREATE TABLE IF NOT EXISTS subtasks (
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  parent_task_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  PRIMARY KEY (workflow_id, parent_task_id, task_id)
);
CREATE TABLE IF NOT EXISTS worker_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  session_id TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  exit_code INTEGER,
  error_code TEXT,
  lease_token TEXT,
  worktree_id TEXT
);
CREATE TABLE IF NOT EXISTS worktrees (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  path TEXT NOT NULL,
  branch TEXT,
  commit_sha TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  cleaned_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduling_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  decision_type TEXT NOT NULL,
  task_id TEXT,
  worker_id TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT,
  checkpoint_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  verdict TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clarifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  answer TEXT,
  source TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL,
  answered_at TEXT
);
CREATE TABLE IF NOT EXISTS integration_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  branch TEXT NOT NULL,
  status TEXT NOT NULL,
  commit_sha TEXT,
  conflict_detail TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


DEFAULT_BUSY_TIMEOUT_MS = 10_000


def busy_timeout_ms() -> int:
    """How long to wait for another writer, in milliseconds (``DRIDH_SQLITE_BUSY_TIMEOUT_MS``, 100 to 120000)."""
    try:
        return min(120_000, max(100, int(os.environ.get("DRIDH_SQLITE_BUSY_TIMEOUT_MS", DEFAULT_BUSY_TIMEOUT_MS))))
    except ValueError:
        return DEFAULT_BUSY_TIMEOUT_MS


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def busy_error(waited_ms: int) -> DridhError:
    return DridhError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "The workspace database is busy: another Dridh process is writing to it.",
        details=(f"waited {waited_ms} ms",),
        recovery="Nothing was changed. Retry in a few seconds; if it persists, check for a stuck `dridh` process.",
        retryable=True,
    )


class StateStore:
    """One connection to one workspace database. Use as a context manager."""

    def __init__(self, path: Path, workspace_id: str) -> None:
        self.path = path
        self.workspace_id = workspace_id
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> StateStore:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        wait_ms = busy_timeout_ms()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=wait_ms / 1000, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {wait_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.OperationalError as exc:
            if connection is not None:  # a file that opens but is not a usable database must not leak its handle
                connection.close()
            if _is_busy(exc):  # contention is not corruption: say so and let the caller retry
                raise busy_error(wait_ms) from None
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "The workflow state database could not be opened.",
                details=(str(self.path),),
                recovery="Keep the file for diagnosis. Restore it from a backup or reinitialise the workspace.",
            ) from None
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.close()
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "The workflow state database could not be opened.",
                details=(str(self.path),),
                recovery="Keep the file for diagnosis. Restore it from a backup or reinitialise the workspace.",
            ) from None
        self.connection = connection
        try:
            if is_new:
                self._create(connection)
            self._check(connection)
        except DridhError:
            connection.close()
            self.connection = None
            raise

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _create(self, connection: sqlite3.Connection) -> None:
        # executescript() commits implicitly, so the statements run one by one inside
        # a single immediate transaction to keep schema creation atomic.
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in (part.strip() for part in _SCHEMA.split(";")):
                if statement:
                    connection.execute(statement)
            for statement in _DOCUMENT_STATEMENTS:
                connection.execute(statement)
            for statement in _USECASE_STATEMENTS:
                connection.execute(statement)
            for statement in _DESIGN_STATEMENTS:
                connection.execute(statement)
            for statement in _PLANNING_STATEMENTS:
                connection.execute(statement)
            from dridh.state.reviews import REVIEW_STATEMENTS
            for statement in REVIEW_STATEMENTS:
                connection.execute(statement)
            for statement in _EXECUTION_STATEMENTS:
                connection.execute(statement)
            from dridh.state.lifecycle import LIFECYCLE_STATEMENTS
            for statement in LIFECYCLE_STATEMENTS:
                connection.execute(statement)
            from dridh.state.invocations import INVOCATION_STATEMENTS
            from dridh.state.ledger_entries import LEDGER_ENTRY_STATEMENTS
            from dridh.state.runs import RUN_STATEMENTS, STORY_STATUS_STATEMENTS
            for statement in (*RUN_STATEMENTS, *STORY_STATUS_STATEMENTS, *INVOCATION_STATEMENTS, *LEDGER_ENTRY_STATEMENTS):
                connection.execute(statement)
            connection.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(STATE_SCHEMA_VERSION),))
            connection.execute("INSERT INTO meta(key, value) VALUES ('workspace_id', ?)", (self.workspace_id,))
            connection.execute("COMMIT")
        except sqlite3.DatabaseError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise DridhError(ErrorCode.STATE_CORRUPT, "The workflow state database could not be created.") from None

    def _check(self, connection: sqlite3.Connection) -> None:
        try:
            verdict = connection.execute("PRAGMA quick_check").fetchone()[0]
            meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
        except sqlite3.DatabaseError:
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "The workflow state database failed its integrity check.",
                details=(str(self.path),),
                recovery="Keep the file for diagnosis. Restore it from a backup or reinitialise the workspace.",
            ) from None
        if verdict != "ok":
            raise DridhError(ErrorCode.STATE_CORRUPT, "The workflow state database failed its integrity check.", details=(str(verdict),))
        version = meta.get("schema_version")
        if version in ("2", "3", "4", "5", "6", "7", "8", "9", "10"):
            raise DridhError(
                ErrorCode.SCHEMA_UNSUPPORTED,
                f"The workflow state schema is version {version}. This runtime needs version {STATE_SCHEMA_VERSION}.",
                details=(f"found {version}, supported {STATE_SCHEMA_VERSION}",),
                recovery="Run `dridh migrate --project <path>` to upgrade the state with a backup first.",
            )
        if version not in ("1", str(STATE_SCHEMA_VERSION)):
            raise DridhError(
                ErrorCode.SCHEMA_UNSUPPORTED,
                "The workflow state schema version is not supported by this Dridh runtime.",
                details=(f"found {version}, supported 1 or {STATE_SCHEMA_VERSION}",),
            )
        if meta.get("workspace_id") != self.workspace_id:
            raise DridhError(
                ErrorCode.STATE_CORRUPT,
                "The state database belongs to a different workspace.",
                recovery="Do not copy state between projects. Initialise a workspace for this project.",
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection is None:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "The state store is not open.")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise busy_error(busy_timeout_ms()) from None
            raise
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def read(self) -> sqlite3.Connection:
        if self.connection is None:
            raise DridhError(ErrorCode.INTERNAL_ERROR, "The state store is not open.")
        return self.connection


def append_ledger(connection: sqlite3.Connection, workflow_id: str | None, event_type: str, actor: str, payload: dict[str, Any]) -> int:
    """Append one event to the hash-chained ledger. Call inside a transaction."""

    safe_payload = redact(payload)
    body = canonical_json(safe_payload)
    created = utc_now()
    row = connection.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    previous = row["hash"] if row is not None else GENESIS_HASH
    digest = _digest(previous, workflow_id or "", event_type, actor, body, created)
    cursor = connection.execute(
        "INSERT INTO ledger(workflow_id, event_type, actor, payload, created_at, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (workflow_id, event_type, actor, body, created, previous, digest),
    )
    return int(cursor.lastrowid)


def verify_ledger(connection: sqlite3.Connection) -> dict[str, Any]:
    """Recompute the hash chain. Returns the verdict, the event count and the head hash."""

    previous = GENESIS_HASH
    count = 0
    for row in connection.execute("SELECT * FROM ledger ORDER BY seq ASC"):
        count += 1
        expected = _digest(previous, row["workflow_id"] or "", row["event_type"], row["actor"], row["payload"], row["created_at"])
        if row["prev_hash"] != previous or row["hash"] != expected:
            return {"ok": False, "events": count, "first_bad_seq": row["seq"], "head": None}
        previous = row["hash"]
    return {"ok": True, "events": count, "first_bad_seq": None, "head": previous}
