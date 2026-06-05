"""Capabilities descriptor — must match the JSONL handler registration."""

from __future__ import annotations

from vouch import capabilities
from vouch.jsonl_server import DECISION_HANDLERS, HANDLERS


def test_default_capabilities_match_agent_jsonl_handlers() -> None:
    caps = capabilities.capabilities()
    declared = set(caps.methods)
    implemented = set(HANDLERS.keys()) - DECISION_HANDLERS
    assert declared == implemented, (
        f"capabilities/handlers mismatch: "
        f"missing handlers={declared - implemented}, "
        f"missing capabilities={implemented - declared}"
    )


def test_trusted_capabilities_include_decision_handlers() -> None:
    caps = capabilities.capabilities(include_decision_tools=True)
    assert set(caps.methods) == set(HANDLERS.keys())
