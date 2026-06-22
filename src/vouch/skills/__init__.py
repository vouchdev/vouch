"""Skill / slash-command discovery for agents.

Lets an MCP-connected agent introspect installed skills and slash commands
without reading the filesystem. Scanned locations, in priority order (later
wins on name collision):

1. ``<kb_root>/.claude/skills/<name>/SKILL.md`` — project-local skills
2. ``<kb_root>/.claude/commands/<name>.md``     — project-local commands
3. ``~/.claude/skills/<name>/SKILL.md``         — user-global skills
4. ``~/.claude/commands/<name>.md``             — user-global commands

Project-local entries override user-global ones with the same name.
"""

from __future__ import annotations

from .access import get_skill, list_skills
from .errors import SkillsAccessDenied

__all__ = ["SkillsAccessDenied", "get_skill", "list_skills"]
