"""MCP tool profiles — narrow the tool surface an agent sees by default.

vouch exposes 58 kb.* methods. Handing all of them to an agent every turn is
the main first-touch friendliness cost (the closest competitor, pmb, exposes
~10 by default and hides the rest behind a profile flag). Profiles control
*exposure* only: the JSONL and CLI surfaces, the protocol method list
(capabilities.METHODS), and the review gate are unchanged.

Resolution order: VOUCH_TOOL_PROFILE env var > config.yaml `mcp.tool_profile`
> "minimal" (the default). Unknown names fall back to "minimal".
"""

from __future__ import annotations

import os
from typing import Any

from .capabilities import METHODS

_MINIMAL: frozenset[str] = frozenset({
    "kb.capabilities",
    "kb.status",
    "kb.context",
    "kb.search",
    "kb.read_page",
    "kb.propose_claim",
    "kb.propose_page",
    "kb.list_pending",
})

_STANDARD: frozenset[str] = _MINIMAL | frozenset({
    "kb.approve",
    "kb.reject",
    "kb.supersede",
    "kb.contradict",
    "kb.confirm",
    "kb.read_claim",
    "kb.list_claims",
    "kb.neighbors",
    "kb.why",
})

PROFILES: dict[str, frozenset[str]] = {
    "minimal": _MINIMAL,
    "standard": _STANDARD,
    "full": frozenset(METHODS),
}

DEFAULT_PROFILE = "minimal"


def _tool_name(method: str) -> str:
    """`"kb.propose_claim"` -> `"kb_propose_claim"` (MCP tool names use `_`)."""
    return "kb_" + method.split(".", 1)[1]


def resolve_profile_name(config: dict[str, Any] | None = None) -> str:
    """Pick the active profile from env > config > default."""
    raw = os.environ.get("VOUCH_TOOL_PROFILE")
    if not raw and isinstance(config, dict):
        mcp_cfg = config.get("mcp")
        if isinstance(mcp_cfg, dict):
            raw = mcp_cfg.get("tool_profile")
    name = str(raw).strip().lower() if raw else DEFAULT_PROFILE
    return name if name in PROFILES else DEFAULT_PROFILE


def tool_names_for(name: str) -> set[str]:
    """The MCP (underscore) tool names exposed by profile `name`."""
    return {_tool_name(m) for m in PROFILES.get(name, PROFILES[DEFAULT_PROFILE])}


def apply_tool_profile(mcp: Any, name: str) -> list[str]:
    """Remove every registered `kb_*` MCP tool not in profile `name`.

    Returns the sorted list of removed tool names. Idempotent. `full` removes
    nothing. Only `kb_*` tools are touched, so trust/diagnostic tools
    registered elsewhere are never dropped.
    """
    keep = tool_names_for(name)
    tools = mcp._tool_manager._tools
    removed: list[str] = []
    for tool_name in list(tools.keys()):
        if tool_name.startswith("kb_") and tool_name not in keep:
            tools.pop(tool_name, None)
            removed.append(tool_name)
    return sorted(removed)


def compact_descriptions(mcp: Any) -> int:
    """Trim each kb_ tool's description to its first line to save context.

    Full docstrings are only needed under the `full` profile; the first line
    is enough for an agent choosing a tool. Returns the number changed.
    """
    changed = 0
    for tool in mcp._tool_manager._tools.values():
        if not tool.name.startswith("kb_"):
            continue
        desc = tool.description or ""
        first = desc.strip().split("\n", 1)[0].strip()
        if first and first != desc:
            try:
                tool.description = first
            except (AttributeError, TypeError):
                # pydantic frozen-model fallback
                object.__setattr__(tool, "description", first)
            changed += 1
    return changed
