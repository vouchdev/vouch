"""Cross-encoder reranking over candidate hits."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

Hit = tuple[str, str, str, float]


class Reranker(ABC):
    name: ClassVar[str] = ""
    version: ClassVar[str] = ""

    @abstractmethod
    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        """Return one score per candidate. Higher = more relevant."""


class CrossEncoderReranker(Reranker):
    name = "cross-encoder/ms-marco-MiniLM-L6-v2"
    version = "v1"

    def __init__(self) -> None:
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
        self._model: Any = CrossEncoder(self.name)

    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        if not candidates:
            return []
        pairs = [(query, c) for c in candidates]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]


def rerank(
    *, query: str, hits: list[Hit], reranker: Reranker, top_k: int = 50,
) -> list[Hit]:
    if not hits:
        return []
    if top_k < 0:
        raise ValueError(f"top_k must be >= 0, got {top_k}")
    candidates = [h[2] or h[1] for h in hits]
    scores = reranker.score(query, candidates)
    if len(scores) != len(candidates):
        raise ValueError(
            f"reranker returned {len(scores)} scores for {len(candidates)} candidates"
        )
    reranked = [
        (kind, id_, snip, score)
        for (kind, id_, snip, _orig), score in zip(hits, scores, strict=True)
    ]
    reranked.sort(key=lambda h: h[3], reverse=True)
    return reranked[:top_k]


def default_reranker() -> Reranker:
    """Return a CrossEncoderReranker; raise ImportError if extras not installed."""
    return CrossEncoderReranker()
