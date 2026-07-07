"""Capabilities descriptor — must match the JSONL handler registration."""

from __future__ import annotations

import json
from pathlib import Path

from vouch import capabilities
from vouch.jsonl_server import HANDLERS

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "openclaw.plugin.json"


def _manifest_plugin_api() -> str:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest["openclaw"]["compat"]["pluginApi"]


def test_capabilities_matches_jsonl_handlers() -> None:
    caps = capabilities.capabilities()
    declared = set(caps.methods)
    implemented = set(HANDLERS.keys())
    assert declared == implemented, (
        f"capabilities/handlers mismatch: "
        f"missing handlers={declared - implemented}, "
        f"missing capabilities={implemented - declared}"
    )


def test_capabilities_host_compat_matches_openclaw_manifest() -> None:
    caps = capabilities.capabilities()
    manifest_range = _manifest_plugin_api()
    assert caps.host_compat["openclaw"]["pluginApi"] == manifest_range


def test_capabilities_host_compat_present_and_nonempty() -> None:
    caps = capabilities.capabilities()
    assert "openclaw" in caps.host_compat
    assert caps.host_compat["openclaw"]["pluginApi"]
