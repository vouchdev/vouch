"""`context._retrieve` honors `retrieval.backend` in config.yaml (#92).

These tests monkeypatch `index_db.search_semantic` so they exercise the
dispatch logic without needing the optional embeddings extras (numpy /
sentence-transformers), and therefore run under the base CI install.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import context, health
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"e")
    s.put_claim(Claim(id="c1", text="JWT token rotation", evidence=[src.id]))
    health.rebuild_index(s)
    return s


def _set_backend(store: KBStore, backend: str) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg.setdefault("retrieval", {})["backend"] = backend
    store.config_path.write_text(yaml.safe_dump(cfg))


def _force_semantic_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the embedding path always return a hit, so a backend label of
    "embedding" appears iff `_retrieve` actually consulted semantic search."""
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "JWT token rotation", 0.99)],
    )


def _backends(pack: dict) -> set[str]:
    return {item["backend"] for item in pack["items"]}


def test_backend_fts5_skips_embedding(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #92: with retrieval.backend=fts5, the embedding path
    must not run even when it would return hits."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "fts5")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert "embedding" not in _backends(pack)
    assert _backends(pack) <= {"fts5", "substring"}


def test_backend_embedding_is_recognized(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`embedding` is an accepted value and forces the semantic path."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "embedding")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert _backends(pack) == {"embedding"}


def test_backend_substring_only(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "substring")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert _backends(pack) == {"substring"}


def test_backend_auto_now_fuses(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto` no longer waterfalls embedding-first; it fuses embedding + fts5
    (RRF) and tags hits `hybrid`."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "auto")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert _backends(pack) == {"hybrid"}


def test_unset_backend_fuses(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config with no retrieval.backend behaves like fused `auto`."""
    _force_semantic_hit(monkeypatch)
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg.get("retrieval", {}).pop("backend", None)
    store.config_path.write_text(yaml.safe_dump(cfg))
    pack = context.build_context_pack(store, query="JWT")
    assert _backends(pack) == {"hybrid"}


def test_backend_hybrid_merges_semantic_and_lexical(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hybrid` returns the union of both retrievers, not first-non-empty."""
    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "JWT token rotation", 0.99)],
    )
    monkeypatch.setattr(
        context.index_db, "search",
        lambda *a, **k: [("claim", "c2", "OAuth refresh flow", 0.88)],
    )
    _set_backend(store, "hybrid")
    pack = context.build_context_pack(store, query="auth")
    assert {item["id"] for item in pack["items"]} == {"c1", "c2"}
    assert _backends(pack) == {"hybrid"}


def test_near_duplicate_summaries_are_dropped(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent should not see the same fact twice."""
    src = store.put_source(b"z")
    store.put_claim(Claim(
        id="d1", text="the cache uses redis with a 60 second ttl", evidence=[src.id]))
    store.put_claim(Claim(
        id="d2", text="the cache uses redis with a 60 second ttl now", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [
            ("claim", "d1", "the cache uses redis with a 60 second ttl", 0.90),
            ("claim", "d2", "the cache uses redis with a 60 second ttl now", 0.89),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    _set_backend(store, "hybrid")
    pack = context.build_context_pack(store, query="cache")
    assert {item["id"] for item in pack["items"]} == {"d1"}


def test_dedupe_keeps_highest_scored_regardless_of_input_order() -> None:
    """Invariant: the highest-scored member of a near-duplicate cluster
    survives even when items arrive out of score order (as graph-expansion
    neighbours can)."""
    from vouch.context import _dedupe_near_duplicates
    from vouch.models import ContextItem

    lo = ContextItem(id="lo", type="claim",
                     summary="the cache uses redis with a 60 second ttl",
                     score=0.30, backend="hybrid", citations=[], freshness="unknown")
    hi = ContextItem(id="hi", type="claim",
                     summary="the cache uses redis with a 60 second ttl now",
                     score=0.90, backend="hybrid", citations=[], freshness="unknown")
    out = _dedupe_near_duplicates([lo, hi])  # deliberately low-score-first
    assert [i.id for i in out] == ["hi"]
