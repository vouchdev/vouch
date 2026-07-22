"""Derived wiki render: index, map-of-content, and backlinks over pages.

These artifacts are regenerable *views* over the approved page set — like the
SQLite index, not authored knowledge — so they never go through the review
gate. ``render_*`` are pure functions; the CLI (``vouch render-wiki``) writes
their output to a render target. Keeping them derived is what lets vouch match
the llm-wiki compiler's browsable front door (index + map-of-content +
backlinks) without opening an ungated write path.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .models import Page

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


def _link_index(pages: list[Page]) -> dict[str, Page]:
    """Map every resolvable name (title, id/slug, alias) to its page.

    Keys are lowercased. Earlier pages win a name collision (``setdefault``),
    so a later page's alias never shadows an existing page's title.
    """
    index: dict[str, Page] = {}
    for page in pages:
        for name in (page.title, page.id, *(page.metadata.get("aliases") or [])):
            key = str(name).strip().lower()
            if key:
                index.setdefault(key, page)
    return index


def resolve_link(target: str, pages: list[Page]) -> Page | None:
    """Resolve a ``[[target]]`` to a page by title, id/slug, or alias."""
    return _link_index(pages).get(target.strip().lower())


def backlinks(pages: list[Page]) -> dict[str, list[str]]:
    """Map page id to the sorted titles of pages that link to it.

    A page's link to itself is ignored. Links are resolved through the same
    title/slug/alias index used everywhere else, so an inbound link written as
    an alias still counts.
    """
    index = _link_index(pages)
    inbound: dict[str, set[str]] = defaultdict(set)
    for page in pages:
        for raw in _WIKILINK_RE.findall(page.body):
            target = index.get(raw.strip().lower())
            if target is not None and target.id != page.id:
                inbound[target.id].add(page.title)
    return {pid: sorted(titles) for pid, titles in inbound.items()}


def render_index(pages: list[Page]) -> str:
    """Render an index grouped by page type, each entry with its summary."""
    if not pages:
        return "# Knowledge Wiki\n\n_no approved pages yet._\n"
    by_type: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_type[page.type].append(page)
    lines = ["# Knowledge Wiki", ""]
    for ptype in sorted(by_type):
        lines.append(f"## {ptype}")
        for page in sorted(by_type[ptype], key=lambda p: p.title.lower()):
            summary = str(page.metadata.get("summary") or "").strip()
            suffix = f" — {summary}" if summary else ""
            lines.append(f"- [[{page.title}]]{suffix}")
        lines.append("")
    lines.append(f"_{len(pages)} page(s)_")
    return "\n".join(lines) + "\n"


def render_moc(pages: list[Page]) -> str:
    """Render a map-of-content ranked by how referenced each page is.

    The most linked-to pages surface first (hubs), each followed by its
    inbound links, so a reader sees the graph's centre before its leaves.
    """
    if not pages:
        return "# Map of Content\n\n_no approved pages yet._\n"
    inbound = backlinks(pages)
    ordered = sorted(
        pages,
        key=lambda p: (-len(inbound.get(p.id, [])), p.title.lower()),
    )
    lines = ["# Map of Content", ""]
    for page in ordered:
        refs = inbound.get(page.id, [])
        lines.append(f"- **[[{page.title}]]** ({len(refs)} inbound)")
        for title in refs:
            lines.append(f"  - ← [[{title}]]")
    lines.append("")
    return "\n".join(lines) + "\n"
