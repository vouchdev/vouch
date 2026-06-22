"""Cursor pagination for kb.list_* methods (#245).

Covers the storage-layer paged variants (list_*_page), the JSONL handler
envelope shape, and the fuzz test the issue asks for: walking 1 000
artifacts page-by-page must return every id exactly once with no
duplicates and no gaps.
"""

from __future__ import annotations

import random
import string
from pathlib import Path

from vouch.models import (
    Claim,
    Entity,
    EntityType,
    Page,
    PageType,
    ProposalStatus,
    Relation,
    RelationType,
)
from vouch.storage import KBStore

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path / "kb")


def _src(store: KBStore, tag: str = "s") -> str:
    return store.put_source(tag.encode(), title=tag).id


def _claim(store: KBStore, cid: str, src_id: str) -> Claim:
    c = Claim(id=cid, text=f"claim {cid}", evidence=[src_id])
    store.put_claim(c)
    return c


def _page(store: KBStore, pid: str, src_id: str) -> Page:
    p = Page(id=pid, title=f"page {pid}", body="body", type=PageType.CONCEPT,
             sources=[src_id])
    store.put_page(p)
    return p


def _entity(store: KBStore, eid: str) -> Entity:
    e = Entity(id=eid, name=eid, type=EntityType.CONCEPT)
    store.put_entity(e)
    return e


def _relation(store: KBStore, src_id: str, tgt_id: str, rid: str) -> Relation:
    r = Relation(id=rid, source=src_id, relation=RelationType.USES, target=tgt_id)
    store.put_relation(r)
    return r


# ---------------------------------------------------------------------------
# basic pagination contract
# ---------------------------------------------------------------------------


def test_list_claims_page_returns_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(5):
        _claim(store, f"c-{i:03}", sid)
    items, next_cursor, total = store.list_claims_page(limit=3)
    assert total == 5
    assert len(items) == 3
    assert next_cursor is not None
    # Second page
    items2, next_cursor2, total2 = store.list_claims_page(limit=3, cursor=next_cursor)
    assert total2 == 5
    assert len(items2) == 2
    assert next_cursor2 is None


def test_list_pages_page_returns_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(4):
        _page(store, f"pg-{i:03}", sid)
    items, next_cursor, total = store.list_pages_page(limit=2)
    assert total == 4
    assert len(items) == 2
    assert next_cursor is not None
    items2, next_cursor2, _ = store.list_pages_page(limit=2, cursor=next_cursor)
    assert len(items2) == 2
    assert next_cursor2 is None


def test_list_entities_page_returns_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(6):
        _entity(store, f"e-{i:03}")
    items, next_cursor, total = store.list_entities_page(limit=4)
    assert total == 6
    assert len(items) == 4
    items2, nc2, _ = store.list_entities_page(limit=4, cursor=next_cursor)
    assert len(items2) == 2
    assert nc2 is None


def test_list_sources_page_returns_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.put_source(f"content-{i}".encode(), title=f"s{i}")
    items, next_cursor, total = store.list_sources_page(limit=3)
    assert total == 5
    assert len(items) == 3
    assert next_cursor is not None


def test_list_relations_page_returns_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(4):
        _entity(store, f"n-{i:03}")
    for i in range(3):
        _relation(store, f"n-{i:03}", f"n-{i+1:03}", f"r-{i:03}")
    items, _, total = store.list_relations_page(limit=2)
    assert total == 3
    assert len(items) == 2


def test_list_proposals_page_pending_only(tmp_path: Path) -> None:
    from vouch.proposals import propose_claim
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(5):
        propose_claim(store, text=f"claim {i}", evidence=[sid],
                      proposed_by="agent")
    items, _, total = store.list_proposals_page(
        ProposalStatus.PENDING, limit=3
    )
    assert total == 5
    assert len(items) == 3
    assert all(p.status == ProposalStatus.PENDING for p in items)


def test_empty_collection_returns_no_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    items, next_cursor, total = store.list_claims_page(limit=10)
    assert items == []
    assert next_cursor is None
    assert total == 0


def test_limit_larger_than_collection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(3):
        _claim(store, f"c-{i:03}", sid)
    items, next_cursor, total = store.list_claims_page(limit=100)
    assert len(items) == 3
    assert next_cursor is None
    assert total == 3


def test_cursor_resumes_correctly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(7):
        _claim(store, f"c-{i:03}", sid)
    page1, cur1, _ = store.list_claims_page(limit=3)
    page2, cur2, _ = store.list_claims_page(limit=3, cursor=cur1)
    page3, cur3, _ = store.list_claims_page(limit=3, cursor=cur2)
    all_ids = [c.id for c in page1 + page2 + page3]
    assert len(all_ids) == 7
    assert len(set(all_ids)) == 7
    assert all_ids == sorted(all_ids)
    assert cur3 is None


# ---------------------------------------------------------------------------
# fuzz: 1 000 artifacts -- every id returned exactly once, no gaps
# ---------------------------------------------------------------------------


def _random_id(prefix: str, n: int) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase, k=6))
    return f"{prefix}-{n:06}-{suffix}"


def test_fuzz_pagination_covers_all_claims(tmp_path: Path) -> None:
    """Walk 1 000 claims page-by-page. Every id must appear exactly once."""
    random.seed(42)
    store = _store(tmp_path)
    sid = _src(store, "evidence")
    expected_ids: set[str] = set()
    for i in range(1000):
        cid = _random_id("c", i)
        _claim(store, cid, sid)
        expected_ids.add(cid)

    seen_ids: list[str] = []
    cursor = None
    page_size = random.randint(7, 53)  # intentionally irregular page size
    while True:
        items, next_cursor, total = store.list_claims_page(
            limit=page_size, cursor=cursor
        )
        assert total == 1000, f"total must stay 1000, got {total}"
        seen_ids.extend(c.id for c in items)
        if next_cursor is None:
            break
        cursor = next_cursor

    assert len(seen_ids) == 1000, f"expected 1000 ids, got {len(seen_ids)}"
    assert len(set(seen_ids)) == 1000, "duplicate ids found across pages"
    assert set(seen_ids) == expected_ids, "some ids were missed or wrong"
    assert seen_ids == sorted(seen_ids), "ids are not in sorted order"


# ---------------------------------------------------------------------------
# JSONL envelope shape
# ---------------------------------------------------------------------------


def test_jsonl_list_claims_returns_paged_envelope(tmp_path: Path, monkeypatch) -> None:
    """kb.list_claims must return {items, _meta} not a bare list."""
    from vouch.jsonl_server import handle_request
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(3):
        _claim(store, f"c-{i:03}", sid)
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "1", "method": "kb.list_claims", "params": {}})
    assert resp["ok"]
    result = resp["result"]
    assert "items" in result
    assert "_meta" in result
    assert isinstance(result["items"], list)
    assert "next_cursor" in result["_meta"]
    assert "total" in result["_meta"]


def test_jsonl_list_pages_cursor_walks_all(tmp_path: Path, monkeypatch) -> None:
    """Cursor-walking via JSONL must return all pages exactly once."""
    from vouch.jsonl_server import handle_request
    store = _store(tmp_path)
    sid = _src(store)
    for i in range(5):
        _page(store, f"pg-{i:03}", sid)
    monkeypatch.chdir(store.root)
    seen = []
    cursor = None
    while True:
        params: dict = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = handle_request({"id": "1", "method": "kb.list_pages",
                               "params": params})
        assert resp["ok"]
        result = resp["result"]
        seen.extend(p["id"] for p in result["items"])
        cursor = result["_meta"]["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5
    assert len(set(seen)) == 5
