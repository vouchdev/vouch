"""Frontmatter filters on page listings (page_filters + the three surfaces)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.models import Page, PageStatus
from vouch.page_filters import filter_pages, parse_kv
from vouch.storage import KBStore


def _page(pid: str, *, kind: str = "concept", **meta: object) -> Page:
    return Page(
        id=pid,
        title=pid.replace("-", " "),
        type=kind,
        status=PageStatus.ACTIVE,
        metadata=dict(meta),
    )


@pytest.fixture
def followups() -> list[Page]:
    return [
        _page(
            "ping-alice-example", kind="followup",
            due_at="2026-07-01", followup_status="open", owner="bob-example",
        ),
        _page(
            "renew-acme-example", kind="followup",
            due_at="2026-07-20", followup_status="open",
        ),
        _page(
            "ship-report", kind="followup",
            due_at="2026-06-01", followup_status="done",
        ),
        _page("acme-example", kind="org", website="https://acme.example"),
    ]


def test_filter_by_kind(followups: list[Page]) -> None:
    hits = filter_pages(followups, kind="followup")
    assert [p.id for p in hits] == ["ping-alice-example", "renew-acme-example", "ship-report"]


def test_filter_equality_and_missing_field_excludes(followups: list[Page]) -> None:
    hits = filter_pages(followups, kind="followup", equals={"owner": "bob-example"})
    assert [p.id for p in hits] == ["ping-alice-example"]
    # a page without the field never matches
    assert filter_pages(followups, equals={"nonexistent": "x"}) == []


def test_filter_date_bounds_are_inclusive(followups: list[Page]) -> None:
    due = filter_pages(
        followups, kind="followup",
        equals={"followup_status": "open"}, before={"due_at": "2026-07-01"},
    )
    assert [p.id for p in due] == ["ping-alice-example"]

    upcoming = filter_pages(followups, kind="followup", after={"due_at": "2026-07-01"})
    assert [p.id for p in upcoming] == ["ping-alice-example", "renew-acme-example"]


def test_filter_numeric_bounds() -> None:
    pages = [
        _page(f"p{i}", kind="project-record", record_status="active", budget=i)
        for i in (5, 30, 200)
    ]
    hits = filter_pages(pages, before={"budget": "30"})
    assert [p.id for p in hits] == ["p5", "p30"]


def test_parse_kv() -> None:
    assert parse_kv(("a=1", "b=x=y")) == {"a": "1", "b": "x=y"}
    with pytest.raises(ValueError):
        parse_kv(("noequals",))


def _seed_store(root: Path, pages: list[Page]) -> KBStore:
    store = KBStore.init(root)
    for p in pages:
        store.put_page(p)
    return store


def test_jsonl_list_pages_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, followups: list[Page],
) -> None:
    _seed_store(tmp_path, followups)
    monkeypatch.chdir(tmp_path)

    from vouch.jsonl_server import HANDLERS

    rows = HANDLERS["kb.list_pages"]({"type": "followup", "meta": {"followup_status": "open"}})
    assert sorted(r["id"] for r in rows) == ["ping-alice-example", "renew-acme-example"]

    rows = HANDLERS["kb.list_pages"]({"meta_before": {"due_at": "2026-06-30"}})
    assert [r["id"] for r in rows] == ["ship-report"]

    # no params -> unchanged full listing
    rows = HANDLERS["kb.list_pages"]({})
    assert len(rows) == 4


def test_mcp_list_pages_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, followups: list[Page],
) -> None:
    _seed_store(tmp_path, followups)
    monkeypatch.chdir(tmp_path)

    from vouch import server

    rows = server.kb_list_pages(type="followup", meta={"followup_status": "open"})
    assert sorted(r["id"] for r in rows) == ["ping-alice-example", "renew-acme-example"]
    assert all("metadata" in r for r in rows)


def test_cli_pages_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, followups: list[Page],
) -> None:
    _seed_store(tmp_path, followups)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["pages", "--kind", "followup", "--meta", "followup_status=open",
         "--before", "due_at=2026-07-31", "--json"],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert sorted(r["id"] for r in rows) == ["ping-alice-example", "renew-acme-example"]

    bad = CliRunner().invoke(cli, ["pages", "--meta", "malformed"])
    assert bad.exit_code != 0
