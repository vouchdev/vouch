"""Advisory anomaly flags on pending proposals (vouchdev/vouch#323).

Reviewers scan the pending queue linearly, and at a glance every item carries
the same apparent weight. The ones that most deserve a hard look — a claim far
from anything already in the kb, a claim that contradicts a pile of approved
ones, a claim scraping by with the barest evidence — blend in. The
near-*duplicate* direction already surfaces (``find_similar_on_propose``
attaches non-blocking warnings on propose); this surfaces the *outlier*
direction.

This computes a small set of heuristics per pending claim proposal and returns
them as read-side annotations. It is a **hint** for the human reviewer: it never
rejects, approves, blocks, or rewrites anything, emits no proposal, and touches
no artifact — so it goes nowhere near ``proposals.approve()``. Scoring logic
lives here, not in ``storage.py``, which stays pure I/O.

Reason codes:

* ``thin_evidence`` — evidence list at or below a floor (``propose_claim``
  already requires ≥1; this is the softer "technically cited but suspiciously
  thin" case). Non-embedding.
* ``contradicts_many`` — the proposal declares it contradicts a threshold number
  of *approved, live* claims (over ``kb.contradict``'s existing notion).
  Non-embedding.
* ``far_from_corpus`` — nearest-neighbour cosine to the approved claim corpus is
  below a floor (no neighbour is close → an outlier). Embedding-derived, so it
  degrades gracefully to *no code* when the embeddings extra is absent — the
  same swallow ``find_similar_on_propose`` does — leaving the two non-embedding
  codes still computed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import yaml

from .models import ClaimStatus, ProposalStatus
from .storage import KBStore

_log = logging.getLogger("vouch.anomaly")

# Defaults. All overridable per-KB under ``review.anomaly.*`` in config.yaml,
# resolved like ``embeddings.similarity.similarity_threshold``.
DEFAULT_MIN_EVIDENCE = 1  # evidence count <= this => thin
DEFAULT_CONTRADICTION_COUNT = 1  # >= this many approved contradictions => flag
DEFAULT_FAR_FROM_CORPUS_FLOOR = 0.35  # best cosine < this => outlier

# Claims in these statuses are not live corpus (mirrors metrics / health), so a
# declared contradiction against one of them does not count.
_RETIRED = frozenset(
    {ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED}
)

_MAX_IDS = 10


@dataclass(frozen=True)
class Anomaly:
    """One flagged pending proposal and its reason codes (worst-first list)."""

    proposal_id: str
    kind: str
    proposed_by: str
    reasons: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "proposed_by": self.proposed_by,
            "reasons": self.reasons,
        }


def _resolve_cfg(
    store: KBStore,
    min_evidence: int | None,
    contradiction_count: int | None,
    far_floor: float | None,
) -> tuple[int, int, float]:
    """Fill unset thresholds from ``review.anomaly.*``, else the module defaults."""
    review: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("review"), dict):
            anomaly = loaded["review"].get("anomaly")
            if isinstance(anomaly, dict):
                review = anomaly
    except Exception:
        pass

    def pick(val, key, default):  # type: ignore[no-untyped-def]
        if val is not None:
            return val
        return review[key] if review.get(key) is not None else default

    return (
        int(pick(min_evidence, "min_evidence", DEFAULT_MIN_EVIDENCE)),
        int(pick(contradiction_count, "contradiction_count", DEFAULT_CONTRADICTION_COUNT)),
        float(pick(far_floor, "far_from_corpus_floor", DEFAULT_FAR_FROM_CORPUS_FLOOR)),
    )


def _try_embedder() -> Any:
    """Return an embedder, or ``None`` when the embeddings extra is absent."""
    try:
        from .embeddings import get_embedder

        return get_embedder()
    except Exception as e:  # ImportError, missing model, etc.
        _log.debug("anomaly: embeddings unavailable, far_from_corpus skipped: %s", e)
        return None


def _far_from_corpus(
    store: KBStore, embedder: Any, text: str, floor: float
) -> dict[str, Any] | None:
    """Nearest-neighbour cosine to the approved claim corpus, or ``None``.

    ``None`` when embeddings error out or the corpus is empty/unindexed (nothing
    to be an outlier *from*). A code only when a neighbour exists and the best
    cosine is below ``floor``.
    """
    try:
        from .index_db import search_embedding

        vec = embedder.encode(text.strip())
        hits = search_embedding(
            store.kb_dir, query_vec=vec, kinds=("claim",), limit=1, min_score=0.0
        )
    except Exception as e:
        _log.debug("anomaly far_from_corpus skipped: %s", e)
        return None
    if not hits:
        return None
    best = round(float(hits[0][3]), 4)
    if best < floor:
        return {"code": "far_from_corpus", "best_cosine": best, "floor": floor}
    return None


def flag_anomalies(
    store: KBStore,
    *,
    min_evidence: int | None = None,
    contradiction_count: int | None = None,
    far_floor: float | None = None,
) -> list[Anomaly]:
    """Score every pending claim proposal, returning the flagged ones worst-first.

    Read-only: it reads proposals + approved claims (+ the embedding index when
    present) and computes; it writes nothing.
    """
    ev_floor, contra_floor, far = _resolve_cfg(
        store, min_evidence, contradiction_count, far_floor
    )
    approved_live = {
        c.id for c in store.list_claims() if c.status not in _RETIRED
    }
    embedder = _try_embedder()

    out: list[Anomaly] = []
    for pr in store.list_proposals(ProposalStatus.PENDING):
        if pr.kind.value != "claim":
            continue  # the heuristics are claim-shaped
        payload = pr.payload
        reasons: list[dict[str, Any]] = []

        evidence = payload.get("evidence") or []
        if len(evidence) <= ev_floor:
            reasons.append(
                {"code": "thin_evidence", "evidence_count": len(evidence), "floor": ev_floor}
            )

        contra = [
            cid for cid in (payload.get("contradicts") or []) if cid in approved_live
        ]
        if len(contra) >= contra_floor:
            reasons.append(
                {"code": "contradicts_many", "count": len(contra), "ids": contra[:_MAX_IDS]}
            )

        text = payload.get("text")
        if embedder is not None and text and str(text).strip():
            code = _far_from_corpus(store, embedder, str(text), far)
            if code is not None:
                reasons.append(code)

        if reasons:
            out.append(Anomaly(pr.id, pr.kind.value, pr.proposed_by, reasons))

    # worst-first: most reason codes first, id tie-break for deterministic output.
    out.sort(key=lambda a: (-len(a.reasons), a.proposal_id))
    return out


# --- rendering ------------------------------------------------------------


def _reason_str(r: dict[str, Any]) -> str:
    code = r["code"]
    if code == "thin_evidence":
        return f"thin_evidence (cites {r['evidence_count']}, floor {r['floor']})"
    if code == "contradicts_many":
        return f"contradicts_many ({r['count']} approved)"
    if code == "far_from_corpus":
        return f"far_from_corpus (best cosine {r['best_cosine']} < {r['floor']})"
    return code


def render_text(anomalies: list[Anomaly]) -> str:
    out: list[str] = []
    out.append(f"vouch flag-anomalies  ({len(anomalies)} flagged)")
    out.append("")
    if not anomalies:
        out.append("  (no pending proposals look anomalous)")
        return "\n".join(out) + "\n"
    for a in anomalies:
        out.append(f"  {a.proposal_id}  [{a.kind}]  by {a.proposed_by}")
        for r in a.reasons:
            out.append(f"    • {_reason_str(r)}")
    return "\n".join(out) + "\n"
