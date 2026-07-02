"""Retrieval evaluation metrics: recall@k, MRR, nDCG.

Ground truth format: a set of "kind:id" strings (e.g. {"claim:c1"}).
Hits format matches index_db: list of (kind, id, snippet, score).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

Hit = tuple[str, str, str, float]


def _key(h: Hit) -> str:
    return f"{h[0]}:{h[1]}"


def recall_at_k(hits: list[Hit], relevant: set[str], *, k: int = 10) -> float:
    if not relevant:
        return 0.0
    top = {_key(h) for h in hits[:k]}
    return len(top & relevant) / len(relevant)


def mrr(hits: list[Hit], relevant: set[str]) -> float:
    for i, h in enumerate(hits, start=1):
        if _key(h) in relevant:
            return 1.0 / i
    return 0.0


def ndcg(hits: list[Hit], relevant: set[str], *, k: int = 10) -> float:
    dcg = 0.0
    for i, h in enumerate(hits[:k], start=1):
        if _key(h) in relevant:
            dcg += 1.0 / math.log2(i + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    if ideal <= 0:
        return 0.0
    return dcg / ideal


def evaluate(
    *,
    kb_dir: Path,
    queries_file: Path,
    k: int = 10,
    metrics: tuple[str, ...] = ("recall@k", "mrr", "ndcg"),
) -> dict[str, float]:
    """Run a metric sweep over a JSONL queries file."""
    from .. import index_db
    known = {"recall@k", "mrr", "ndcg"}
    unknown = set(metrics) - known
    if unknown:
        raise ValueError(f"unknown metric(s): {sorted(unknown)}; known: {sorted(known)}")
    totals = {m: 0.0 for m in metrics}
    n = 0
    with queries_file.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            q = row["query"]
            rel = set(row["relevant"])
            hits = index_db.search_semantic(kb_dir, q, limit=k)
            if "recall@k" in metrics:
                totals["recall@k"] += recall_at_k(hits, rel, k=k)
            if "mrr" in metrics:
                totals["mrr"] += mrr(hits, rel)
            if "ndcg" in metrics:
                totals["ndcg"] += ndcg(hits, rel, k=k)
            n += 1
    if n == 0:
        return {m: 0.0 for m in metrics}
    return {m: totals[m] / n for m in metrics}


def write_report(out: dict[str, float], path: Path) -> None:
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
