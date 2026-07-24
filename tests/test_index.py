"""SQLite FTS5 index — rebuild, search, special characters."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import health, index_db
from vouch.models import Claim, Entity, EntityType, Page
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_index_rebuild_then_search(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="JWT tokens are stateless",
                          evidence=[src.id]))
    store.put_page(Page(id="p1", title="Auth", body="overview of auth"))
    store.put_entity(Entity(id="e1", name="JWT", type=EntityType.CONCEPT))
    health.rebuild_index(store)
    hits = index_db.search(store.kb_dir, "JWT")
    kinds = {k for k, *_ in hits}
    assert "claim" in kinds
    assert "entity" in kinds


def test_search_handles_special_chars(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="quote \"this\" and (parens)",
                          evidence=[src.id]))
    health.rebuild_index(store)
    # Should not crash on the FTS5-special characters.
    hits = index_db.search(store.kb_dir, 'quote "this"')
    assert any(k == "claim" for k, *_ in hits)


def test_index_via_approve(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="indexed automatically",
                       evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="u")
    hits = index_db.search(store.kb_dir, "indexed")
    assert any(k == "claim" for k, *_ in hits)


def test_reset_clears_legacy_embeddings_table(store: KBStore) -> None:
    """reset() promises to clear every derived table, embedding vectors
    included — but the legacy `embeddings` table (the one
    `search_embeddings` scans) was left out, so a deleted artifact's
    vector survived a full reindex and kept coming back as a semantic
    hit: exactly the orphaned-hits case the docstring warns about."""
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_embedding(conn, kind="claim", id="ghost", vec=[0.1, 0.2])

    index_db.reset(store.kb_dir)

    with index_db.open_db(store.kb_dir) as conn:
        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert count == 0


def test_deindex_removes_legacy_embedding_row(store: KBStore) -> None:
    """deindex() documents removing the embedding rows for a deleted
    artifact; it cleared `embedding_index` but left the legacy
    `embeddings` row behind."""
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_embedding(conn, kind="claim", id="c1", vec=[0.1, 0.2])
        index_db.deindex(conn, kind="claim", id="c1")
        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert count == 0
