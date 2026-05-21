"""Result-list fusion strategies for hybrid retrieval."""

from __future__ import annotations

Hit = tuple[str, str, str, float]
Hits = list[Hit]


def _key(h: Hit) -> tuple[str, str]:
    return (h[0], h[1])


def _coalesce_snippet(h: Hit, other: Hits) -> str:
    if h[2]:
        return h[2]
    for o in other:
        if _key(o) == _key(h) and o[2]:
            return o[2]
    return ""


def rrf_fuse(a: Hits, b: Hits, *, limit: int = 10, k: int = 60) -> Hits:
    """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across lists."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if k < 0:
        # rank starts at 1, so k must be > -1, but disallow negative
        # entirely to match the canonical RRF definition (Cormack et al.)
        # and to avoid division by zero when k == -rank.
        raise ValueError("k must be >= 0")
    scores: dict[tuple[str, str], float] = {}
    for lst in (a, b):
        for rank, h in enumerate(lst, start=1):
            scores[_key(h)] = scores.get(_key(h), 0.0) + 1.0 / (k + rank)
    out: Hits = []
    seen: set[tuple[str, str]] = set()
    for h in a + b:
        if _key(h) in seen:
            continue
        seen.add(_key(h))
        out.append((h[0], h[1], _coalesce_snippet(h, a + b), scores[_key(h)]))
    out.sort(key=lambda h: h[3], reverse=True)
    return out[:limit]


def _minmax(xs: list[float]) -> list[float]:
    if not xs:
        return xs
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [0.5] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def weighted_fuse(
    a: Hits,
    b: Hits,
    *,
    w_a: float = 0.5,
    w_b: float = 0.5,
    limit: int = 10,
) -> Hits:
    """Min-max normalize each list, then score = w_a * a_score + w_b * b_score."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    a_norm = _minmax([h[3] for h in a])
    b_norm = _minmax([h[3] for h in b])
    a_score = {_key(h): a_norm[i] for i, h in enumerate(a)}
    b_score = {_key(h): b_norm[i] for i, h in enumerate(b)}
    merged: dict[tuple[str, str], float] = {}
    for key in {*a_score, *b_score}:
        merged[key] = w_a * a_score.get(key, 0.0) + w_b * b_score.get(key, 0.0)
    out: Hits = []
    seen: set[tuple[str, str]] = set()
    for h in a + b:
        if _key(h) in seen:
            continue
        seen.add(_key(h))
        out.append((h[0], h[1], _coalesce_snippet(h, a + b), merged[_key(h)]))
    out.sort(key=lambda h: h[3], reverse=True)
    return out[:limit]


def normalized_fuse(a: Hits, b: Hits, *, limit: int = 10) -> Hits:
    """Equal-weight weighted_fuse (0.5/0.5)."""
    return weighted_fuse(a, b, w_a=0.5, w_b=0.5, limit=limit)
