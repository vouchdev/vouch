"""Skill / slash-command discovery for agents.

Lets an MCP-connected agent (Claude Code, Cursor, …) introspect what
skills and slash commands are available in the current environment
without having to read the filesystem itself. The agent calls
``kb.list_skills`` to see what's installed, then ``kb.get_skill`` to
pull the body of one it wants to run.

Scanned locations, in priority order (later wins on name collision):

1. ``<kb_root>/.claude/skills/<name>/SKILL.md`` — project-local skills
2. ``<kb_root>/.claude/commands/<name>.md``     — project-local slash commands
3. ``~/.claude/skills/<name>/SKILL.md``         — user-global skills
4. ``~/.claude/commands/<name>.md``             — user-global slash commands

Project-local skills override user-global ones with the same name so a
KB can ship its own slash-command flavour that masks the user's default.

A skill description is parsed from YAML frontmatter when present
(``description:`` field, gbrain-style), otherwise from the first
markdown paragraph after the first heading. Discovery silently skips
unreadable files — the catalogue is best-effort.

Publishing the catalogue over MCP is gated by ``mcp.publish_skills`` in
``config.yaml`` (default ``true``). When flipped to ``false`` —
"company-brain" mode where the slash-command catalogue is itself
sensitive — ``list_skills`` returns an empty list and ``get_skill``
raises :class:`SkillsDisabledError`, which the MCP / JSONL layer maps to
a ``permission_denied`` error. The flag is read fresh on every call, so
toggling it takes effect without restarting the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .storage import KBStore

# Matches a YAML frontmatter block at the very top of a file.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
# After stripping frontmatter, find the first markdown heading.
_FIRST_HEADING_RE = re.compile(r"^#+\s+(.*?)\s*$", re.MULTILINE)
DESCRIPTION_PREVIEW_CHARS = 280


class SkillsDisabledError(RuntimeError):
    """Raised when the skill catalogue is requested while it's gated off.

    Surfaced when ``mcp.publish_skills`` is ``false`` in ``config.yaml``.
    The JSONL / MCP layer translates this into a ``permission_denied``
    error rather than a generic failure.
    """


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    scope: str  # "project" | "user"
    kind: str   # "skill" | "command"
    path: str   # absolute path on disk


def _load_cfg(store: KBStore) -> dict[str, Any]:
    """Read ``config.yaml`` defensively — a missing or malformed file is
    treated as an empty config (default-on behaviour)."""
    try:
        loaded = yaml.safe_load((store.kb_dir / "config.yaml").read_text())
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def publish_skills_enabled(store: KBStore) -> bool:
    """Whether the skill catalogue may be published over MCP.

    Controlled by ``mcp.publish_skills`` in ``config.yaml``. Defaults to
    ``True`` so existing KBs with no ``mcp:`` block keep the catalogue.
    Only an explicit ``false`` turns the gate off — any other value (or a
    missing key) is treated as enabled.
    """
    mcp = _load_cfg(store).get("mcp")
    if not isinstance(mcp, dict):
        return True
    return mcp.get("publish_skills", True) is not False


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
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


def _derive_description(meta: dict[str, Any], body: str) -> str:
    """Prefer explicit ``description:`` frontmatter; fall back to the first
    paragraph of body text after the first heading."""
    desc = meta.get("description")
    if isinstance(desc, str) and desc.strip():
        flat = " ".join(desc.strip().split())
        return flat[:DESCRIPTION_PREVIEW_CHARS]

    # Drop the first heading line, then take the first non-empty paragraph.
    stripped = _FIRST_HEADING_RE.sub("", body, count=1).strip()
    paragraphs = [p.strip() for p in stripped.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""
    flat = " ".join(paragraphs[0].split())
    if len(flat) <= DESCRIPTION_PREVIEW_CHARS:
        return flat
    return flat[: DESCRIPTION_PREVIEW_CHARS - 1] + "…"


def _read_skill_file(
    path: Path, *, scope: str, kind: str, fallback_name: str,
) -> SkillRecord | None:
    """Parse one skill file. Returns None if the file is unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") if isinstance(meta.get("name"), str) else None
    name = (name or fallback_name).strip()
    description = _derive_description(meta, body)
    return SkillRecord(
        name=name,
        description=description,
        scope=scope,
        kind=kind,
        path=str(path),
    )


def _scan_dir(
    base: Path, *, scope: str,
) -> list[SkillRecord]:
    """Walk one of the four catalogue roots and yield records."""
    records: list[SkillRecord] = []
    skills_root = base / "skills"
    if skills_root.is_dir():
        for sub in sorted(skills_root.iterdir()):
            skill_md = sub / "SKILL.md"
            if skill_md.is_file():
                rec = _read_skill_file(
                    skill_md, scope=scope, kind="skill", fallback_name=sub.name,
                )
                if rec is not None:
                    records.append(rec)

    commands_root = base / "commands"
    if commands_root.is_dir():
        for md in sorted(commands_root.glob("*.md")):
            rec = _read_skill_file(
                md, scope=scope, kind="command", fallback_name=md.stem,
            )
            if rec is not None:
                records.append(rec)

    return records


def _catalogue(store: KBStore) -> dict[str, SkillRecord]:
    """Build the merged catalogue. Project-local entries override user ones."""
    # User-global first, then project — project overwrites on collision.
    user_base = Path.home() / ".claude"
    project_base = store.root / ".claude"

    merged: dict[str, SkillRecord] = {}
    for rec in _scan_dir(user_base, scope="user"):
        merged[rec.name] = rec
    for rec in _scan_dir(project_base, scope="project"):
        merged[rec.name] = rec
    return merged


def list_skills(store: KBStore) -> list[dict[str, Any]]:
    """Catalogue every discoverable skill / slash command.

    Returns one row per skill: ``{name, description, scope, kind, path}``.
    Sorted by name for stable output. Returns an empty list when
    ``mcp.publish_skills`` is ``false`` — the catalogue is hidden without
    erroring so a polling client just sees "nothing installed".
    """
    if not publish_skills_enabled(store):
        return []
    cat = _catalogue(store)
    return [
        {
            "name": r.name,
            "description": r.description,
            "scope": r.scope,
            "kind": r.kind,
            "path": r.path,
        }
        for r in sorted(cat.values(), key=lambda r: r.name)
    ]


def get_skill(store: KBStore, name: str) -> dict[str, Any]:
    """Return the full markdown body of a named skill.

    Raises :class:`SkillsDisabledError` when ``mcp.publish_skills`` is
    ``false`` (mapped to ``permission_denied`` by the transport), and
    ``KeyError`` if the name isn't in the catalogue (mapped to a clean
    ``not_found`` error).
    """
    if not publish_skills_enabled(store):
        raise SkillsDisabledError(
            "skill catalogue is disabled (mcp.publish_skills: false)"
        )
    cat = _catalogue(store)
    rec = cat.get(name)
    if rec is None:
        raise KeyError(f"unknown skill: {name!r}")
    try:
        body = Path(rec.path).read_text(encoding="utf-8")
    except OSError as e:
        raise KeyError(f"could not read skill {name!r}: {e}") from e
    return {
        "name": rec.name,
        "description": rec.description,
        "scope": rec.scope,
        "kind": rec.kind,
        "path": rec.path,
        "body": body,
    }
