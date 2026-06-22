"""Skill discovery errors."""

from __future__ import annotations


class SkillsAccessDenied(PermissionError):
    """Raised when ``mcp.publish_skills`` is false."""
