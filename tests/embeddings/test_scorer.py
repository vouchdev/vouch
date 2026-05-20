"""Metrics for embedding retrieval."""

from __future__ import annotations

from vouch.embeddings.scorer import mrr, ndcg, recall_at_k


def test_recall_at_k_full_hit() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c1", "", 0.9), ("claim", "c2", "", 0.1)]
    assert recall_at_k(hits, relevant, k=1) == 1.0


def test_recall_at_k_miss() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c2", "", 0.9)]
    assert recall_at_k(hits, relevant, k=1) == 0.0


def test_recall_at_k_partial_with_k_larger_than_hits() -> None:
    relevant = {"claim:c1", "claim:c2"}
    hits = [("claim", "c1", "", 0.9)]
    assert recall_at_k(hits, relevant, k=5) == 0.5


def test_mrr_first_position() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c1", "", 0.9), ("claim", "c2", "", 0.5)]
    assert mrr(hits, relevant) == 1.0


def test_mrr_second_position() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c2", "", 0.9), ("claim", "c1", "", 0.5)]
    assert abs(mrr(hits, relevant) - 0.5) < 1e-9


def test_ndcg_monotonic() -> None:
    relevant = {"claim:c1"}
    good = [("claim", "c1", "", 1.0), ("claim", "c2", "", 0.5)]
    bad = [("claim", "c2", "", 1.0), ("claim", "c1", "", 0.5)]
    assert ndcg(good, relevant, k=2) > ndcg(bad, relevant, k=2)
