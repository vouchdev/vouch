"""MCP transport settings read from ``config.yaml``.

Mirrors gbrain's ``mcp.publish_skills`` gate (issue #235): when false, skill
catalogue endpoints hide the catalogue so a company-brain KB can keep slash
commands on disk without advertising them to every MCP caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from .storage import KBStore


@dataclass(frozen=True)
class McpConfig:
    publish_skills: bool = True

    def to_capabilities_dict(self) -> dict[str, Any]:
        return {"publish_skills": self.publish_skills}


def load_config(store: KBStore) -> McpConfig:
    """Read ``mcp:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
    except (OSError, yaml.YAMLError):
        return McpConfig()
    if not isinstance(loaded, dict):
        return McpConfig()
    raw = loaded.get("mcp")
    if not isinstance(raw, dict):
        return McpConfig()
    publish = raw.get("publish_skills", True)
    if not isinstance(publish, bool):
        publish = bool(publish)
    return McpConfig(publish_skills=publish)
