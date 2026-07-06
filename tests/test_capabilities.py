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


# --- host_compat drift detection (#237) -----------------------------------
#
# vouch declares openclaw.compat.pluginApi in openclaw.plugin.json. The same
# value must surface in kb.capabilities so non-OpenClaw clients can detect
# compat without parsing the manifest. These tests fail CI with a clear
# message if the two declarations drift apart.

_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "openclaw.plugin.json"
)


def _manifest_plugin_api() -> str:
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest["openclaw"]["compat"]["pluginApi"]


def test_capabilities_host_compat_matches_openclaw_manifest() -> None:
    """kb.capabilities.host_compat.openclaw.pluginApi must equal the
    pluginApi range declared in openclaw.plugin.json. A bump in one file
    without the other is exactly the "host compat drift" #237 asks CI to
    catch."""
    caps = capabilities.capabilities()
    manifest_range = _manifest_plugin_api()
    capabilities_range = caps.host_compat.get("openclaw", {}).get("pluginApi")
    assert capabilities_range == manifest_range, (
        f"host compat drift: openclaw.plugin.json declares pluginApi="
        f"{manifest_range!r} but kb.capabilities.host_compat reports "
        f"{capabilities_range!r}. Keep both in sync."
    )


def test_capabilities_host_compat_present_and_nonempty() -> None:
    """host_compat must not silently degrade to {} when the manifest is
    readable -- that would defeat the drift check above by making both
    sides agree on "missing" rather than catching real drift."""
    caps = capabilities.capabilities()
    assert "openclaw" in caps.host_compat
    assert "pluginApi" in caps.host_compat["openclaw"]
    assert caps.host_compat["openclaw"]["pluginApi"].strip() != ""


def test_load_host_compat_returns_empty_on_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """_load_host_compat must degrade gracefully (empty dict, no raise) if
    the manifest is absent -- e.g. installed as a standalone wheel without
    openclaw.plugin.json packaged alongside it."""
    monkeypatch.setattr(
        capabilities, "_PLUGIN_MANIFEST_PATH", tmp_path / "does-not-exist.json"
    )
    assert capabilities._load_host_compat() == {}


def test_load_host_compat_returns_empty_on_malformed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A malformed manifest must not crash capabilities() -- it's reporting
    diagnostic info, not validating the install."""
    bad = tmp_path / "openclaw.plugin.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(capabilities, "_PLUGIN_MANIFEST_PATH", bad)
    assert capabilities._load_host_compat() == {}
