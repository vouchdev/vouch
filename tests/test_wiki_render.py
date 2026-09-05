"""Tests for `vouch.wiki_render` — the derived index/MOC/backlink render.

These are pure functions over the approved page set: regenerable views (like
the SQLite index), never gated writes. The tests pin the shape of the front
door — grouped index with summaries, alias/slug resolution, inbound backlinks,
and a map-of-content ranked by how referenced a page is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import wiki_render
from vouch.cli import cli
from vouch.jsonl_server import handle_request
from vouch.models import Page, PageStatus
from vouch.storage import KBStore


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


def test_a_real_title_beats_another_pages_alias() -> None:
    # Alpha claims "Beta" as an alias while Beta is actually titled that. With
    # titles and aliases indexed in a single pass per page, Alpha's alias landed
    # first purely because Alpha comes first in the list, so `[[Beta]]` resolved
    # to Alpha — and since a self-link is dropped, Beta lost the backlink too.
    a = _page("Alpha", body="see [[Beta]]", aliases=["Beta"], pid="alpha")
    b = _page("Beta", body="a standalone leaf page", pid="beta")
    pages = [a, b]

    assert wiki_render.resolve_link("Beta", pages) is b
    assert wiki_render.backlinks(pages).get("beta") == ["Alpha"]


def test_alias_still_resolves_when_no_title_claims_it() -> None:
    # The two-pass index must not cost aliases their resolution.
    a = _page("Alpha", body="builds on [[the beta]]", pid="alpha")
    b = _page("Beta", aliases=["the beta"], pid="beta")
    assert wiki_render.resolve_link("the beta", [a, b]) is b


def test_render_moc_ranks_by_inbound_links() -> None:
    a = _page("Alpha", body="see [[Gamma]]", pid="alpha")
    b = _page("Beta", body="see [[Gamma]]", pid="beta")
    g = _page("Gamma", body="a leaf", pid="gamma")
    out = wiki_render.render_moc([a, b, g])
    # Gamma has 2 inbound links; it must rank above the 0-inbound pages.
    assert out.index("Gamma") < out.index("Alpha")
    assert out.index("Gamma") < out.index("Beta")


# --- outbound_links / page_links (kb.backlinks) ---------------------------


def test_outbound_links_resolves_and_excludes_self() -> None:
    a = _page("Alpha", body="see [[Beta]] and also [[Alpha]] (self)", pid="alpha")
    b = _page("Beta", pid="beta")
    assert wiki_render.outbound_links(a, [a, b]) == ["Beta"]


def test_outbound_links_deduplicates_repeated_links() -> None:
    a = _page("Alpha", body="see [[Beta]] and again [[Beta]]", pid="alpha")
    b = _page("Beta", pid="beta")
    assert wiki_render.outbound_links(a, [a, b]) == ["Beta"]


def test_outbound_links_drops_unresolved() -> None:
    a = _page("Alpha", body="see [[Ghost]]", pid="alpha")
    assert wiki_render.outbound_links(a, [a]) == []


def test_page_links_combines_inbound_and_outbound() -> None:
    a = _page("Alpha", body="see [[Beta]]", pid="alpha")
    b = _page("Beta", body="see [[Gamma]]", pid="beta")
    g = _page("Gamma", pid="gamma")
    pages = [a, b, g]
    assert wiki_render.page_links(pages, "beta") == {
        "inbound": ["Alpha"],
        "outbound": ["Gamma"],
    }


def test_page_links_returns_none_for_unknown_page() -> None:
    a = _page("Alpha", pid="alpha")
    assert wiki_render.page_links([a], "nope") is None


# --- kb.backlinks (server/jsonl/cli registration) --------------------------


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _put(
    store: KBStore, pid: str, title: str, body: str,
    *, status: PageStatus = PageStatus.ACTIVE,
) -> None:
    store.put_page(Page(id=pid, title=title, body=body, status=status))


def test_jsonl_backlinks_single_page(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    _put(store, "alpha", "Alpha", "see [[Beta]] for more.")
    _put(store, "beta", "Beta", "a leaf page.")
    resp = handle_request(
        {"id": "b1", "method": "kb.backlinks", "params": {"page_id": "beta"}}
    )
    assert resp["ok"] is True
    assert resp["result"]["inbound"] == ["Alpha"]
    assert resp["result"]["outbound"] == []


def test_jsonl_backlinks_full_map_with_no_page_id(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _put(store, "alpha", "Alpha", "see [[Beta]] for more.")
    _put(store, "beta", "Beta", "a leaf page.")
    resp = handle_request({"id": "b2", "method": "kb.backlinks", "params": {}})
    assert resp["ok"] is True
    assert resp["result"]["backlinks"] == {"beta": ["Alpha"]}


def test_jsonl_backlinks_unknown_page_errors(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request(
        {"id": "b3", "method": "kb.backlinks", "params": {"page_id": "nope"}}
    )
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_request"


def test_jsonl_backlinks_excludes_archived_pages(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archived pages are out of the wiki front door (#695) — a link to one
    is exactly as dead as a link to nothing, so it's dropped from both the
    inbound map and treated as unresolved for outbound purposes."""
    monkeypatch.chdir(store.root)
    _put(store, "gone", "Gone", "retired content.", status=PageStatus.ARCHIVED)
    _put(store, "linker", "Linker", "see [[Gone]] for the old details.")
    resp = handle_request(
        {"id": "b4", "method": "kb.backlinks", "params": {"page_id": "linker"}}
    )
    assert resp["ok"] is True
    assert resp["result"]["outbound"] == []
    full = handle_request({"id": "b5", "method": "kb.backlinks", "params": {}})
    assert "gone" not in full["result"]["backlinks"]


def test_mcp_surface_serves_backlinks(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server

    _put(store, "alpha", "Alpha", "see [[Beta]] for more.")
    _put(store, "beta", "Beta", "a leaf page.")
    monkeypatch.setattr(server, "_store", lambda: store)

    single = server.kb_backlinks("beta")
    assert single["inbound"] == ["Alpha"]
    assert single["outbound"] == []

    full = server.kb_backlinks()
    assert full["backlinks"] == {"beta": ["Alpha"]}

    with pytest.raises(ValueError, match="not found"):
        server.kb_backlinks("nope")


def test_cli_backlinks_full_map(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    _put(store, "alpha", "Alpha", "see [[Beta]] for more.")
    _put(store, "beta", "Beta", "a leaf page.")
    runner = CliRunner()
    result = runner.invoke(cli, ["backlinks"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backlinks"] == {"beta": ["Alpha"]}


def test_cli_backlinks_single_page(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    _put(store, "alpha", "Alpha", "see [[Beta]] for more.")
    _put(store, "beta", "Beta", "a leaf page.")
    runner = CliRunner()
    result = runner.invoke(cli, ["backlinks", "beta"])
    assert result.exit_code == 0
    assert '"inbound"' in result.output
    assert "Alpha" in result.output


def test_cli_backlinks_unknown_page_errors(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    runner = CliRunner()
    result = runner.invoke(cli, ["backlinks", "nope"])
    assert result.exit_code != 0
