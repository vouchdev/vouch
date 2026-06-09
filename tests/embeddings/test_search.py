"""End-to-end semantic search through KBStore + index_db."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.embeddings._fakes import MockEmbedder
from vouch import index_db
from vouch.embeddings import register
from vouch.models import (
    Claim,
    Entity,
    EntityType,
    Evidence,
    Page,
    Relation,
    RelationType,
)
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _use_mock_embedder() -> None:
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_put_claim_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="claim text", evidence=[src.id]))
    rec = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec is not None
    vec, _ch, model = rec
    assert vec.shape == (8,)
    assert model == "mock"


def test_put_page_writes_embedding(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="Title", body="page body"))
    rec = index_db.get_embedding(store.kb_dir, kind="page", id="p1")
    assert rec is not None
    assert rec[0].shape == (8,)


def test_put_source_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"content bytes here", title="src1")
    rec = index_db.get_embedding(store.kb_dir, kind="source", id=src.id)
    assert rec is not None


def test_put_entity_writes_embedding(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="AuthN", type=EntityType.CONCEPT,
                            description="who you are"))
    rec = index_db.get_embedding(store.kb_dir, kind="entity", id="e1")
    assert rec is not None


def test_put_relation_writes_embedding(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="x", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="e2", name="y", type=EntityType.CONCEPT))
    rel = Relation(id="r1", relation=RelationType.SIMILAR_TO,
                   source="e1", target="e2")
    store.put_relation(rel)
    rec = index_db.get_embedding(store.kb_dir, kind="relation", id="r1")
    assert rec is not None


def test_put_evidence_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"abc")
    ev = Evidence(id="ev1", source_id=src.id, locator="line 1",
                  quote="excerpt text body")
    store.put_evidence(ev)
    rec = index_db.get_embedding(store.kb_dir, kind="evidence", id="ev1")
    assert rec is not None


def test_update_claim_recomputes_embedding(store: KBStore) -> None:
    src = store.put_source(b"e")
    c = store.put_claim(Claim(id="c1", text="original", evidence=[src.id]))
    rec_before = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec_before is not None
    hash_before = rec_before[1]

    c2 = c.model_copy(update={"text": "updated"})
    store.update_claim(c2)
    rec_after = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec_after is not None
    assert rec_after[1] != hash_before


def test_jsonl_search_uses_embedding_backend(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch import jsonl_server
    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="claim about logins", evidence=[src.id]))
    from vouch import health
    health.rebuild_index(store)
    resp = jsonl_server.handle_request({
        "id": "1", "method": "kb.search",
        "params": {"query": "claim about logins"},
    })
    assert resp["ok"] is True
    assert resp["result"]["hits"]
    assert resp["result"]["backend"] in ("embedding", "fts5", "substring")


def test_kb_search_defaults_to_semantic_then_fts5(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="authentication design", evidence=[src.id]))
    from vouch import health
    health.rebuild_index(store)
    result = server.kb_search("authentication design", limit=5)
    assert result["hits"]
    assert result["backend"] in ("embedding", "fts5", "substring")
    assert result["hits"][0]["id"] == "c1"


def test_mcp_kb_reindex_embeddings(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    out = server.kb_reindex_embeddings(backfill=True)
    assert out["touched"] >= 1


def test_mcp_kb_dedup_scan(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    out = server.kb_dedup_scan(threshold=0.95, dry_run=True)
    assert "duplicates" in out


def test_mcp_kb_embeddings_stats(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    out = server.kb_embeddings_stats()
    assert "model" in out


def test_jsonl_kb_reindex_embeddings(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import jsonl_server
    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    resp = jsonl_server.handle_request({
        "id": "1", "method": "kb.reindex_embeddings",
        "params": {"backfill": True},
    })
    assert resp["ok"] is True


def test_search_semantic_returns_top_hits(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="alpha alpha alpha", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="beta beta beta", evidence=[src.id]))
    hits = index_db.search_semantic(store.kb_dir, query="alpha alpha alpha", limit=2)
    assert hits[0][1] == "c1"
