"""Capabilities descriptor — must match the JSONL handler registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import capabilities
from vouch.jsonl_server import HANDLERS


def test_capabilities_matches_jsonl_handlers() -> None:
    caps = capabilities.capabilities()
    declared = set(caps.methods)
    implemented = set(HANDLERS.keys())
    assert declared == implemented, (
        f"capabilities/handlers mismatch: "
        f"missing handlers={declared - implemented}, "
        f"missing capabilities={implemented - declared}"
    )


_PACKAGE_JSON_PATH = Path(__file__).resolve().parent.parent / "package.json"


def _package_plugin_api() -> str:
    package_json = json.loads(_PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
    return package_json["openclaw"]["compat"]["pluginApi"]


def test_capabilities_host_compat_matches_package_json() -> None:
    caps = capabilities.capabilities()
    package_range = _package_plugin_api()
    capabilities_range = caps.host_compat.get("openclaw", {}).get("pluginApi")
    assert capabilities_range == package_range, (
        f"host compat drift: package.json declares pluginApi={package_range!r} "
        f"but kb.capabilities.host_compat reports {capabilities_range!r}. "
        f"Keep both in sync."
    )


def test_capabilities_host_compat_present_and_nonempty() -> None:
    caps = capabilities.capabilities()
    assert "openclaw" in caps.host_compat
    assert "pluginApi" in caps.host_compat["openclaw"]
    assert caps.host_compat["openclaw"]["pluginApi"].strip() != ""


def test_load_host_compat_returns_empty_on_missing_package_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(capabilities, "_PACKAGE_JSON_PATH", tmp_path / "does-not-exist.json")
    assert capabilities._load_host_compat() == {}


def test_load_host_compat_returns_empty_on_malformed_package_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    bad = tmp_path / "package.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(capabilities, "_PACKAGE_JSON_PATH", bad)
    assert capabilities._load_host_compat() == {}
