"""Deterministic story-to-code mapping (``code_repository_mapping``).

Links indexed code entities to the stories, components, use cases and flows they implement, using only recorded facts:

* ``FILE_OWNERSHIP``: the entity's file is inside a path (or glob) the approved story card owns.
* ``STORY_CHANGE``: the entity's file was changed by the story's passing Executor attempt (its recorded changed files, or
  the files of its commit when the evidence lacks them).
* ``VIA_STORY``: the component, use cases and flows a mapped story is linked to in the plan (``story``, ``story_use_case``),
  with the stories as evidence.

No model is involved and nothing is inferred from names. The table is a projection: it is rebuilt from the current index
and the plan, so a re-index (which recreates changed entities) or a new attempt is reflected by rebuilding it.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import uuid
from collections import defaultdict
from typing import Any

from dridh.code_reacher.indexer import require_index
from dridh.common.errors import DridhError, ErrorCode
from dridh.state.store import StateStore, utc_now
from dridh.workspace.layout import WorkspaceInfo

MAX_RESULTS = 500
_GLOB = re.compile(r"[*?\[]")
_FLOW = re.compile(r"^(SF|AF|EF|BE)-\d+(\.\d+)*$")


def _normalise(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/")


def owns(pattern: str, path: str) -> bool:
    """Whether a story's ownership entry covers ``path``: an exact file, a directory prefix or a glob."""
    owned, target = _normalise(pattern), _normalise(path)
    if not owned:
        return False
    if _GLOB.search(owned):
        return fnmatch.fnmatchcase(target, owned) or fnmatch.fnmatchcase(target, owned.rstrip("/*") + "/*")
    return target == owned or target.startswith(owned + "/")


def target_type(target_id: str) -> str:
    if target_id.startswith("UC-"):
        return "USE_CASE"
    if target_id.startswith("CMP-"):
        return "COMPONENT"
    if _FLOW.match(target_id):
        return "FLOW"
    return "STORY"


class StoryCodeMapper:
    def __init__(self, workspace: WorkspaceInfo) -> None:
        self.workspace = workspace

    # ------------------------------------------------------------------ rebuild

    def _stories(self) -> tuple[list[dict[str, Any]], list[str]]:
        from dridh.coordinator.lifecycle import FeatureLifecycle
        from dridh.planner.service import PlannerService

        sessions: dict[str, str] = {}
        for feature in FeatureLifecycle(self.workspace).list_features():
            if feature.get("planner_session_id"):
                sessions.setdefault(feature["planner_session_id"], f"feature {feature['slug']}")
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            for row in store.read().execute("SELECT DISTINCT planner_session_id FROM execution_run WHERE workspace_id=?", (self.workspace.workspace_id,)):
                sessions.setdefault(row[0], "execution run")
        stories: dict[str, dict[str, Any]] = {}
        notes: list[str] = []
        planner = PlannerService(self.workspace)
        for session, origin in sessions.items():
            try:
                for story in planner.planned_stories(session):
                    stories[story["story_id"]] = story
            except DridhError as exc:
                notes.append(f"{origin} (planner session {session}): plan not readable ({exc.message})")
        return list(stories.values()), notes

    def _changed_files(self, connection: Any) -> dict[str, dict[str, Any]]:
        """The latest passing attempt per story, with the files it changed."""
        latest: dict[str, dict[str, Any]] = {}
        rows = connection.execute(
            "SELECT id, story_id, commit_sha, evidence_json FROM story_attempt WHERE workspace_id=? AND status='PASSED' "
            "ORDER BY finished_at, rowid", (self.workspace.workspace_id,)).fetchall()
        for row in rows:
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except ValueError:
                evidence = {}
            files = [f for f in evidence.get("changed_files") or [] if isinstance(f, str)]
            if not files and row["commit_sha"]:
                files = self._commit_files(row["commit_sha"])
            latest[row["story_id"]] = {"attempt_id": row["id"], "commit_sha": row["commit_sha"], "files": {_normalise(f) for f in files}}
        return latest

    def _commit_files(self, sha: str) -> list[str]:
        if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
            return []
        completed = subprocess.run(["git", "show", "--no-renames", "--name-only", "--format=", sha], cwd=self.workspace.project_root,
                                   capture_output=True, text=True, timeout=30, check=False)  # noqa: S603, S607 - fixed argv
        return [line for line in completed.stdout.splitlines() if line.strip()] if completed.returncode == 0 else []

    def rebuild(self) -> dict[str, Any]:
        stories, notes = self._stories()
        workspace_id = self.workspace.workspace_id
        with StateStore(self.workspace.state_db, workspace_id) as store:
            with store.transaction() as connection:
                run = require_index(connection, workspace_id)
                entities = connection.execute(
                    "SELECT e.id, f.path FROM code_entity e JOIN code_file f ON f.id=e.file_id WHERE f.index_run_id=?", (run["id"],)).fetchall()
                by_path: dict[str, list[str]] = defaultdict(list)
                for row in entities:
                    by_path[_normalise(row["path"])].append(row["id"])
                changes = self._changed_files(connection)
                links: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
                for row in connection.execute("SELECT story_id, use_case_id, flow_id FROM story_use_case WHERE workspace_id=?", (workspace_id,)):
                    links[row["story_id"]].append((row["use_case_id"], row["flow_id"]))

                # (entity, target_type, target_id, mapping_type) -> evidence
                mappings: dict[tuple[str, str, str, str], dict[str, Any]] = {}
                mapped_by_story: dict[str, set[str]] = defaultdict(set)
                for story in stories:
                    sid = story["story_id"]
                    for path, ids in by_path.items():
                        pattern = next((p for p in story.get("file_ownership") or [] if owns(p, path)), None)
                        if pattern is not None:
                            for entity in ids:
                                mappings[(entity, "STORY", sid, "FILE_OWNERSHIP")] = {"path": path, "owned": pattern}
                                mapped_by_story[sid].add(entity)
                    change = changes.get(sid)
                    for path in sorted(change["files"]) if change else ():
                        for entity in by_path.get(path, ()):
                            mappings[(entity, "STORY", sid, "STORY_CHANGE")] = {"path": path, "attempt_id": change["attempt_id"], "commit_sha": change["commit_sha"]}
                            mapped_by_story[sid].add(entity)
                for story in stories:
                    sid = story["story_id"]
                    targets = [("COMPONENT", story["component_id"])] if story.get("component_id") else []
                    for use_case, flow in links.get(sid) or [(uc, None) for uc in story.get("use_case_ids") or []]:
                        targets.append(("USE_CASE", use_case))
                        if flow:
                            targets.append(("FLOW", flow))
                    for entity in mapped_by_story.get(sid, ()):
                        for kind, target in targets:
                            evidence = mappings.setdefault((entity, kind, target, "VIA_STORY"), {"stories": []})
                            if sid not in evidence["stories"]:
                                evidence["stories"].append(sid)

                connection.execute("DELETE FROM code_repository_mapping WHERE workspace_id=?", (workspace_id,))
                now = utc_now()
                connection.executemany(
                    "INSERT INTO code_repository_mapping(id,workspace_id,entity_id,target_type,target_id,mapping_type,evidence,source_sha,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(str(uuid.uuid4()), workspace_id, entity, kind, target, mapping, json.dumps(evidence, sort_keys=True), run["source_sha"], now)
                     for (entity, kind, target, mapping), evidence in sorted(mappings.items())])
        counts: dict[str, int] = defaultdict(int)
        for (_, kind, _, mapping) in mappings:
            counts[f"{kind}:{mapping}"] += 1
        unmapped = sorted(s["story_id"] for s in stories if s["story_id"] not in mapped_by_story)
        return {"source_sha": run["source_sha"], "stories": len(stories), "mappings": len(mappings), "by_kind": dict(sorted(counts.items())),
                "stories_without_code": unmapped, "notes": notes}

    # ------------------------------------------------------------------ queries

    def code_for(self, target_id: str) -> dict[str, Any]:
        """The indexed code that implements a story, component, use case or flow."""
        target_id = target_id.strip()
        kind = target_type(target_id)
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            connection = store.read()
            rows = connection.execute(
                "SELECT f.path, e.qualified_name, e.entity_type, e.start_line, m.mapping_type, m.evidence, m.source_sha "
                "FROM code_repository_mapping m JOIN code_entity e ON e.id=m.entity_id JOIN code_file f ON f.id=e.file_id "
                "WHERE m.workspace_id=? AND m.target_type=? AND m.target_id=? ORDER BY f.path, e.start_line, m.mapping_type LIMIT ?",
                (self.workspace.workspace_id, kind, target_id, MAX_RESULTS)).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM code_repository_mapping WHERE workspace_id=?", (self.workspace.workspace_id,)).fetchone()[0]
        files: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            files[row["path"]].append({"entity": row["qualified_name"], "type": row["entity_type"], "line": row["start_line"],
                                       "mapping": row["mapping_type"], "evidence": json.loads(row["evidence"])})
        result: dict[str, Any] = {"target": target_id, "target_type": kind, "files": dict(files), "entities": len(rows), "truncated": len(rows) == MAX_RESULTS}
        if total == 0:
            result["note"] = "No mapping has been built yet. Run `dridh code map`."
        return result

    def targets_for(self, path: str) -> dict[str, Any]:
        """Which stories, components, use cases and flows a file's code is mapped to."""
        target = _normalise(path)
        if not target or target.startswith("../") or target.startswith("/"):
            raise DridhError(ErrorCode.VALIDATION_FAILED, "path must be project-relative.")
        with StateStore(self.workspace.state_db, self.workspace.workspace_id) as store:
            rows = store.read().execute(
                "SELECT DISTINCT m.target_type, m.target_id, m.mapping_type FROM code_repository_mapping m JOIN code_entity e ON e.id=m.entity_id "
                "JOIN code_file f ON f.id=e.file_id WHERE m.workspace_id=? AND f.path=? ORDER BY m.target_type, m.target_id, m.mapping_type",
                (self.workspace.workspace_id, target)).fetchall()
        grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for row in rows:
            grouped[row["target_type"]][row["target_id"]].append(row["mapping_type"])
        return {"path": target, "targets": {kind: dict(items) for kind, items in grouped.items()}}


def refresh_mapping(workspace: WorkspaceInfo) -> dict[str, Any]:
    """Rebuild after a re-index. Best effort: a mapping problem never fails indexing or delivery; it is reported instead."""
    try:
        result = StoryCodeMapper(workspace).rebuild()
    except Exception as exc:  # noqa: BLE001 - reported to the caller, which carries on
        return {"rebuilt": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"rebuilt": True, "mappings": result["mappings"], "stories": result["stories"], "notes": result["notes"]}
