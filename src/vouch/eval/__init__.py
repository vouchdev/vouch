"""Retrieval-quality evaluation harness.

Scores the live `kb.context` retrieval (`build_context_pack`) against a
labeled query set and reports P@k / R@k / MRR / nDCG, plus a baseline
comparison used by CI to gate retrieval regressions.
"""

from __future__ import annotations

from .recall import (
    compare_baseline,
    load_queries,
    run_recall,
    score_query,
)

__all__ = [
    "compare_baseline",
    "load_queries",
    "run_recall",
    "score_query",
]
