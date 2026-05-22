"""Cross-encoder reranker over candidate top-K."""

from __future__ import annotations

from collections.abc import Sequence

from vouch.embeddings.rerank import Reranker, rerank


class MockReranker(Reranker):
    name = "mock-reranker"
    version = "1"

    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        return [10.0 if "BOOST" in c else 0.0 for c in candidates]


def test_rerank_reorders_by_cross_encoder_score() -> None:
    hits = [("claim", "a", "no marker here", 0.9),
            ("claim", "b", "BOOST in this one", 0.1),
            ("claim", "c", "also no marker", 0.5)]
    out = rerank(query="anything", hits=hits, reranker=MockReranker(), top_k=3)
    assert out[0][1] == "b"


def test_rerank_top_k_truncates() -> None:
    hits = [("claim", "a", "x", 0.5), ("claim", "b", "y", 0.3)]
    out = rerank(query="q", hits=hits, reranker=MockReranker(), top_k=1)
    assert len(out) == 1


def test_rerank_empty_hits() -> None:
    out = rerank(query="q", hits=[], reranker=MockReranker(), top_k=5)
    assert out == []
