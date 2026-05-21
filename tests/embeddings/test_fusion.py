"""Fusion strategies -- RRF, weighted, normalized-cosine."""

from __future__ import annotations

import pytest

from vouch.embeddings.fusion import (
    normalized_fuse,
    rrf_fuse,
    weighted_fuse,
)


def _hits(items: list[tuple[str, str, float]]) -> list[tuple[str, str, str, float]]:
    return [(k, i, "", s) for k, i, s in items]


def test_rrf_fuse_prefers_top_of_both_lists() -> None:
    a = _hits([("claim", "x", 0.9), ("claim", "y", 0.5)])
    b = _hits([("claim", "y", 0.9), ("claim", "z", 0.4)])
    fused = rrf_fuse(a, b, limit=3)
    ids = [h[1] for h in fused]
    assert ids[0] == "y"


def test_rrf_fuse_handles_empty_inputs() -> None:
    assert rrf_fuse([], [], limit=5) == []


def test_weighted_fuse_respects_weights() -> None:
    a = _hits([("claim", "x", 1.0), ("claim", "y", 0.0)])
    b = _hits([("claim", "y", 1.0), ("claim", "x", 0.0)])
    only_a = weighted_fuse(a, b, w_a=1.0, w_b=0.0, limit=2)
    assert only_a[0][1] == "x"
    only_b = weighted_fuse(a, b, w_a=0.0, w_b=1.0, limit=2)
    assert only_b[0][1] == "y"


def test_normalized_fuse_zero_score_inputs() -> None:
    a = _hits([("claim", "x", 0.0)])
    b = _hits([("claim", "y", 0.0)])
    fused = normalized_fuse(a, b, limit=2)
    assert len(fused) == 2


def test_rrf_fuse_rejects_negative_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 0"):
        rrf_fuse([], [], limit=-1)


def test_rrf_fuse_rejects_negative_k() -> None:
    # k=-1 would make 1/(k+rank) explode (rank=1 -> divide by zero).
    with pytest.raises(ValueError, match="k must be >= 0"):
        rrf_fuse([], [], limit=5, k=-1)


def test_weighted_fuse_rejects_negative_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 0"):
        weighted_fuse([], [], limit=-1)
