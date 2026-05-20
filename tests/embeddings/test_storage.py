"""Embedding storage layer -- schema, put, get, search."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vouch import index_db
from vouch.embeddings.base import content_hash
from vouch.storage import KBStore


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    store = KBStore.init(tmp_path)
    return store.kb_dir


def test_embedding_schema_creates_tables(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table')"
        )}
    assert "embedding_index" in tables
    assert "query_embedding_cache" in tables
    assert "embedding_dupes" in tables


def test_embedding_meta_default_values(kb_dir: Path) -> None:
    meta = index_db.get_embedding_meta(kb_dir)
    assert meta.get("embedding_model") in (None, "")


def test_put_and_get_embedding(kb_dir: Path) -> None:
    vec = np.zeros(8, dtype=np.float32)
    vec[0] = 1.0
    h = content_hash("hello")
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec,
            content_hash=h, model="mock", model_version="1", dim=8,
        )
    got = index_db.get_embedding(kb_dir, kind="claim", id="c1")
    assert got is not None
    rec_vec, rec_hash, rec_model = got
    assert np.allclose(rec_vec, vec)
    assert rec_hash == h
    assert rec_model == "mock"


def test_put_embedding_idempotent_on_same_hash(kb_dir: Path) -> None:
    vec = np.ones(4, dtype=np.float32)
    h = content_hash("same")
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec, content_hash=h,
            model="mock", model_version="1", dim=4,
        )
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec, content_hash=h,
            model="mock", model_version="1", dim=4,
        )
    with index_db.open_db(kb_dir) as conn:
        n = conn.execute("SELECT COUNT(*) FROM embedding_index WHERE id='c1'").fetchone()[0]
    assert n == 1


def test_set_embedding_meta_round_trip(kb_dir: Path) -> None:
    index_db.set_embedding_meta(
        kb_dir,
        model="sentence-transformers/all-mpnet-base-v2",
        version="v1",
        dim=768,
    )
    meta = index_db.get_embedding_meta(kb_dir)
    assert meta["embedding_model"] == "sentence-transformers/all-mpnet-base-v2"
    assert meta["embedding_dim"] == "768"


def test_search_embedding_returns_topk(kb_dir: Path) -> None:
    from vouch.embeddings.base import content_hash as ch

    query = np.zeros(8, dtype=np.float32)
    query[0] = 1.0
    near = np.zeros(8, dtype=np.float32)
    near[0] = 0.99
    near[1] = 0.05
    far = np.zeros(8, dtype=np.float32)
    far[7] = 1.0
    near /= float(np.linalg.norm(near))
    with index_db.open_db(kb_dir) as conn:
        for cid, vec in [("c-near", near), ("c-far", far)]:
            index_db.put_embedding(
                conn, kind="claim", id=cid, vec=vec,
                content_hash=ch(cid), model="mock", model_version="1", dim=8,
            )
    hits = index_db.search_embedding(
        kb_dir, query_vec=query, kinds=("claim",), limit=2,
    )
    assert hits[0][1] == "c-near"
    assert hits[1][1] == "c-far"


def test_search_embedding_empty_db(kb_dir: Path) -> None:
    q = np.zeros(8, dtype=np.float32)
    q[0] = 1.0
    hits = index_db.search_embedding(kb_dir, query_vec=q, kinds=("claim",), limit=5)
    assert hits == []


def test_search_embedding_filters_by_kind(kb_dir: Path) -> None:
    from vouch.embeddings.base import content_hash as ch

    v = np.zeros(4, dtype=np.float32)
    v[0] = 1.0
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(conn, kind="claim", id="c1", vec=v,
                               content_hash=ch("c1"), model="mock",
                               model_version="1", dim=4)
        index_db.put_embedding(conn, kind="page", id="p1", vec=v,
                               content_hash=ch("p1"), model="mock",
                               model_version="1", dim=4)
    only_claims = index_db.search_embedding(
        kb_dir, query_vec=v, kinds=("claim",), limit=10,
    )
    assert {h[0] for h in only_claims} == {"claim"}


def test_sqlite_vec_loader_is_idempotent(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        a = index_db._load_sqlite_vec(conn)
        b = index_db._load_sqlite_vec(conn)
    assert a == b


def test_search_works_under_both_backends(kb_dir: Path) -> None:
    from vouch.embeddings.base import content_hash as ch
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((20, 8)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    with index_db.open_db(kb_dir) as conn:
        for i, v in enumerate(vecs):
            index_db.put_embedding(
                conn, kind="claim", id=f"c{i}", vec=v,
                content_hash=ch(f"c{i}"), model="mock",
                model_version="1", dim=8,
            )
    hits = index_db.search_embedding(
        kb_dir, query_vec=vecs[0], kinds=("claim",), limit=3,
    )
    assert hits[0][1] == "c0"  # exact self-match


def test_query_cache_round_trip(kb_dir: Path) -> None:
    from vouch.embeddings.cache import cache_query_vec, lookup_query_vec, query_cache_size
    v = np.ones(4, dtype=np.float32)
    cache_query_vec(kb_dir, query="hello", vec=v)
    got = lookup_query_vec(kb_dir, query="hello")
    assert got is not None
    assert np.allclose(got, v)
    assert lookup_query_vec(kb_dir, query="other") is None
    assert query_cache_size(kb_dir) == 1


def test_query_cache_lru_eviction(kb_dir: Path) -> None:
    from vouch.embeddings.cache import cache_query_vec, query_cache_size
    v = np.ones(4, dtype=np.float32)
    for i in range(5):
        cache_query_vec(kb_dir, query=f"q{i}", vec=v, max_entries=3)
    assert query_cache_size(kb_dir) <= 3
