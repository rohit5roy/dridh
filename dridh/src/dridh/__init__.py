"""Dridh harness runtime skeleton."""

from __future__ import annotations

__all__ = ["BRIDGE_PROTOCOL_VERSION", "__version__"]

# The one place the Dridh version is set. The wheel (hatch dynamic version), the release workflow, the install
# manifest, `dridh --version`, the bridge and the packaged plugin all read it from here. Releases move it forward
# automatically (scripts/version.py, .github/workflows/release.yml); do not lower it.
__version__ = "0.9.0rc2"
BRIDGE_PROTOCOL_VERSION = 1
