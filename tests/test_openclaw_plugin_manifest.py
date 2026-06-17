"""openclaw.plugin.json contract checks (#228)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch.openclaw.context_engine import ENGINE_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "openclaw.plugin.json"
EXTENSION_PATH = REPO_ROOT / "adapters" / "openclaw" / "vouch-context-engine.mjs"


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_is_valid_json() -> None:
    json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_declares_vouch_context_engine(manifest: dict) -> None:
    engines = manifest.get("contracts", {}).get("contextEngines") or []
    assert ENGINE_ID in engines


def test_manifest_extension_entry_exists(manifest: dict) -> None:
    extensions = manifest.get("openclaw", {}).get("extensions") or []
    assert extensions, "openclaw.extensions must list the context engine entry"
    rel = extensions[0]
    assert (REPO_ROOT / rel).is_file()


def test_extension_file_exports_engine_id() -> None:
    text = EXTENSION_PATH.read_text(encoding="utf-8")
    assert "vouch-context" in text
    assert "registerContextEngine" in text


def test_manifest_mcp_and_context_contracts(manifest: dict) -> None:
    contracts = manifest.get("contracts") or {}
    assert "reviewGatedKB" in contracts
    assert "mcpMethods" in contracts
    assert "kb.context" in contracts["mcpMethods"]


def test_manifest_openclaw_compat_floor(manifest: dict) -> None:
    compat = manifest.get("openclaw", {}).get("compat") or {}
    assert compat.get("pluginApi")


def test_manifest_trust_boundary(manifest: dict) -> None:
    tb = manifest.get("openclaw", {}).get("trust_boundary") or {}
    assert tb.get("write_tools_review_gated") is True
    assert tb.get("remote_callers_filesystem") == "confined"
