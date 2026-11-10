"""Installation of the shared Dridh runtime into an OpenCode configuration directory."""

from __future__ import annotations

from dridh.install.installer import (
    apply_install,
    apply_uninstall,
    plan_install,
    plan_uninstall,
    resolve_layout,
    status,
)

__all__ = ["apply_install", "apply_uninstall", "plan_install", "plan_uninstall", "resolve_layout", "status"]
