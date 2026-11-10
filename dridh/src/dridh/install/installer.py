"""Plan, apply, verify and remove the Dridh runtime in an OpenCode configuration directory.

Layout written under the configuration directory (default ``~/.config/opencode``,
or ``OPENCODE_CONFIG_DIR``)::

    opencode.json | opencode.jsonc      plugins array gains the Dridh plugin entry
    agents/dridh/*.md                  Dridh agent profiles
    dridh/plugin/dridh-bridge/         built plugin: package.json and dist/
    dridh/install-manifest.json        SHA-256 of every file Dridh manages
    dridh/backups/<stamp>/             originals of every file the installer changed

Every change is planned first. ``apply`` refuses to overwrite a file that Dridh
does not manage unless ``force`` is set, backs up every file it changes, and
writes atomically. ``uninstall`` restores the exact original configuration bytes
when the file is unchanged since installation, and otherwise removes only the
Dridh plugin entry, so later user edits are kept.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dridh
from dridh.common.errors import DridhError, ErrorCode
from dridh.install.merge import (
    COORDINATOR_AGENT,
    is_only_schema,
    merge_plugin_entry,
    remove_plugin_entry,
)

MANIFEST_SCHEMA = "dridh.install-manifest"
MANIFEST_VERSION = 1
CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
PLUGIN_NAME = "dridh-bridge"


@dataclass(frozen=True)
class Layout:
    config_dir: Path
    config_file: Path
    install_root: Path
    manifest: Path
    backups: Path
    plugin_dir: Path
    agents_dir: Path


@dataclass(frozen=True)
class FileAction:
    path: str
    action: str
    reason: str


@dataclass
class InstallPlan:
    layout: Layout
    desired: dict[str, bytes]
    actions: list[FileAction]
    conflicts: list[str]
    config_action: str
    config_before: str | None
    config_text: str | None
    config_notes: tuple[str, ...]
    created_config: bool
    created_plugins_key: bool
    removed_comments: bool
    plugin_entry: str
    previous_manifest: dict[str, Any] | None
    stale: list[str] = field(default_factory=list)
    conflict_paths: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[FileAction]:
        pending = [action for action in self.actions if action.action != "keep"]
        if self.config_action != "keep":
            pending.append(FileAction(self.layout.config_file.name, self.config_action, "OpenCode configuration"))
        return pending

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_dir": str(self.layout.config_dir),
            "config_file": str(self.layout.config_file),
            "config_action": self.config_action,
            "config_notes": list(self.config_notes),
            "plugin_entry": self.plugin_entry,
            "actions": [action.__dict__ for action in self.actions],
            "conflicts": list(self.conflicts),
            "removed_comments": self.removed_comments,
        }


@dataclass
class UninstallPlan:
    layout: Layout
    installed: bool
    actions: list[FileAction]
    conflicts: list[str]
    config_action: str
    config_before: str | None
    config_text: str | None
    restore_bytes: bytes | None
    manifest: dict[str, Any] | None
    conflict_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "config_action": self.config_action,
            "actions": [action.__dict__ for action in self.actions],
            "conflicts": list(self.conflicts),
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def resolve_layout(config_dir: str | Path | None = None, config_file: str | None = None) -> Layout:
    """Resolve the configuration directory and the file Dridh updates."""

    if config_dir is None:
        configured = os.environ.get("OPENCODE_CONFIG_DIR")
        base = Path(configured) if configured else Path.home() / ".config" / "opencode"
    else:
        base = Path(config_dir)
    base = base.expanduser()
    if not base.is_absolute():
        raise DridhError(ErrorCode.CONFIGURATION_ERROR, "config_dir must be an absolute path.", details=(str(base),))
    base = base.resolve(strict=False)
    if base.exists() and not base.is_dir():
        raise DridhError(ErrorCode.CONFIGURATION_ERROR, "config_dir is not a directory.", details=(str(base),))

    if config_file is not None:
        if config_file not in CONFIG_NAMES:
            raise DridhError(
                ErrorCode.VALIDATION_FAILED,
                "config_file must be opencode.json or opencode.jsonc.",
                details=(config_file,),
            )
        name = config_file
    else:
        present = [candidate for candidate in CONFIG_NAMES if (base / candidate).exists()]
        if len(present) > 1:
            raise DridhError(
                ErrorCode.CONFIGURATION_CONFLICT,
                "Both opencode.json and opencode.jsonc exist in the configuration directory.",
                recovery="Re-run with --config-file opencode.json or --config-file opencode.jsonc to choose the file Dridh updates.",
            )
        name = present[0] if present else "opencode.json"

    install_root = base / "dridh"
    return Layout(
        config_dir=base,
        config_file=base / name,
        install_root=install_root,
        manifest=install_root / "install-manifest.json",
        backups=install_root / "backups",
        plugin_dir=install_root / "plugin" / PLUGIN_NAME,
        agents_dir=base / "agents" / "dridh",
    )


def _plugin_files(source: Path) -> dict[str, bytes]:
    package = source / "package.json"
    dist = source / "dist"
    if not package.is_file() or not (dist / "index.js").is_file():
        raise DridhError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "The Dridh plugin artifact is incomplete.",
            details=(f"expected {package} and {dist / 'index.js'}",),
            recovery="Build the plugin with npm run build, or reinstall the Dridh wheel that contains it.",
        )
    files = {"package.json": package.read_bytes()}
    for folder in (dist, source / "tui"):  # tui/: the OpenCode terminal UI part, compiled by OpenCode at load time
        for path in sorted(folder.rglob("*")) if folder.is_dir() else ():
            if path.is_file():
                files[path.relative_to(source).as_posix()] = path.read_bytes()
    return files


def _agent_files(source: Path) -> dict[str, bytes]:
    if not source.is_dir():
        raise DridhError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "The Dridh agent profile directory is missing.",
            details=(str(source),),
        )
    files = {path.relative_to(source).as_posix(): path.read_bytes() for path in sorted(source.rglob("*.md")) if path.is_file()}
    if not files:
        raise DridhError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "No Dridh agent profiles were found.",
            details=(str(source),),
        )
    return files


def desired_files(layout: Layout, plugin_source: Path, agent_source: Path) -> dict[str, bytes]:
    """Map every managed path, relative to the configuration directory, to its content."""

    desired: dict[str, bytes] = {}
    for relative, content in _plugin_files(plugin_source).items():
        desired[f"dridh/plugin/{PLUGIN_NAME}/{relative}"] = content
    for name, content in _agent_files(agent_source).items():
        desired[f"agents/dridh/{name}"] = content
    return desired


def _load_manifest(layout: Layout) -> dict[str, Any] | None:
    if not layout.manifest.is_file():
        return None
    try:
        manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DridhError(
            ErrorCode.STATE_CORRUPT,
            "The Dridh installation manifest is unreadable.",
            details=(str(layout.manifest),),
            recovery="Back up the file, then reinstall or remove the Dridh installation manually.",
        ) from None
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("schema_version") != MANIFEST_VERSION:
        raise DridhError(
            ErrorCode.SCHEMA_UNSUPPORTED,
            "The Dridh installation manifest has an unsupported schema.",
            details=(str(manifest.get("schema")), str(manifest.get("schema_version"))),
        )
    return manifest


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise DridhError(ErrorCode.CONFIGURATION_CONFLICT, "The configuration path is not a regular file.", details=(str(path),))
    return path.read_text(encoding="utf-8")


def plan_install(
    layout: Layout,
    *,
    plugin_source: Path,
    agent_source: Path,
    strip_comments: bool = False,
) -> InstallPlan:
    """Compute every change without writing anything."""

    desired = desired_files(layout, plugin_source, agent_source)
    manifest = _load_manifest(layout)
    managed: dict[str, str] = dict(manifest.get("files", {})) if manifest else {}

    actions: list[FileAction] = []
    conflicts: list[str] = []
    conflict_paths: list[str] = []
    for relative, content in sorted(desired.items()):
        target = layout.config_dir / relative
        digest = _sha(content)
        if target.is_file():
            current = _sha(target.read_bytes())
            if current == digest and managed.get(relative) in (None, digest):
                actions.append(FileAction(relative, "keep", "already current"))
            elif current == digest:
                # Right bytes, stale manifest entry: rewrite so the manifest (and the startup integrity check) agree.
                actions.append(FileAction(relative, "update", "already current; manifest entry refreshed"))
            elif managed.get(relative) == current:
                actions.append(FileAction(relative, "update", "managed by Dridh and outdated"))
            else:
                conflicts.append(f"{relative} exists and is not managed by Dridh")
                conflict_paths.append(relative)
        elif target.exists():
            conflicts.append(f"{relative} exists and is not a regular file")
        else:
            actions.append(FileAction(relative, "create", "new Dridh file"))

    stale = sorted(path for path in managed if path not in desired)
    for relative in stale:
        target = layout.config_dir / relative
        if not target.exists():
            continue
        if target.is_file() and _sha(target.read_bytes()) == managed[relative]:
            actions.append(FileAction(relative, "remove", "no longer shipped by Dridh"))
        else:
            conflicts.append(f"{relative} was modified after installation")
            conflict_paths.append(relative)

    plugin_entry = str(layout.plugin_dir)
    current_text = _read_text(layout.config_file)
    merged = merge_plugin_entry(current_text, plugin_entry, strip_comments=strip_comments)
    if not merged.changed:
        config_action = "keep"
    elif current_text is None:
        config_action = "create"
    else:
        config_action = "update"

    return InstallPlan(
        layout=layout,
        desired=desired,
        actions=actions,
        conflicts=conflicts,
        config_action=config_action,
        config_before=current_text,
        config_text=merged.text if merged.changed else None,
        config_notes=merged.notes,
        created_config=merged.created_file if merged.changed else bool(manifest and manifest.get("config_created")),
        created_plugins_key=merged.created_plugins_key if merged.changed else bool(manifest and manifest.get("created_plugins_key")),
        removed_comments=merged.removed_comments,
        plugin_entry=plugin_entry,
        previous_manifest=manifest,
        stale=stale,
        conflict_paths=conflict_paths,
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _backup_directory(layout: Layout, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = layout.backups / f"{stamp}-{label}"
    suffix = 1
    while directory.exists():
        directory = layout.backups / f"{stamp}-{label}-{suffix}"
        suffix += 1
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _plugin_metadata(desired: dict[str, bytes]) -> dict[str, str | None]:
    """SDK and package versions of the packaged plugin, read from its package.json (None when unavailable)."""

    for relative, content in desired.items():
        if relative.endswith("package.json") and "/plugin/" in f"/{relative}".replace("\\", "/"):
            try:
                document = json.loads(content.decode("utf-8"))
            except ValueError:
                break
            sdk = document.get("opencodePluginSdk") or (document.get("dependencies") or {}).get("@opencode/plugin")
            return {"plugin_package": document.get("name"), "plugin_package_version": document.get("version"), "plugin_sdk_version": sdk}
    return {"plugin_package": None, "plugin_package_version": None, "plugin_sdk_version": None}


def apply_install(plan: InstallPlan, *, force: bool = False) -> dict[str, Any]:
    """Apply a reviewed plan. Returns a summary of what changed."""

    if plan.conflicts and not force:
        raise DridhError(
            ErrorCode.CONFIGURATION_CONFLICT,
            "Installation would overwrite or remove files that Dridh does not manage.",
            details=tuple(plan.conflicts),
            recovery="Review the listed files, move them aside, or re-run with --force. Originals are backed up first.",
        )
    layout = plan.layout
    # A forced run must still replace/remove conflicting files even when nothing else changed.
    forced = (
        [FileAction(relative, "replace" if relative in plan.desired else "remove", "forced over a conflicting file")
         for relative in plan.conflict_paths]
        if force else []
    )
    if not plan.changes and not forced:
        return {"status": "unchanged", "changed": [], "backup": None}

    backup: Path | None = None
    backed_up: list[str] = []

    def ensure_backup() -> Path:
        nonlocal backup
        if backup is None:
            backup = _backup_directory(layout, "install")
        return backup

    config_backup_relative: str | None = None
    previous = plan.previous_manifest or {}
    if plan.config_before is not None and plan.config_action == "update":
        directory = ensure_backup()
        target = directory / layout.config_file.name
        target.write_text(plan.config_before, encoding="utf-8")
        config_backup_relative = target.relative_to(layout.config_dir).as_posix()
        backed_up.append(layout.config_file.name)
    elif previous.get("config_backup"):
        config_backup_relative = previous["config_backup"]

    operations = [(action.path, action.action) for action in plan.actions if action.action in ("create", "update", "remove")]
    if force:
        for relative in plan.conflict_paths:
            operations.append((relative, "replace" if relative in plan.desired else "remove"))

    for relative, action in operations:
        target = layout.config_dir / relative
        if target.exists() and action in ("update", "remove", "replace"):
            directory = ensure_backup()
            copy = directory / "files" / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(target.read_bytes())
            backed_up.append(relative)
        if action in ("create", "update", "replace"):
            _atomic_write(target, plan.desired[relative])
        elif action == "remove" and target.is_file():
            target.unlink()

    for relative in plan.stale:
        directory_path = (layout.config_dir / relative).parent
        if directory_path.is_dir() and not any(directory_path.iterdir()):
            directory_path.rmdir()

    if plan.config_text is not None:
        _atomic_write(layout.config_file, plan.config_text.encode("utf-8"))

    managed = {relative: _sha(content) for relative, content in plan.desired.items()}
    config_hash = None
    if layout.config_file.is_file():
        config_hash = _sha(layout.config_file.read_bytes())
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_VERSION,
        "dridh_version": dridh.__version__,
        "installed_at": _utc_now(),
        "config_file": layout.config_file.name,
        "config_sha256": config_hash,
        "config_created": plan.created_config if previous == {} else bool(previous.get("config_created")),
        "created_plugins_key": plan.created_plugins_key if previous == {} else bool(previous.get("created_plugins_key")),
        "config_backup": config_backup_relative,
        "plugin_entry": plan.plugin_entry,
        "files": managed,
        # Additive provenance (plan section 2.2); older manifests simply lack these keys.
        **_plugin_metadata(plan.desired),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "owned_config_entries": [f"plugins: {plan.plugin_entry}"],
    }
    _atomic_write(layout.manifest, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    return {
        "status": "changed",
        "changed": [action.__dict__ for action in [*plan.changes, *forced]],
        "backup": backup.relative_to(layout.config_dir).as_posix() if backup else None,
        "backed_up": backed_up,
        "config_notes": list(plan.config_notes),
    }


def plan_uninstall(layout: Layout) -> UninstallPlan:
    """Compute the removal without writing anything."""

    manifest = _load_manifest(layout)
    if manifest is None:
        return UninstallPlan(layout, False, [], [], "keep", None, None, None, None)

    actions: list[FileAction] = []
    conflicts: list[str] = []
    conflict_paths: list[str] = []
    for relative, digest in sorted(manifest.get("files", {}).items()):
        target = layout.config_dir / relative
        if not target.exists():
            continue
        if target.is_file() and _sha(target.read_bytes()) == digest:
            actions.append(FileAction(relative, "remove", "installed by Dridh"))
        else:
            conflicts.append(f"{relative} was modified after installation")
            conflict_paths.append(relative)

    current = _read_text(layout.config_file)
    config_action = "keep"
    new_text: str | None = None
    restore: bytes | None = None
    backup_path = layout.config_dir / manifest["config_backup"] if manifest.get("config_backup") else None
    if current is not None:
        if manifest.get("config_sha256") == _sha(current.encode("utf-8")) and backup_path is not None and backup_path.is_file():
            restore = backup_path.read_bytes()
            config_action = "restore"
        elif manifest.get("config_sha256") == _sha(current.encode("utf-8")) and manifest.get("config_created"):
            config_action = "remove"
        else:
            removed = remove_plugin_entry(current, manifest.get("plugin_entry", str(layout.plugin_dir)))
            if removed.changed:
                if manifest.get("config_created") and is_only_schema(removed.text or ""):
                    config_action = "remove"
                else:
                    config_action = "update"
                    new_text = removed.text
    return UninstallPlan(
        layout,
        True,
        actions,
        conflicts,
        config_action,
        current,
        new_text,
        restore,
        manifest,
        conflict_paths=conflict_paths,
    )


def _remove_empty_tree(directory: Path) -> None:
    """Remove ``directory`` and its subdirectories when nothing else remains in them."""

    if not directory.is_dir():
        return
    for child in sorted(directory.rglob("*"), reverse=True):
        if child.is_dir() and not any(child.iterdir()):
            child.rmdir()
    if not any(directory.iterdir()):
        directory.rmdir()


def apply_uninstall(plan: UninstallPlan, *, force: bool = False) -> dict[str, Any]:
    if not plan.installed:
        return {"status": "not_installed", "changed": []}
    if plan.conflicts and not force:
        raise DridhError(
            ErrorCode.CONFIGURATION_CONFLICT,
            "Uninstall would remove files that were modified after installation.",
            details=tuple(plan.conflicts),
            recovery="Review the listed files, or re-run with --force to remove them anyway.",
        )
    layout = plan.layout
    backup: Path | None = None

    def ensure_backup() -> Path:
        nonlocal backup
        if backup is None:
            backup = _backup_directory(layout, "uninstall")
        return backup

    if plan.config_action != "keep" and plan.config_before is not None:
        (ensure_backup() / layout.config_file.name).write_text(plan.config_before, encoding="utf-8")

    removals = [action.path for action in plan.actions]
    if force:
        removals.extend(plan.conflict_paths)
    for relative in removals:
        target = layout.config_dir / relative
        if target.is_file():
            copy = ensure_backup() / "files" / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(target.read_bytes())
            target.unlink()

    if plan.config_action == "restore" and plan.restore_bytes is not None:
        _atomic_write(layout.config_file, plan.restore_bytes)
    elif plan.config_action == "update" and plan.config_text is not None:
        _atomic_write(layout.config_file, plan.config_text.encode("utf-8"))
    elif plan.config_action == "remove" and layout.config_file.is_file():
        layout.config_file.unlink()

    for owned in (layout.install_root / "plugin", layout.agents_dir):
        _remove_empty_tree(owned)

    layout.manifest.unlink()
    return {
        "status": "removed",
        "changed": [action.__dict__ for action in plan.actions],
        "config_action": plan.config_action,
        "backup": backup.relative_to(layout.config_dir).as_posix() if backup else None,
    }


def status(layout: Layout) -> dict[str, Any]:
    manifest = _load_manifest(layout)
    report: dict[str, Any] = {
        "config_dir": str(layout.config_dir),
        "config_file": str(layout.config_file),
        "installed": manifest is not None,
        "files": {},
        "plugin_entry_present": False,
        "default_agent": None,
        "default_agent_is_coordinator": False,
        "backups": sorted(path.name for path in layout.backups.iterdir()) if layout.backups.is_dir() else [],
    }
    if manifest is not None:
        for relative, digest in sorted(manifest.get("files", {}).items()):
            target = layout.config_dir / relative
            if not target.is_file():
                report["files"][relative] = "missing"
            elif _sha(target.read_bytes()) == digest:
                report["files"][relative] = "ok"
            else:
                report["files"][relative] = "modified"
    text = _read_text(layout.config_file)
    if text is not None:
        from dridh.install.jsonc import load_jsonc

        document = load_jsonc(text).value
        plugins = document.get("plugins", []) if isinstance(document, dict) else []
        report["plugin_entry_present"] = any(
            item == str(layout.plugin_dir) or (isinstance(item, dict) and item.get("package") == str(layout.plugin_dir))
            for item in plugins
        )
        if isinstance(document, dict) and "default_agent" in document:
            report["default_agent"] = document["default_agent"]
            report["default_agent_is_coordinator"] = document["default_agent"] == COORDINATOR_AGENT
    return report
