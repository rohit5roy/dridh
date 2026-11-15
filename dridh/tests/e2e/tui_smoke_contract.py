"""Shared screenshot contract between the TUI driver and the Docker E2E scenario."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

SCREENSHOT_FORMATS = (".txt", ".ansi", ".png")

FULL_REQUIRED_SCREENSHOTS = (
    "3-set-up",
    "4-ready",
    "7-indexed",
    "7a-setup-step-3",
    "7b-ready-in-other-folder",
    "7c-search-model-changed",
    "7e-re-indexed",
    "7f-outside-change-indexed",
    "8-error-message",
    "9-feature-overview",
    "10-use-cases-document",
    "11-technical-design-document",
    "12-review-checklist",
    "13-observability-metrics",
    "14-completion-matrix-deviation",
    "15-execution-lot-review",
)

FIRST_RUN_REQUIRED_SCREENSHOTS = (
    "1-first-run",
    "3-set-up",
    "4-ready",
    "7-indexed",
    "8-error-message",
)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as image:
        header = image.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"{path.name} is not a valid PNG header")
    return struct.unpack(">II", header[16:24])


def validate_tui_evidence(
    output_dir: Path,
    manifest: Any,
    *,
    exit_code: int,
    required_screenshots: tuple[str, ...] = FULL_REQUIRED_SCREENSHOTS,
) -> dict[str, Any]:
    """Validate required screenshot names, their recorded steps, and PNG aspect ratios.

    Additional screenshots (the short-lived indexing glimpses or failure captures) are allowed and never required.
    """
    errors: list[str] = []
    if exit_code != 0:
        errors.append(f"smoke driver exited {exit_code}")
    if not isinstance(manifest, dict):
        manifest = {}
        errors.append("steps.json is not a manifest object")

    recorded_required = manifest.get("required_screenshots")
    if recorded_required != list(required_screenshots):
        errors.append("steps.json required_screenshots does not match the expected screenshot contract")
    steps = manifest.get("steps")
    if not isinstance(steps, list):
        steps = []
        errors.append("steps.json steps is not a list")
    completed_steps = {item.get("step") for item in steps if isinstance(item, dict)}
    missing_steps = sorted(set(required_screenshots) - completed_steps)
    if missing_steps:
        errors.append(f"required screenshot steps missing from steps.json: {', '.join(missing_steps)}")

    files_by_format = {
        suffix: {path.stem for path in output_dir.glob(f"*{suffix}") if path.is_file()}
        for suffix in SCREENSHOT_FORMATS
    }
    missing_files = {
        suffix: sorted(set(required_screenshots) - stems)
        for suffix, stems in files_by_format.items()
    }
    for suffix, names in missing_files.items():
        if names:
            errors.append(f"required {suffix} screenshots missing: {', '.join(names)}")

    png_errors: list[str] = []
    for path in sorted(output_dir.glob("*.png")):
        try:
            width, height = _png_dimensions(path)
        except (OSError, ValueError, struct.error) as exc:
            png_errors.append(f"{path.name}: {exc}")
            continue
        if width * 9 != height * 16:
            png_errors.append(f"{path.name}: {width}x{height}, expected 16:9")
    if png_errors:
        errors.extend(png_errors)

    return {
        "passed": not errors,
        "errors": errors,
        "required_screenshots": list(required_screenshots),
        "screens": {suffix: len(stems) for suffix, stems in files_by_format.items()},
        "missing_screenshots": missing_files,
        "png_aspect_ratio": "16:9" if not png_errors else png_errors,
        "steps": len(steps),
    }
