"""YAML frontmatter and description extraction for SKILL.md files."""

from __future__ import annotations

import re
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_FIRST_HEADING_RE = re.compile(r"^#+\s+(.*?)\s*$", re.MULTILINE)
DESCRIPTION_PREVIEW_CHARS = 280


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    return meta, m.group(2)


def derive_description(meta: dict[str, Any], body: str) -> str:
    """Prefer explicit ``description:`` frontmatter; fall back to the first
    paragraph of body text after the first heading."""
    desc = meta.get("description")
    if isinstance(desc, str) and desc.strip():
        flat = " ".join(desc.strip().split())
        return flat[:DESCRIPTION_PREVIEW_CHARS]

    stripped = _FIRST_HEADING_RE.sub("", body, count=1).strip()
    paragraphs = [p.strip() for p in stripped.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""
    flat = " ".join(paragraphs[0].split())
    if len(flat) <= DESCRIPTION_PREVIEW_CHARS:
        return flat
    return flat[: DESCRIPTION_PREVIEW_CHARS - 1] + "…"
