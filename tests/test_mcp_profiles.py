"""MCP tool profiles narrow the surface an agent sees (friendlier-mcp slice)."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from vouch import mcp_profiles
from vouch.capabilities import METHODS

_MINIMAL_TOOLS = {
    "kb_capabilities", "kb_status", "kb_context", "kb_search",
    "kb_read_page", "kb_propose_claim", "kb_propose_page", "kb_list_pending",
}


def _make(name: str):
    def fn(x: int = 0) -> int:
        return x
    fn.__name__ = name
    return fn


def _fresh_mcp() -> FastMCP:
    m = FastMCP("probe")
    for method in METHODS:
        m.tool()(_make("kb_" + method.split(".", 1)[1]))
    return m


def test_full_profile_removes_nothing() -> None:
    m = _fresh_mcp()
    removed = mcp_profiles.apply_tool_profile(m, "full")
    assert removed == []
    assert len(m._tool_manager._tools) == len(METHODS)


def test_minimal_exposes_core_only() -> None:
    m = _fresh_mcp()
    mcp_profiles.apply_tool_profile(m, "minimal")
    assert set(m._tool_manager._tools) == _MINIMAL_TOOLS


def test_standard_is_superset_of_minimal() -> None:
    assert mcp_profiles.PROFILES["minimal"] <= mcp_profiles.PROFILES["standard"]


def test_every_profile_is_subset_of_methods() -> None:
    allm = set(METHODS)
    for name, methods in mcp_profiles.PROFILES.items():
        assert methods <= allm, f"{name} references non-methods: {methods - allm}"
    assert mcp_profiles.PROFILES["full"] == allm


def test_resolve_env_beats_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_TOOL_PROFILE", "full")
    assert mcp_profiles.resolve_profile_name({"mcp": {"tool_profile": "minimal"}}) == "full"


def test_resolve_default_is_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOUCH_TOOL_PROFILE", raising=False)
    assert mcp_profiles.resolve_profile_name(None) == "minimal"


def test_unknown_profile_falls_back_to_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_TOOL_PROFILE", "bogus")
    assert mcp_profiles.resolve_profile_name(None) == "minimal"


def test_resolve_tolerates_non_dict_mcp_section(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare `mcp:` key (YAML -> None) or non-dict mcp section must degrade
    to the default, not crash `vouch serve`."""
    monkeypatch.delenv("VOUCH_TOOL_PROFILE", raising=False)
    assert mcp_profiles.resolve_profile_name({"mcp": None}) == "minimal"
    assert mcp_profiles.resolve_profile_name({"mcp": "oops"}) == "minimal"
    assert mcp_profiles.resolve_profile_name({"mcp": {}}) == "minimal"


def test_compact_descriptions_trims_to_first_line() -> None:
    m = FastMCP("probe")

    def kb_thing(x: int = 0) -> int:
        """First line.

        Second paragraph with lots of detail the agent does not need.
        """
        return x

    m.tool()(kb_thing)
    changed = mcp_profiles.compact_descriptions(m)
    assert changed == 1
    assert m._tool_manager._tools["kb_thing"].description == "First line."
