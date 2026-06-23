"""Flatpak packaging contract checks (#211)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FLATPAK_DIR = REPO_ROOT / "desktop" / "flatpak"

APP_ID = "com.vouchdev.vouch"
MANIFEST = FLATPAK_DIR / "com.vouchdev.vouch.yaml"


@pytest.fixture
def manifest_text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def test_flatpak_directory_layout() -> None:
    expected = [
        MANIFEST,
        FLATPAK_DIR / "com.vouchdev.vouch.desktop",
        FLATPAK_DIR / "com.vouchdev.vouch.metainfo.xml",
        FLATPAK_DIR / "vouch-review-ui.sh",
        FLATPAK_DIR / "requirements-flatpak.txt",
        FLATPAK_DIR / "share/icons/hicolor/scalable/apps/com.vouchdev.vouch.svg",
        FLATPAK_DIR / "flathub/com.vouchdev.vouch.json",
    ]
    for path in expected:
        assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"


def test_manifest_runtime_and_permissions(manifest_text: str) -> None:
    assert f"id: {APP_ID}" in manifest_text
    assert "runtime: org.freedesktop.Platform" in manifest_text
    assert (
        "runtime-version: '23.08'" in manifest_text
        or 'runtime-version: "23.08"' in manifest_text
    )
    assert "command: vouch-review-ui" in manifest_text
    assert "--share=network" in manifest_text
    assert "--filesystem=home" in manifest_text


def test_manifest_installs_web_extra(manifest_text: str) -> None:
    assert ".[web]" in manifest_text
    assert "name: vouch-desktop" in manifest_text


def test_desktop_entry_points_at_launcher() -> None:
    text = (FLATPAK_DIR / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    assert "Exec=vouch-review-ui" in text
    assert f"Icon={APP_ID}" in text


def test_metainfo_id_and_launchable() -> None:
    tree = ET.parse(FLATPAK_DIR / f"{APP_ID}.metainfo.xml")
    root = tree.getroot()
    assert root.findtext("id") == APP_ID
    launch = root.find("launchable")
    assert launch is not None
    assert launch.get("type") == "desktop-id"
    assert launch.text == f"{APP_ID}.desktop"


def test_metainfo_has_release() -> None:
    tree = ET.parse(FLATPAK_DIR / f"{APP_ID}.metainfo.xml")
    releases = tree.getroot().findall("releases/release")
    assert releases, "Flathub needs at least one release"
    assert releases[0].get("version")


def test_icon_png_sizes() -> None:
    sizes = (16, 32, 48, 64, 128, 256, 512)
    for size in sizes:
      png = (
          FLATPAK_DIR
          / "share/icons/hicolor"
          / f"{size}x{size}"
          / "apps"
          / f"{APP_ID}.png"
      )
      assert png.is_file(), f"missing icon {size}x{size}"
      assert png.stat().st_size > 64


def test_launcher_invokes_review_ui() -> None:
    text = (FLATPAK_DIR / "vouch-review-ui.sh").read_text(encoding="utf-8")
    assert "vouch review-ui" in text


def test_flathub_json_matches_app_id() -> None:
    data = json.loads((FLATPAK_DIR / "flathub/com.vouchdev.vouch.json").read_text())
    assert data["id"] == APP_ID
    assert data["runtime-version"] == "23.08"
    finish = set(data["finish-args"])
    assert "--filesystem=home" in finish
    assert "--share=network" in finish


def test_requirements_cover_web_stack() -> None:
    text = (FLATPAK_DIR / "requirements-flatpak.txt").read_text(encoding="utf-8")
    for dep in ("fastapi", "uvicorn", "websockets", "mcp", "pydantic"):
        assert dep in text


def test_validate_module_reports_clean() -> None:
    import sys

    sys.path.insert(0, str(FLATPAK_DIR))
    from lib.validate import run_all_validations

    report = run_all_validations(FLATPAK_DIR)
    assert report.ok(), [f"{i.level}: {i.message}" for i in report.errors]


def test_manifest_excludes_build_artifacts(manifest_text: str) -> None:
    assert "desktop/flatpak/build-dir" in manifest_text
    assert ".venv" in manifest_text


def test_issue_211_acceptance_documented() -> None:
    readme = (FLATPAK_DIR / "README.md").read_text(encoding="utf-8")
    assert "23.08" in readme
    assert "filesystem=home" in readme or "--filesystem=home" in readme
    assert "flathub" in readme.lower()


def test_generate_icons_script_is_idempotent() -> None:
    script = FLATPAK_DIR / "scripts/generate-icons.py"
    assert script.is_file()
    # script path referenced in Makefile
    makefile = (FLATPAK_DIR / "Makefile").read_text(encoding="utf-8")
    assert "generate-icons.py" in makefile


def test_flathub_submission_checklist_mentions_commit_pin() -> None:
    text = (FLATPAK_DIR / "flathub/SUBMISSION.md").read_text(encoding="utf-8")
    assert "REPLACE_WITH_TAG_COMMIT" in text
    assert "flatpak install flathub" in text
