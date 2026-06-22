"""Publish gate for skill catalogue endpoints (``mcp.publish_skills``)."""

from __future__ import annotations

from typing import Any

from ..mcp_config import load_config
from ..storage import KBStore
from .catalogue import get_catalogue_entry, list_catalogue
from .errors import SkillsAccessDenied

_GATE_MESSAGE = (
    "skill catalogue is not published "
    "(set mcp.publish_skills: true in config.yaml to enable)"
)


def list_skills(store: KBStore) -> list[dict[str, Any]]:
    """Return the skill catalogue, or ``[]`` when publishing is disabled."""
    if not load_config(store).publish_skills:
        return []
    return list_catalogue(store)


def get_skill(store: KBStore, name: str) -> dict[str, Any]:
    """Return one skill body, or raise when publishing is disabled."""
    if not load_config(store).publish_skills:
        raise SkillsAccessDenied(_GATE_MESSAGE)
    return get_catalogue_entry(store, name)
