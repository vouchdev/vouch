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

    Keys are lowercased. Titles and slugs are indexed before any alias, so a
    page's real name always beats another page's nickname for it — inserting
    both in one pass per page made that depend on list order, letting an
    earlier page's alias shadow a later page's actual title. Within each pass
    earlier pages still win (``setdefault``).
    """
    index: dict[str, Page] = {}

    def add(name: object, page: Page) -> None:
        key = str(name).strip().lower()
        if key:
            index.setdefault(key, page)

    for page in pages:
        add(page.title, page)
        add(page.id, page)
    for page in pages:
        for alias in page.metadata.get("aliases") or []:
            add(alias, page)
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


def outbound_links(page: Page, pages: list[Page]) -> list[str]:
    """Titles of pages ``page``'s body links to, resolved and deduplicated.

    Self-links are dropped, matching ``backlinks()``'s own exclusion. Order
    is first-occurrence in the body text, not sorted - ``backlinks()`` sorts
    because it aggregates across many source pages, but outbound is already
    one page's own authored order.
    """
    index = _link_index(pages)
    seen: set[str] = set()
    out: list[str] = []
    for raw in _WIKILINK_RE.findall(page.body):
        target = index.get(raw.strip().lower())
        if target is not None and target.id != page.id and target.id not in seen:
            seen.add(target.id)
            out.append(target.title)
    return out


def page_links(pages: list[Page], page_id: str) -> dict[str, list[str]] | None:
    """Inbound + outbound wikilink titles for one page.

    ``None`` if ``page_id`` doesn't match any page in ``pages`` - the caller
    decides how to report that (``kb.backlinks`` raises, matching
    ``kb.neighbors``' contract for an unknown root node).
    """
    page = next((p for p in pages if p.id == page_id), None)
    if page is None:
        return None
    return {
        "inbound": backlinks(pages).get(page_id, []),
        "outbound": outbound_links(page, pages),
    }


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
