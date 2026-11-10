"""Locations of the Dridh assets packaged with the runtime."""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def asset_root() -> Path:
    return Path(str(resources.files("dridh"))) / "assets"


def default_plugin_source() -> Path:
    return asset_root() / "plugin" / "dridh-bridge"


def default_agent_source() -> Path:
    return asset_root() / "agents" / "dridh"
