"""Every document Dridh wrote, as a tree per feature, with each document's status.

The same data feeds `dridh documents tree` (text or JSON), the terminal panel's Files view and the desktop panel's
"All documents" tree. Read-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from dridh.state.documents import content_hash
from dridh.state.store import StateStore
from dridh.ui.state import STAGE_LABELS
from dridh.workspace.documents_layout import DOCS_ROOT
from dridh.workspace.layout import WorkspaceInfo

# What a status means, in words a person reads at a glance.
STATUS_LABELS = {
    "approved": "approved",
    "in_review": "waiting for review",
    "changes_requested": "changes requested",
    "stale": "changed since approval",
    "edited": "edited outside Dridh",
    "recorded": "recorded",
    "missing": "missing on disk",
    "untracked": "not recorded by Dridh",
}


def _status(connection: sqlite3.Connection, workspace_id: str, document: sqlite3.Row | None, path: Path) -> str:
    if document is None:
        return "untracked"
    if not path.is_file():
        return "missing"
    revision = connection.execute(
        "SELECT id, content_sha256 FROM document_revision WHERE document_id=? ORDER BY revision_number DESC LIMIT 1",
        (document["id"],)).fetchone()
    if revision is None:
        return "untracked"
    if content_hash(path.read_text(encoding="utf-8")) != revision["content_sha256"]:
        return "edited"
    review = connection.execute(
        "SELECT status FROM review WHERE workspace_id=? AND document_id=? AND revision_id=? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
        (workspace_id, document["id"], revision["id"])).fetchone()
    if review is not None:
        return {"APPROVED": "approved", "REJECTED": "changes_requested", "CHANGES_REQUESTED": "changes_requested"}.get(review["status"], "in_review")
    approved_before = connection.execute(
        "SELECT 1 FROM review WHERE workspace_id=? AND document_id=? AND status='APPROVED' LIMIT 1",
        (workspace_id, document["id"])).fetchone()
    return "stale" if approved_before else "recorded"


def _insert(tree: list[dict[str, Any]], parts: list[str], leaf: dict[str, Any]) -> None:
    """Puts ``leaf`` under the folders named by ``parts``, creating them as needed."""
    level = tree
    for part in parts[:-1]:
        folder = next((node for node in level if node["type"] == "folder" and node["name"] == part), None)
        if folder is None:
            folder = {"type": "folder", "name": part, "children": []}
            level.append(folder)
        level = folder["children"]
    level.append({"type": "file", "name": parts[-1], **leaf})


def _sort(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for node in nodes:
        if node["type"] == "folder":
            _sort(node["children"])
    nodes.sort(key=lambda node: (node["type"] == "folder", node["name"]))  # numbered files first, then sub-folders
    return nodes


def document_tree(workspace: WorkspaceInfo) -> dict[str, Any]:
    """``{root, features: [{slug, name, stage, folder, tree, counts}], other: {tree}}``.

    A feature with a folder lists every Markdown file in it (recorded by Dridh or not). Documents Dridh recorded outside
    any feature folder (features from before feature folders) are listed under ``other`` by their own paths.
    """
    root = Path(workspace.project_root)
    with StateStore(workspace.state_db, workspace.workspace_id) as store:
        connection = store.read()
        documents = {row["relative_path"]: row for row in connection.execute(
            "SELECT id, relative_path FROM document WHERE workspace_id=?", (workspace.workspace_id,))}
        features = connection.execute(
            "SELECT f.id, f.slug, f.name, f.stage, ff.folder FROM feature f LEFT JOIN feature_folder ff ON ff.feature_id=f.id "
            "WHERE f.workspace_id=? ORDER BY COALESCE(ff.serial, 1000), f.created_at", (workspace.workspace_id,)).fetchall()
        claimed: set[str] = set()
        result = []
        for feature in features:
            tree: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            folder = feature["folder"]
            if folder:
                base = root / folder
                paths = {p.relative_to(root).as_posix() for p in base.rglob("*.md")} if base.is_dir() else set()
                paths |= {path for path in documents if path.startswith(folder + "/")}
                for path in sorted(paths):
                    claimed.add(path)
                    status = _status(connection, workspace.workspace_id, documents.get(path), root / path)
                    counts[status] = counts.get(status, 0) + 1
                    _insert(tree, path[len(folder) + 1:].split("/"), {"path": path, "status": status, "label": STATUS_LABELS[status]})
            result.append({"slug": feature["slug"], "name": feature["name"], "stage": feature["stage"],
                           "stage_label": STAGE_LABELS.get(feature["stage"], feature["stage"]), "folder": folder,
                           "tree": _sort(tree), "counts": counts})
        other: list[dict[str, Any]] = []
        for path in sorted(set(documents) - claimed):
            if not path.endswith(".md"):
                continue
            status = _status(connection, workspace.workspace_id, documents[path], root / path)
            _insert(other, path.split("/"), {"path": path, "status": status, "label": STATUS_LABELS[status]})
    return {"root": DOCS_ROOT, "features": result, "other": _sort(other)}


def render_tree(data: dict[str, Any]) -> str:
    """The tree as text, the way `tree` draws folders, with each document's status beside it."""
    lines: list[str] = []

    def walk(nodes: list[dict[str, Any]], prefix: str) -> None:
        for index, node in enumerate(nodes):
            last = index == len(nodes) - 1
            branch = "└── " if last else "├── "
            if node["type"] == "folder":
                lines.append(f"{prefix}{branch}{node['name']}/")
                walk(node["children"], prefix + ("    " if last else "│   "))
            else:
                name = f"{prefix}{branch}{node['name']}"
                lines.append(f"{name:<64} {node['label']}")

    features = [item for item in data["features"] if item["folder"]]
    if features:
        lines.append(f"{data['root']}/")
        for index, feature in enumerate(features):
            last = index == len(features) - 1
            lines.append(f"{'└── ' if last else '├── '}{feature['folder'].rsplit('/', 1)[-1]}/    {feature['name']} · {feature['stage_label']}")
            walk(feature["tree"], "    " if last else "│   ")
            if not feature["tree"]:
                lines.append(("    " if last else "│   ") + "(no documents yet)")
    if data["other"]:
        if lines:
            lines.append("")
        lines.append("Older documents (before feature folders):")
        walk(data["other"], "")
    return "\n".join(lines) if lines else "No documents yet. They appear here as the agents write them."
