"""Filesystem scan for Claude Code skills and slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import KBStore
from .frontmatter import derive_description, parse_frontmatter


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    scope: str  # "project" | "user"
    kind: str   # "skill" | "command"
    path: str   # absolute path on disk


def _read_skill_file(
    path: Path, *, scope: str, kind: str, fallback_name: str,
) -> SkillRecord | None:
    """Parse one skill file. Returns None if the file is unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    name = meta.get("name") if isinstance(meta.get("name"), str) else None
    name = (name or fallback_name).strip()
    description = derive_description(meta, body)
    return SkillRecord(
        name=name,
        description=description,
        scope=scope,
        kind=kind,
        path=str(path),
    )


def _scan_dir(base: Path, *, scope: str) -> list[SkillRecord]:
    """Walk one catalogue root and yield records."""
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


def build_catalogue(store: KBStore) -> dict[str, SkillRecord]:
    """Build the merged catalogue. Project-local entries override user ones."""
    user_base = Path.home() / ".claude"
    project_base = store.root / ".claude"

    merged: dict[str, SkillRecord] = {}
    for rec in _scan_dir(user_base, scope="user"):
        merged[rec.name] = rec
    for rec in _scan_dir(project_base, scope="project"):
        merged[rec.name] = rec
    return merged


def list_catalogue(store: KBStore) -> list[dict[str, Any]]:
    """Catalogue every discoverable skill / slash command."""
    cat = build_catalogue(store)
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


def get_catalogue_entry(store: KBStore, name: str) -> dict[str, Any]:
    """Return the full markdown body of a named skill."""
    cat = build_catalogue(store)
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
