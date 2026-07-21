"""Evaluation harnesses: retrieval quality and knowledge effectiveness.

`recall.py` scores the live `kb.context` retrieval (`build_context_pack`)
against a labeled query set and reports P@k / R@k / MRR / nDCG, plus a
baseline comparison used by CI to gate retrieval regressions.

`effectiveness.py` is the outcome layer above it (vouchdev/vouch#426):
per-artifact lift between context-pack surfacing and a coarse audit-derived
session outcome.
"""

from __future__ import annotations

from .effectiveness import (
    ArtifactEffect,
    EffectivenessError,
    EffectivenessReport,
    compute,
    render_text,
    wilson_interval,
)
from .recall import (
    compare_baseline,
    load_queries,
    run_recall,
    score_query,
)

__all__ = [
    "ArtifactEffect",
    "EffectivenessError",
    "EffectivenessReport",
    "compare_baseline",
    "compute",
    "load_queries",
    "render_text",
    "run_recall",
    "score_query",
    "wilson_interval",
]
