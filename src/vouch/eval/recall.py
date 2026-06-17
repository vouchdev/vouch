"""Recall-quality eval: score `build_context_pack` against a labeled set.

Pure-Python metrics (no numpy). The ranked result for a query is the
ordered list of `items` ids returned by `build_context_pack`; expected ids
are the human-labeled relevant claim ids for that query.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..context import build_context_pack

if TYPE_CHECKING:
    from ..storage import KBStore

_METRICS = ("p_at_k", "r_at_k", "mrr", "ndcg_at_k")


def load_queries(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL labeled query set.

    Each line is ``{"query": str, "expected": [<claim_id>, ...]}``. The key
    ``expected_ids`` is accepted as an alias for ``expected``. Blank lines are
    skipped.
    """
    queries: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            expected = row.get("expected", row.get("expected_ids", []))
            queries.append({"query": row["query"], "expected": list(expected)})
    return queries


def score_query(
    ranked_ids: list[str], expected: list[str], *, k: int = 5
) -> dict[str, float]:
    """Compute P@k, R@k, MRR and nDCG@k for one ranked list vs expected ids."""
    rel = set(expected)
    top = ranked_ids[:k]
    hits = sum(1 for rid in top if rid in rel)

    p_at_k = hits / k if k else 0.0
    r_at_k = hits / len(rel) if rel else 0.0

    mrr = 0.0
    for i, rid in enumerate(ranked_ids, start=1):
        if rid in rel:
            mrr = 1.0 / i
            break

    dcg = 0.0
    for i, rid in enumerate(top, start=1):
        if rid in rel:
            dcg += 1.0 / math.log2(i + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rel), k) + 1))
    ndcg = dcg / ideal if ideal > 0 else 0.0

    return {"p_at_k": p_at_k, "r_at_k": r_at_k, "mrr": mrr, "ndcg_at_k": ndcg}


def _macro(per_query: list[dict[str, Any]]) -> dict[str, float]:
    if not per_query:
        return dict.fromkeys(_METRICS, 0.0)
    n = len(per_query)
    return {m: sum(q["scores"][m] for q in per_query) / n for m in _METRICS}


def run_recall(
    store: KBStore, queries_path: str | Path, *, k: int = 5
) -> dict[str, Any]:
    """Score retrieval over a labeled query set and return a report.

    For each query the ranked result is the ordered ``items`` ids from
    ``build_context_pack(store, query=q, limit=max(k, 10))``. The report is
    deterministic (queries preserve input order; metrics are pure functions).
    """
    queries = load_queries(queries_path)
    limit = max(k, 10)
    per_query: list[dict[str, Any]] = []
    for row in queries:
        pack = cast(
            dict[str, Any],
            build_context_pack(store, query=row["query"], limit=limit),
        )
        ranked_ids = [item["id"] for item in pack["items"]]
        scores = score_query(ranked_ids, row["expected"], k=k)
        per_query.append(
            {
                "query": row["query"],
                "expected": row["expected"],
                "ranked": ranked_ids,
                "scores": scores,
            }
        )
    return {
        "k": k,
        "n_queries": len(per_query),
        "macro": _macro(per_query),
        "per_query": per_query,
    }


def compare_baseline(
    report: dict[str, Any], baseline: dict[str, Any], *, max_regression: float = 0.05
) -> tuple[bool, str]:
    """Compare a fresh report against a committed baseline on macro P@k.

    Returns ``(ok, message)``. Not ok when the report's macro ``p_at_k`` falls
    below ``baseline.macro.p_at_k - max_regression``.
    """
    cur = float(report["macro"]["p_at_k"])
    base = float(baseline["macro"]["p_at_k"])
    floor = base - max_regression
    delta = cur - base
    if cur < floor:
        return (
            False,
            f"P@{report['k']} regression: {cur:.4f} < baseline {base:.4f} "
            f"- tol {max_regression:.4f} = {floor:.4f} (delta {delta:+.4f})",
        )
    return (
        True,
        f"P@{report['k']} ok: {cur:.4f} vs baseline {base:.4f} "
        f"(delta {delta:+.4f}, tol {max_regression:.4f})",
    )
