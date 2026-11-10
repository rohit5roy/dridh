"""Safe workspace backup, restore and integrity verification."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from dridh.common.errors import DridhError, ErrorCode

_BACKUP_FORMAT = "dridh-workspace-backup-v1"
_RESERVED_RESTORE_PATHS = {
    (".opencode", "dridh", "state", "dridh.sqlite"),
    (".opencode", "dridh", "state", "dridh.sqlite-wal"),
    (".opencode", "dridh", "state", "dridh.sqlite-shm"),
}


def integrity_check(db_path: Path) -> dict[str, Any]:
    """Check SQLite structural and foreign-key integrity, including schema metadata."""
    if not db_path.is_file():
        raise DridhError(ErrorCode.STATE_CORRUPT, "State database does not exist.", details=(str(db_path),))
    try:
        with closing(sqlite3.connect(db_path)) as conn:  # `with connect()` only commits; closing() is what releases the file
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            foreign_key_violations = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row[0]) if row else 0
    except (sqlite3.Error, TypeError, ValueError):
        raise DridhError(ErrorCode.STATE_CORRUPT, "State database could not be verified.") from None
    return {
        "ok": quick == "ok" and not foreign_key_violations and version > 0,
        "quick_check": quick,
        "foreign_key_violations": foreign_key_violations,
        "schema_version": version,
    }


def backup_workspace(*, workspace_root: Path, db_path: Path, destination: Path, dridh_version: str) -> dict[str, Any]:
    """Create an atomic ZIP with an SQLite online snapshot and workspace artefacts."""
    workspace_root = workspace_root.expanduser().resolve()
    requested_destination = destination.expanduser()
    if requested_destination.is_symlink():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "Backup destination must not be a symbolic link.")
    destination = requested_destination.resolve()
    if destination == workspace_root or workspace_root in destination.parents:
        raise DridhError(ErrorCode.VALIDATION_FAILED, "Backup destination must be outside the workspace.")
    if destination.exists() and destination.is_dir():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "Backup destination must be a file path.")
    if not db_path.is_file():
        raise DridhError(ErrorCode.STATE_CORRUPT, "State database does not exist.", details=(str(db_path),))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dridh-backup-", dir=destination.parent) as temporary:
        temporary_root = Path(temporary)
        snapshot = temporary_root / "state.sqlite"
        archive = temporary_root / "backup.zip"

        try:
            with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
        except sqlite3.Error:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Could not create a consistent SQLite backup snapshot.") from None

        verdict = integrity_check(snapshot)
        if not verdict["ok"]:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Backup snapshot failed integrity checks.")

        metadata = {
            "format": _BACKUP_FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "dridh_version": dridh_version,
            "schema_version": verdict["schema_version"],
            "workspace_root_name": workspace_root.name,
            "database": "state.sqlite",
            "integrity": verdict,
        }

        state_root = workspace_root / ".opencode" / "dridh"
        artefacts: list[Path] = []
        if state_root.exists():
            for candidate in state_root.rglob("*"):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if any(part in {".git", "node_modules", "__pycache__"} for part in candidate.parts):
                    continue
                if candidate.name in {"dridh.sqlite", "dridh.sqlite-wal", "dridh.sqlite-shm"}:
                    continue
                artefacts.append(candidate)

        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            bundle.write(snapshot, "state.sqlite")
            bundle.writestr("backup.json", json.dumps(metadata, indent=2, sort_keys=True))
            for artefact in artefacts:
                bundle.write(artefact, artefact.relative_to(workspace_root).as_posix())

        # The final path is replaced only after the complete archive has been written.
        os.replace(archive, destination)

    return {
        "path": str(destination),
        "files": len(artefacts),
        "schema_version": verdict["schema_version"],
        "integrity": "ok",
    }


def _validated_members(bundle: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Validate every archive member before writing any file to disk."""
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    file_paths: set[tuple[str, ...]] = set()

    for info in bundle.infolist():
        name = info.filename
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains an unsafe archive path.")
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains an unsafe archive path.")
        if path.parts[0] not in {"backup.json", "state.sqlite", ".opencode"}:
            raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains files outside the Dridh workspace state directory.")
        if path.parts[0] == ".opencode":
            if path.parts == (".opencode",):
                if not info.is_dir():
                    raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains an invalid OpenCode directory entry.")
            elif path.parts[1] != "dridh":
                raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains files outside the Dridh workspace state directory.")
        if path.parts in _RESERVED_RESTORE_PATHS:
            raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup attempts to replace the canonical state database.")
        if path.parts[0] in {"backup.json", "state.sqlite"} and (len(path.parts) != 1 or info.is_dir()):
            raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains an invalid archive layout.")
        if path.as_posix() in seen:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Backup contains duplicate archive paths.")
        seen.add(path.as_posix())

        if info.flag_bits & 0x1:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Encrypted backup entries are not supported.")
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        allowed_types = {0, stat.S_IFREG, stat.S_IFDIR}
        if file_type not in allowed_types or (file_type == stat.S_IFLNK):
            raise DridhError(ErrorCode.AUTHORIZATION_DENIED, "Backup contains a symbolic link or special file.")
        if info.is_dir() and file_type == stat.S_IFREG:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Backup contains an invalid directory entry.")
        if not info.is_dir():
            file_paths.add(path.parts)
        members.append((info, path))

    if "backup.json" not in seen or "state.sqlite" not in seen:
        raise DridhError(ErrorCode.STATE_CORRUPT, "Not a Dridh workspace backup.")

    for _, path in members:
        for index in range(1, len(path.parts)):
            if path.parts[:index] in file_paths:
                raise DridhError(ErrorCode.STATE_CORRUPT, "Backup file paths conflict with directory paths.")

    return members


def _rebind_workspace_metadata(staging: Path, destination: Path, database: Path) -> None:
    """Bind restored workspace metadata/config to its new absolute project root."""
    descriptor_path = staging / ".opencode" / "dridh" / "workspace.json"
    config_path = staging / ".opencode" / "dridh" / "dridh.yaml"
    try:
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        database_workspace_id = row[0] if row else None
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise DridhError(ErrorCode.STATE_CORRUPT, "Restored workspace metadata could not be verified.") from None

    if (
        not isinstance(descriptor, dict)
        or descriptor.get("schema") != "dridh.workspace"
        or descriptor.get("schema_version") != 1
        or not database_workspace_id
        or descriptor.get("workspace_id") != database_workspace_id
    ):
        raise DridhError(ErrorCode.STATE_CORRUPT, "Workspace descriptor does not match the restored state database.")

    descriptor["project_root"] = str(destination)
    descriptor_path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if config_path.is_file():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Restored Dridh configuration could not be verified.") from None
        if not isinstance(config, dict) or not isinstance(config.get("workspace"), dict):
            raise DridhError(ErrorCode.STATE_CORRUPT, "Restored Dridh configuration has an invalid workspace section.")
        workspace_config = config["workspace"]
        if workspace_config.get("id") != database_workspace_id:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Dridh configuration does not match the restored state database.")
        workspace_config["root"] = str(destination)
        workspace_config["name"] = destination.name
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False), encoding="utf-8")


def restore_workspace(*, backup_path: Path, destination: Path) -> dict[str, Any]:
    """Restore into an absent/empty destination using staged extraction and atomic publish."""
    requested_destination = destination.expanduser()
    if requested_destination.is_symlink():
        raise DridhError(ErrorCode.VALIDATION_FAILED, "Restore destination must not be a symbolic link.")
    destination = requested_destination.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise DridhError(ErrorCode.VALIDATION_FAILED, "Restore destination must be absent or empty.")
    if not backup_path.is_file():
        raise DridhError(ErrorCode.STATE_CORRUPT, "Backup archive does not exist.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.dridh-restore-", dir=destination.parent))
    published = False

    try:
        with zipfile.ZipFile(backup_path, "r") as bundle:
            members = _validated_members(bundle)
            metadata = json.loads(bundle.read("backup.json"))
            if not isinstance(metadata, dict) or metadata.get("format") != _BACKUP_FORMAT:
                raise DridhError(ErrorCode.STATE_CORRUPT, "Backup metadata is missing or has an unsupported format.")
            expected_version = metadata.get("schema_version")
            if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
                raise DridhError(ErrorCode.STATE_CORRUPT, "Backup metadata has an invalid schema version.")

            for info, path in members:
                target = staging.joinpath(*path.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)

        staged_database = staging / "state.sqlite"
        verdict = integrity_check(staged_database)
        if not verdict["ok"] or verdict["schema_version"] != expected_version:
            raise DridhError(ErrorCode.STATE_CORRUPT, "Restored backup failed integrity/schema verification.")

        restored_database = staging / ".opencode" / "dridh" / "state" / "dridh.sqlite"
        restored_database.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_database, restored_database)
        _rebind_workspace_metadata(staging, destination, restored_database)

        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
        published = True

        return {
            "destination": str(destination),
            "state_db": str(destination / ".opencode" / "dridh" / "state" / "dridh.sqlite"),
            "schema_version": verdict["schema_version"],
            "integrity": "ok",
            "metadata": metadata,
        }
    except DridhError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, TypeError, KeyError):
        raise DridhError(ErrorCode.STATE_CORRUPT, "Backup archive is invalid or could not be restored.") from None
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
