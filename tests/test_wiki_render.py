"""Tests for `vouch.wiki_render` — the derived index/MOC/backlink render.

These are pure functions over the approved page set: regenerable views (like
the SQLite index), never gated writes. The tests pin the shape of the front
door — grouped index with summaries, alias/slug resolution, inbound backlinks,
and a map-of-content ranked by how referenced a page is.
"""

from __future__ import annotations

from vouch import wiki_render
from vouch.models import Page


def _page(
    title: str,
    *,
    body: str = "",
    ptype: str = "concept",
    summary: str = "",
    aliases: list[str] | None = None,
    pid: str | None = None,
) -> Page:
    meta: dict[str, object] = {}
    if summary:
        meta["summary"] = summary
    if aliases:
        meta["aliases"] = aliases
    return Page(
        id=pid or title.lower().replace(" ", "-"),
        title=title,
        body=body,
        type=ptype,
        metadata=meta,
    )


def test_render_index_groups_by_type_with_summaries() -> None:
    pages = [
        _page("Retry Policy", ptype="concept", summary="retries cap at three"),
        _page("Ship Flow", ptype="workflow", summary="how to ship a release"),
    ]
    out = wiki_render.render_index(pages)
    assert "[[Retry Policy]] — retries cap at three" in out
    assert "[[Ship Flow]] — how to ship a release" in out
    assert "concept" in out.lower()
    assert "workflow" in out.lower()


def test_render_index_empty_is_safe() -> None:
    out = wiki_render.render_index([])
    assert "no approved pages" in out.lower()


def test_resolve_link_matches_title_slug_and_alias() -> None:
    p = _page("Retry Policy", aliases=["backoff cap"], pid="retry-policy")
    pages = [p]
    assert wiki_render.resolve_link("Retry Policy", pages) is p
    assert wiki_render.resolve_link("retry-policy", pages) is p
    assert wiki_render.resolve_link("backoff cap", pages) is p
    assert wiki_render.resolve_link("nope", pages) is None


def test_backlinks_are_inbound_and_exclude_self() -> None:
    a = _page("Alpha", body="see [[Beta]] for more", pid="alpha")
    b = _page("Beta", body="a standalone leaf page", pid="beta")
    bl = wiki_render.backlinks([a, b])
    assert bl.get("beta") == ["Alpha"]
    assert "alpha" not in bl  # nothing links to Alpha


def test_backlinks_resolve_through_aliases() -> None:
    a = _page("Alpha", body="builds on [[the beta]]", pid="alpha")
    b = _page("Beta", aliases=["the beta"], pid="beta")
    bl = wiki_render.backlinks([a, b])
    assert bl.get("beta") == ["Alpha"]


def test_render_moc_ranks_by_inbound_links() -> None:
    a = _page("Alpha", body="see [[Gamma]]", pid="alpha")
    b = _page("Beta", body="see [[Gamma]]", pid="beta")
    g = _page("Gamma", body="a leaf", pid="gamma")
    out = wiki_render.render_moc([a, b, g])
    # Gamma has 2 inbound links; it must rank above the 0-inbound pages.
    assert out.index("Gamma") < out.index("Alpha")
    assert out.index("Gamma") < out.index("Beta")


def test_title_beats_an_earlier_pages_alias_regardless_of_order() -> None:
    # A real page is TITLED "Retry Policy"; an unrelated page carries the same
    # string only as an alias and sorts earlier in the list. Resolution must
    # follow the title, not the list order — the module docstring promises an
    # alias never shadows an existing page's title.
    aliaser = _page("Backoff Notes", aliases=["Retry Policy"], pid="backoff-notes")
    titled = _page("Retry Policy", pid="retry-policy")
    pages = [aliaser, titled]

    resolved = wiki_render.resolve_link("Retry Policy", pages)
    assert resolved is titled

    # …and the backlink for a [[Retry Policy]] reference lands on the titled
    # page, not the alias holder.
    caller = _page("Caller", body="we follow [[Retry Policy]] here", pid="caller")
    bl = wiki_render.backlinks([aliaser, titled, caller])
    assert bl.get("retry-policy") == ["Caller"]
    assert "backoff-notes" not in bl
