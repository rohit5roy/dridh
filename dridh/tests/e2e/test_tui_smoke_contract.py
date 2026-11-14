from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tui_smoke_contract import FULL_REQUIRED_SCREENSHOTS, validate_tui_evidence


def _write_png(path: Path, width: int = 160, height: int = 90) -> None:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"\x00" + b"\x00\x00\x00" * width
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw * height))
        + chunk(b"IEND", b"")
    )


def _evidence(tmp_path: Path, *, omit: str | None = None) -> dict[str, object]:
    for name in FULL_REQUIRED_SCREENSHOTS:
        if name == omit:
            continue
        (tmp_path / f"{name}.txt").write_text("screen")
        (tmp_path / f"{name}.ansi").write_text("screen")
        _write_png(tmp_path / f"{name}.png")
    return {"required_screenshots": list(FULL_REQUIRED_SCREENSHOTS),
            "steps": [{"step": name} for name in FULL_REQUIRED_SCREENSHOTS]}


def test_required_screenshots_validate_even_when_optional_glimpses_are_absent(tmp_path: Path) -> None:
    manifest = _evidence(tmp_path)
    result = validate_tui_evidence(tmp_path, manifest, exit_code=0)
    assert result["passed"] is True
    assert result["screens"][".png"] == len(FULL_REQUIRED_SCREENSHOTS)


def test_missing_required_file_fails_regardless_of_screenshot_total(tmp_path: Path) -> None:
    missing = FULL_REQUIRED_SCREENSHOTS[0]
    manifest = _evidence(tmp_path, omit=missing)
    # An optional capture must not stand in for the missing required name.
    for suffix in (".txt", ".ansi", ".png"):
        (tmp_path / f"optional-glimpse{suffix}").write_bytes(b"extra")
    result = validate_tui_evidence(tmp_path, manifest, exit_code=0)
    assert result["passed"] is False
    assert missing in result["missing_screenshots"][".png"]


def test_manifest_drift_is_reported(tmp_path: Path) -> None:
    manifest = _evidence(tmp_path)
    manifest["required_screenshots"] = ["optional-glimpse"]
    result = validate_tui_evidence(tmp_path, manifest, exit_code=0)
    assert result["passed"] is False
    assert any("does not match" in error for error in result["errors"])


def test_contract_requires_all_formats_and_16_by_9(tmp_path: Path) -> None:
    manifest = _evidence(tmp_path)
    (tmp_path / f"{FULL_REQUIRED_SCREENSHOTS[0]}.png").write_bytes(b"not a png")
    result = validate_tui_evidence(tmp_path, manifest, exit_code=0)
    assert result["passed"] is False
    assert result["png_aspect_ratio"] != "16:9"


def test_glimpse_skips_screens_without_side_by_side_layout(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("tui_smoke", Path(__file__).with_name("tui_smoke.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "screen", lambda ansi=False: "short lived indexing state" if not ansi else "ansi")
    smoke = module.Smoke(tmp_path)
    assert smoke.glimpse("optional-glimpse", "indexing", timeout=0.1) is False
    assert not list(tmp_path.iterdir())


def test_missed_glimpse_is_optional(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("tui_smoke_missed", Path(__file__).with_name("tui_smoke.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "screen", lambda ansi=False: "indexing already finished" if not ansi else "")
    smoke = module.Smoke(tmp_path)
    assert smoke.glimpse("6b-indexing", "spinner", timeout=0.1) is False
    assert smoke.steps == []
    assert not list(tmp_path.iterdir())


def test_wait_timeout_preserves_original_error_and_saves_failure_capture(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("tui_smoke", Path(__file__).with_name("tui_smoke.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "screen", lambda ansi=False: "last visible content" if not ansi else "raw ansi")
    monkeypatch.setattr(module, "render_png", lambda _ansi, path: path.write_bytes(b"png") > 0)
    smoke = module.Smoke(tmp_path)
    try:
        smoke.wait("required", "not present", timeout=0)
    except module.StepFailed as exc:
        message = str(exc)
    else:
        raise AssertionError("wait should time out")
    assert "did not appear within 0 s" in message
    assert "last visible content" in message
    assert (tmp_path / "required-FAILED.txt").is_file()
    assert (tmp_path / "required-FAILED.ansi").is_file()
    assert (tmp_path / "required-FAILED.png").is_file()
