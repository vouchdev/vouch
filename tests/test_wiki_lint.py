"""kb.wiki_lint - page-health sweep (roadmap 1.4: orphan pages, dead
wikilinks, stale pages, thin citation coverage).

Page-level sibling of health.lint()'s claim-health sweep, reusing
wiki_render's link index/backlinks and compile's citation-coverage walk
against the live, already-approved page set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vouch.jsonl_server import handle_request
from vouch.models import Page, PageStatus
from vouch.storage import KBStore
from vouch.wiki_lint import wiki_lint


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _page(
    store: KBStore,
    pid: str,
    title: str,
    body: str,
    *,
    status: PageStatus = PageStatus.ACTIVE,
    updated_at: datetime | None = None,
) -> None:
    kwargs: dict = {}
    if updated_at is not None:
        kwargs["created_at"] = updated_at
        kwargs["updated_at"] = updated_at
    store.put_page(Page(id=pid, title=title, body=body, status=status, **kwargs))


def test_orphan_page_flagged_when_nothing_links_to_it(store: KBStore) -> None:
    _page(store, "lonely", "Lonely", "# Lonely\n\nnothing links here.")
    report = wiki_lint(store)
    codes = {(f.code, tuple(f.object_ids)) for f in report.findings}
    assert ("orphan_page", ("lonely",)) in codes


def test_page_with_inbound_link_is_not_orphan(store: KBStore) -> None:
    _page(store, "target", "Target", "# Target\n\n[claim: c1] a cited fact here for real.")
    _page(store, "linker", "Linker", "# Linker\n\nsee [[Target]] for the real details on this.")
    report = wiki_lint(store)
    object_ids_flagged_orphan = {
        oid for f in report.findings if f.code == "orphan_page" for oid in f.object_ids
    }
    assert "target" not in object_ids_flagged_orphan


def test_dead_wikilink_flagged_and_deduplicated(store: KBStore) -> None:
    _page(
        store, "broken", "Broken",
        "# Broken\n\nsee [[Ghost Page]] and also [[Ghost Page]] again for details.",
    )
    report = wiki_lint(store)
    dead = [f for f in report.findings if f.code == "dead_wikilink"]
    assert len(dead) == 1  # deduplicated per (page, target)
    assert "Ghost Page" in dead[0].message
    assert dead[0].object_ids == ["broken"]


def test_wikilink_to_live_page_is_not_dead(store: KBStore) -> None:
    _page(store, "target", "Target", "# Target\n\nsome real content with a claim [claim: c1].")
    _page(store, "linker", "Linker", "# Linker\n\nrefers to [[Target]] for background reading.")
    report = wiki_lint(store)
    dead = [f for f in report.findings if f.code == "dead_wikilink"]
    assert dead == []


def test_wikilink_to_archived_page_is_dead(store: KBStore) -> None:
    """Archived pages are out of the wiki front door (#695) — a link to one
    is exactly as dead as a link to nothing, matching render-wiki's own
    archived-exclusion policy."""
    _page(store, "gone", "Gone", "# Gone\n\nretired", status=PageStatus.ARCHIVED)
    _page(store, "linker", "Linker", "# Linker\n\nsee [[Gone]] for the old details on this.")
    report = wiki_lint(store)
    dead = [f for f in report.findings if f.code == "dead_wikilink"]
    assert len(dead) == 1
    assert dead[0].object_ids == ["linker"]


def test_archived_pages_excluded_from_all_checks(store: KBStore) -> None:
    old_ts = datetime.now(UTC) - timedelta(days=9999)
    _page(
        store, "gone", "Gone",
        "# Gone\n\nsee [[Nonexistent]] no citations here at all really.",
        status=PageStatus.ARCHIVED, updated_at=old_ts,
    )
    report = wiki_lint(store)
    assert report.findings == []
    assert report.counts["pages_checked"] == 0


def test_stale_page_flagged_past_threshold(store: KBStore) -> None:
    old_ts = datetime.now(UTC) - timedelta(days=400)
    _page(store, "ancient", "Ancient", "# Ancient\n\nold content.", updated_at=old_ts)
    report = wiki_lint(store, stale_after_days=180)
    codes = {(f.code, tuple(f.object_ids)) for f in report.findings}
    assert ("stale_page", ("ancient",)) in codes


def test_stale_page_check_handles_naive_datetime(store: KBStore) -> None:
    """A page's updated_at can be timezone-naive (e.g. hand-written YAML, or
    an older on-disk page predating a tz-aware write path) — the staleness
    check must normalize it to UTC rather than raising on the naive/aware
    comparison."""
    naive_old = datetime.now() - timedelta(days=400)  # no tzinfo
    _page(store, "ancient", "Ancient", "# Ancient\n\nold content.", updated_at=naive_old)
    report = wiki_lint(store, stale_after_days=180)
    codes = {(f.code, tuple(f.object_ids)) for f in report.findings}
    assert ("stale_page", ("ancient",)) in codes


def test_recently_updated_page_is_not_stale(store: KBStore) -> None:
    recent = datetime.now(UTC) - timedelta(days=5)
    _page(store, "fresh", "Fresh", "# Fresh\n\nnew content.", updated_at=recent)
    report = wiki_lint(store, stale_after_days=180)
    stale = [f for f in report.findings if f.code == "stale_page"]
    assert stale == []


def test_uncited_section_flagged_below_threshold(store: KBStore) -> None:
    _page(
        store, "thin", "Thin",
        "# Thin\n\nThis page makes several substantive claims with real content "
        "but never once cites any evidence for any of them at all.",
    )
    report = wiki_lint(store, min_citation_coverage=0.5)
    codes = {(f.code, tuple(f.object_ids)) for f in report.findings}
    assert ("uncited_section", ("thin",)) in codes


def test_well_cited_page_is_not_flagged(store: KBStore) -> None:
    _page(
        store, "solid", "Solid",
        "# Solid\n\n[claim: c1] a well cited substantive sentence about the topic.",
    )
    report = wiki_lint(store, min_citation_coverage=0.5)
    uncited = [f for f in report.findings if f.code == "uncited_section"]
    assert uncited == []


def test_clean_kb_has_no_findings(store: KBStore) -> None:
    report = wiki_lint(store)
    assert report.findings == []
    assert report.ok is True
    assert report.counts == {"pages_checked": 0}


def test_ok_is_true_even_with_findings(store: KBStore) -> None:
    """None of wiki_lint's checks are data-integrity errors (unlike
    health.lint's broken_citation) — they're quality signals, so `ok` stays
    true even when findings exist."""
    _page(store, "lonely", "Lonely", "# Lonely\n\nno links, no citations really at all.")
    report = wiki_lint(store)
    assert report.findings != []
    assert report.ok is True


def test_jsonl_wiki_lint_envelope_success(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    _page(store, "lonely", "Lonely", "# Lonely\n\nno links here at all in this page.")
    resp = handle_request({"id": "w1", "method": "kb.wiki_lint", "params": {}})
    assert resp["id"] == "w1"
    assert resp["ok"] is True
    codes = {f["code"] for f in resp["result"]["findings"]}
    assert "orphan_page" in codes


def test_jsonl_wiki_lint_honors_stale_days_param(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    recent = datetime.now(UTC) - timedelta(days=10)
    _page(store, "fresh", "Fresh", "# Fresh\n\nnew.", updated_at=recent)
    resp = handle_request(
        {"id": "w2", "method": "kb.wiki_lint", "params": {"stale_days": 5}}
    )
    codes = {f["code"] for f in resp["result"]["findings"]}
    assert "stale_page" in codes


def test_mcp_surface_serves_wiki_lint(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server

    _page(store, "lonely", "Lonely", "# Lonely\n\nno links here at all in this page.")
    monkeypatch.setattr(server, "_store", lambda: store)

    result = server.kb_wiki_lint()
    direct = wiki_lint(store)
    assert result["ok"] == direct.ok
    assert {f["code"] for f in result["findings"]} == {f.code for f in direct.findings}
    assert result["counts"] == direct.counts


def test_cli_wiki_lint(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    monkeypatch.chdir(store.root)
    _page(store, "lonely", "Lonely", "# Lonely\n\nno links here at all in this page.")
    runner = CliRunner()
    result = runner.invoke(cli, ["wiki-lint"])
    assert result.exit_code == 0, result.output
    assert "orphan_page" in result.output


def test_cli_wiki_lint_clean_kb(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    monkeypatch.chdir(store.root)
    runner = CliRunner()
    result = runner.invoke(cli, ["wiki-lint"])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output
