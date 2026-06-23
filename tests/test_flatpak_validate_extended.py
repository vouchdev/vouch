"""Extended flatpak validation tests (#211)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FLATPAK_DIR = REPO_ROOT / "desktop" / "flatpak"
sys.path.insert(0, str(FLATPAK_DIR))

from lib.validate import (  # noqa: E402
    APP_ID,
    ICON_SIZES,
    REQUIRED_FINISH_ARGS,
    ValidationReport,
    validate_desktop_entry,
    validate_icons,
    validate_launcher_script,
    validate_manifest_content,
    validate_metainfo,
    validate_requirements_pin,
)


@pytest.fixture
def report() -> ValidationReport:
    return ValidationReport()


def test_required_finish_args_frozen() -> None:
    assert "--share=network" in REQUIRED_FINISH_ARGS
    assert "--filesystem=home" in REQUIRED_FINISH_ARGS


def test_manifest_content_clean(report: ValidationReport) -> None:
    validate_manifest_content(report, FLATPAK_DIR)
    assert not report.errors


def test_desktop_entry_clean(report: ValidationReport) -> None:
    validate_desktop_entry(report, FLATPAK_DIR)
    assert not report.errors


def test_metainfo_clean(report: ValidationReport) -> None:
    validate_metainfo(report, FLATPAK_DIR)
    assert not report.errors


def test_icons_all_sizes(report: ValidationReport) -> None:
    validate_icons(report, FLATPAK_DIR)
    assert not report.errors
    assert len(ICON_SIZES) == 7


def test_launcher_clean(report: ValidationReport) -> None:
    validate_launcher_script(report, FLATPAK_DIR)
    assert not report.errors


def test_requirements_clean(report: ValidationReport) -> None:
    validate_requirements_pin(report, FLATPAK_DIR)
    assert not report.errors


def test_app_id_matches_flathub_json() -> None:
    import json

    data = json.loads((FLATPAK_DIR / "flathub/com.vouchdev.vouch.json").read_text())
    assert data["id"] == APP_ID


@pytest.mark.parametrize("size", ICON_SIZES)
def test_each_icon_png_magic(size: int) -> None:
    path = (
        FLATPAK_DIR
        / "share/icons/hicolor"
        / f"{size}x{size}"
        / "apps"
        / f"{APP_ID}.png"
    )
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
