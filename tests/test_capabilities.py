"""Capabilities descriptor — must match the JSONL handler registration."""

from __future__ import annotations

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
