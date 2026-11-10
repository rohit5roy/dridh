"""Idempotent, reversible merge of the Dridh plugin entry into an OpenCode configuration.

The merge touches exactly one key, ``plugins``, and only adds or removes the
Dridh plugin entry. Every other key, including unknown keys and the order of
existing keys, is preserved. ``default_agent`` is never written, and the merge
refuses to proceed when the configuration already selects the Coordinator as
the global default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from dridh.common.errors import DridhError, ErrorCode
from dridh.install.jsonc import load_jsonc

SCHEMA_URL = "https://opencode.ai/config.json"
COORDINATOR_AGENT = "dridh/coordinator"


@dataclass(frozen=True)
class MergeResult:
    text: str | None
    changed: bool
    created_file: bool
    created_plugins_key: bool
    removed_comments: bool
    notes: tuple[str, ...]


def _entry_matches(item: Any, entry: str) -> bool:
    if isinstance(item, str):
        return item == entry
    return isinstance(item, dict) and item.get("package") == entry


def _parse(original: str, strip_comments: bool) -> tuple[dict[str, Any], bool]:
    parsed = load_jsonc(original)
    if not isinstance(parsed.value, dict):
        raise DridhError(
            ErrorCode.CONFIGURATION_ERROR,
            "OpenCode configuration must be a JSON object.",
        )
    if parsed.had_comments and not strip_comments:
        raise DridhError(
            ErrorCode.CONFIGURATION_CONFLICT,
            "OpenCode configuration contains comments that a rewrite would remove.",
            details=("re-run the installer with --strip-comments; a backup of the original file is written first",),
        )
    return parsed.value, parsed.had_comments


def _serialise(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def merge_plugin_entry(original: str | None, entry: str, *, strip_comments: bool = False) -> MergeResult:
    """Return the configuration text with ``entry`` present in ``plugins``."""

    if original is None:
        document: dict[str, Any] = {"$schema": SCHEMA_URL}
        had_comments = False
        created_file = True
    else:
        document, had_comments = _parse(original, strip_comments)
        created_file = False

    if document.get("default_agent") == COORDINATOR_AGENT:
        raise DridhError(
            ErrorCode.CONFIGURATION_CONFLICT,
            "default_agent selects the Dridh Coordinator. Dridh never writes or changes default_agent.",
            recovery="Change or remove default_agent yourself, then re-run the installer.",
        )

    notes: list[str] = []
    created_key = False
    plugins = document.get("plugins")
    if plugins is None:
        plugins = []
        document["plugins"] = plugins
        created_key = True
        notes.append("created the plugins array")
    elif not isinstance(plugins, list):
        raise DridhError(ErrorCode.CONFIGURATION_ERROR, "plugins must be an array.")

    if "plugin" in document:
        notes.append("legacy singular 'plugin' key is left unchanged")

    if any(_entry_matches(item, entry) for item in plugins):
        if created_file or created_key:
            return MergeResult(
                text=_serialise(document),
                changed=True,
                created_file=created_file,
                created_plugins_key=created_key,
                removed_comments=had_comments,
                notes=tuple(notes),
            )
        return MergeResult(
            text=original,
            changed=False,
            created_file=False,
            created_plugins_key=False,
            removed_comments=False,
            notes=tuple(notes + ["plugins entry already present"]),
        )

    plugins.append(entry)
    notes.append(f"added plugins entry {entry}")
    return MergeResult(
        text=_serialise(document),
        changed=True,
        created_file=created_file,
        created_plugins_key=created_key,
        removed_comments=had_comments,
        notes=tuple(notes),
    )


def remove_plugin_entry(original: str, entry: str, *, strip_comments: bool = False) -> MergeResult:
    """Remove every ``plugins`` item that refers to ``entry``. Other keys are untouched."""

    document, had_comments = _parse(original, strip_comments)
    plugins = document.get("plugins")
    if not isinstance(plugins, list):
        return MergeResult(
            text=original,
            changed=False,
            created_file=False,
            created_plugins_key=False,
            removed_comments=False,
            notes=("no plugins array to update",),
        )
    remaining = [item for item in plugins if not _entry_matches(item, entry)]
    if len(remaining) == len(plugins):
        return MergeResult(
            text=original,
            changed=False,
            created_file=False,
            created_plugins_key=False,
            removed_comments=False,
            notes=("Dridh plugins entry not present",),
        )
    document["plugins"] = remaining
    return MergeResult(
        text=_serialise(document),
        changed=True,
        created_file=False,
        created_plugins_key=False,
        removed_comments=had_comments,
        notes=("removed Dridh plugins entry",),
    )


def is_only_schema(text: str) -> bool:
    """True when the configuration holds nothing but the published schema reference."""

    document, _ = _parse(text, strip_comments=True)
    return set(document) <= {"$schema"}
