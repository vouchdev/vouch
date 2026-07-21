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


def _set_rerank(store: KBStore, *, enabled: bool, top_k: int | None = None) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    rerank_cfg = {"enabled": enabled}
    if top_k is not None:
        rerank_cfg["top_k"] = top_k
    cfg.setdefault("retrieval", {})["rerank"] = rerank_cfg
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


def test_context_rerank_disabled_preserves_hybrid_order(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [
            ("claim", "c1", "JWT token rotation", 0.90),
            ("claim", "c2", "OAuth refresh flow", 0.80),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    _set_backend(store, "hybrid")
    _set_rerank(store, enabled=False)

    pack = context.build_context_pack(store, query="auth", limit=2)

    assert [item["id"] for item in pack["items"]] == ["c1", "c2"]


def test_context_rerank_enabled_reorders_scoped_window(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch.embeddings import rerank as rerank_mod

    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    store.put_claim(Claim(id="c3", text="SAML login flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [
            ("claim", "c1", "JWT token rotation", 0.90),
            ("claim", "c2", "OAuth refresh flow", 0.80),
            ("claim", "c3", "SAML login flow", 0.70),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    monkeypatch.setattr(context, "_RERANKER_CACHE", None)
    monkeypatch.setattr(rerank_mod, "default_reranker", lambda: object())
    monkeypatch.setattr(
        rerank_mod,
        "rerank",
        lambda *, query, hits, reranker, top_k: [
            (hits[1][0], hits[1][1], hits[1][2], 99.0),
            (hits[0][0], hits[0][1], hits[0][2], 88.0),
        ][:top_k],
    )
    _set_backend(store, "hybrid")
    _set_rerank(store, enabled=False)
    before = context.build_context_pack(store, query="auth", limit=3)
    scores_by_id = {item["id"]: item["score"] for item in before["items"]}
    _set_rerank(store, enabled=True, top_k=2)

    pack = context.build_context_pack(store, query="auth", limit=3)

    assert [item["id"] for item in pack["items"]] == ["c2", "c1", "c3"]
    assert [item["score"] for item in pack["items"]] == [
        scores_by_id["c2"],
        scores_by_id["c1"],
        scores_by_id["c3"],
    ]


def test_context_rerank_bool_top_k_falls_back_to_limit(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch.embeddings import rerank as rerank_mod

    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    store.put_claim(Claim(id="c3", text="SAML login flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [
            ("claim", "c1", "JWT token rotation", 0.90),
            ("claim", "c2", "OAuth refresh flow", 0.80),
            ("claim", "c3", "SAML login flow", 0.70),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    seen_top_k: list[int] = []
    monkeypatch.setattr(context, "_RERANKER_CACHE", None)
    monkeypatch.setattr(rerank_mod, "default_reranker", lambda: object())

    def fake_rerank(*, query, hits, reranker, top_k):
        seen_top_k.append(top_k)
        return list(reversed(hits))

    monkeypatch.setattr(rerank_mod, "rerank", fake_rerank)
    _set_backend(store, "hybrid")
    _set_rerank(store, enabled=True, top_k=True)

    pack = context.build_context_pack(store, query="auth", limit=3)

    assert seen_top_k == [3]
    assert [item["id"] for item in pack["items"]] == ["c3", "c2", "c1"]


def test_context_rerank_reuses_default_reranker(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch.embeddings import rerank as rerank_mod

    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [
            ("claim", "c1", "JWT token rotation", 0.90),
            ("claim", "c2", "OAuth refresh flow", 0.80),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    calls = 0
    monkeypatch.setattr(context, "_RERANKER_CACHE", None)

    def fake_default_reranker():
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr(rerank_mod, "default_reranker", fake_default_reranker)
    monkeypatch.setattr(rerank_mod, "rerank", lambda *, query, hits, reranker, top_k: hits)
    _set_backend(store, "hybrid")
    _set_rerank(store, enabled=True)

    context.build_context_pack(store, query="auth", limit=2)
    context.build_context_pack(store, query="auth", limit=2)

    assert calls == 1


def test_context_rerank_missing_extra_degrades_to_fused_order(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch.embeddings import rerank as rerank_mod

    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [
            ("claim", "c1", "JWT token rotation", 0.90),
            ("claim", "c2", "OAuth refresh flow", 0.80),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    monkeypatch.setattr(context, "_RERANKER_CACHE", None)
    monkeypatch.setattr(
        rerank_mod,
        "default_reranker",
        lambda: (_ for _ in ()).throw(ImportError("missing optional extra")),
    )
    _set_backend(store, "hybrid")
    _set_rerank(store, enabled=True)

    pack = context.build_context_pack(store, query="auth", limit=2)

    assert [item["id"] for item in pack["items"]] == ["c1", "c2"]


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


def test_dedupe_preserves_input_order_not_score_order() -> None:
    """Survivors keep the caller's order (ranked hits first, appended
    neighbours last) even when a later distinct item outscores an earlier one,
    so budget eviction drops the tail, not the real matches."""
    from vouch.context import _dedupe_near_duplicates
    from vouch.models import ContextItem

    a = ContextItem(id="a", type="claim", summary="alpha topic one",
                    score=0.02, backend="hybrid", citations=[], freshness="unknown")
    b = ContextItem(id="b", type="claim", summary="beta subject two",
                    score=0.32, backend="graph", citations=[], freshness="unknown")
    out = _dedupe_near_duplicates([a, b])  # distinct summaries, a first but lower-scored
    assert [i.id for i in out] == ["a", "b"]


# --- recency decay (retrieval.recency) --------------------------------------


def _set_recency(
    store: KBStore, *, enabled: bool, half_life_days: float | None = None
) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    recency_cfg: dict = {"enabled": enabled}
    if half_life_days is not None:
        recency_cfg["half_life_days"] = half_life_days
    cfg.setdefault("retrieval", {})["recency"] = recency_cfg
    store.config_path.write_text(yaml.safe_dump(cfg))


def _backdate_claim(store: KBStore, claim_id: str, *, days: int) -> None:
    from datetime import UTC, datetime, timedelta

    path = store.kb_dir / "claims" / f"{claim_id}.yaml"
    raw = yaml.safe_load(path.read_text())
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    raw["created_at"] = stamp
    raw["updated_at"] = stamp
    path.write_text(yaml.safe_dump(raw))


def _two_claim_fts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic lexical ranking: c1 above c2, no embeddings."""
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(
        context.index_db, "search",
        lambda *a, **k: [
            ("claim", "c1", "JWT token rotation", 1.0),
            ("claim", "c2", "JWT token rotation policy", 0.9),
        ],
    )


@pytest.fixture
def two_claim_store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"e")
    s.put_claim(Claim(id="c1", text="JWT token rotation", evidence=[src.id]))
    s.put_claim(Claim(id="c2", text="JWT token rotation policy", evidence=[src.id]))
    health.rebuild_index(s)
    return s


def test_recency_prefers_fresh_over_stale(
    two_claim_store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A year-old artifact loses to an equally-relevant fresh one when
    retrieval.recency is enabled (the starter-config default)."""
    _two_claim_fts(monkeypatch)
    _set_backend(two_claim_store, "hybrid")
    _set_recency(two_claim_store, enabled=True, half_life_days=90)
    _backdate_claim(two_claim_store, "c1", days=365)

    pack = context.build_context_pack(two_claim_store, query="JWT", limit=2)

    assert [item["id"] for item in pack["items"]] == ["c2", "c1"]
    assert pack["retrieval"]["recency"] is True


def test_recency_disabled_keeps_fused_order(
    two_claim_store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing KBs (no opt-in) keep byte-identical ordering."""
    _two_claim_fts(monkeypatch)
    _set_backend(two_claim_store, "hybrid")
    _set_recency(two_claim_store, enabled=False)
    _backdate_claim(two_claim_store, "c1", days=365)

    pack = context.build_context_pack(two_claim_store, query="JWT", limit=2)

    assert [item["id"] for item in pack["items"]] == ["c1", "c2"]
    assert pack["retrieval"]["recency"] is False


# --- search_kb: the one shared kb.search implementation ----------------------


def test_search_kb_auto_reports_actual_backend_and_degradation(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A semantic-capable backend with no embeddings serves lexical hits and
    says so: used=fts5, degraded=true — never a hollow \"hybrid\"."""
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(
        context.index_db, "semantic_search_available", lambda: False
    )

    result = context.search_kb(store, query="JWT token rotation")

    assert result["hits"], result
    assert result["backend"] == "fts5"
    assert result["retrieval"]["configured"] == "hybrid"  # starter default
    assert result["retrieval"]["used"] == "fts5"
    assert result["retrieval"]["semantic_available"] is False
    assert result["retrieval"]["degraded"] is True


def test_search_kb_hybrid_label_requires_both_retrievers(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "JWT token rotation", 0.99)],
    )
    monkeypatch.setattr(
        context.index_db, "semantic_search_available", lambda: True
    )

    result = context.search_kb(store, query="JWT token rotation", backend="hybrid")

    assert result["backend"] == "hybrid"
    assert result["retrieval"]["degraded"] is False


def test_search_kb_none_backend_defers_to_config(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_backend(store, "substring")

    result = context.search_kb(store, query="JWT token rotation")

    assert result["retrieval"]["configured"] == "substring"
    assert result["backend"] == "substring"
    assert result["hits"], result


def test_search_kb_auto_substring_fallback_when_all_empty(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    _set_backend(store, "auto")

    result = context.search_kb(store, query="JWT token rotation")

    assert result["backend"] == "substring"
    assert result["hits"], result


def test_search_surfaces_share_search_kb(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP and JSONL kb.search both return the retrieval honesty block —
    they delegate to context.search_kb rather than carrying copies."""
    monkeypatch.chdir(store.root)
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])

    from vouch.jsonl_server import handle_request

    jsonl = handle_request(
        {"id": 1, "method": "kb.search", "params": {"query": "JWT token rotation"}}
    )
    assert jsonl["result"]["retrieval"]["used"] == "fts5"

    from vouch import server as mcp_server

    mcp_result = mcp_server.kb_search(query="JWT token rotation")
    assert mcp_result["retrieval"]["used"] == "fts5"
