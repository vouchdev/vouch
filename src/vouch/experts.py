"""kb.experts - rank entities by evidence density on a topic (issue #315).

Read-only aggregation over approved, live claims. Given a free-text topic,
return the entities carrying the most matched evidence, ranked by one of three
weightings (count / recency / citation). It never proposes, writes, or mutates
anything, makes no network or LLM call, and reads only claims already past the
review gate - so the review gate is untouched by construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import index_db
from .models import Claim, ClaimStatus, utcnow
from .salience import _substring_entity_ids
from .storage import KBStore

# A superseded / archived / redacted claim is not live evidence and must never
# inflate an entity ranking (consistent with issue #78).
_EXCLUDED_STATUSES = frozenset(
    {ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED}
)
_VALID_WEIGHTS = frozenset({"count", "recency", "citation"})
_RECENCY_HALF_LIFE_DAYS = 30.0


def _claim_weight(claim: Claim, weight: str, now: datetime) -> float:
    """Per-claim contribution to an entity score under the chosen weighting."""
    if weight == "recency":
        ts = claim.last_confirmed_at or claim.updated_at
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        return 2.0 ** (-age_days / _RECENCY_HALF_LIFE_DAYS)
    if weight == "citation":
        return float(len(set(claim.evidence))) * float(claim.confidence)
    return 1.0  # count


def rank_experts(
    store: KBStore,
    topic: str,
    *,
    limit: int = 10,
    min_claims: int = 1,
    weight: str = "count",
) -> list[dict[str, Any]]:
    """Return entities ranked by evidence density on ``topic``.

    ``weight`` is one of ``count`` | ``recency`` | ``citation``; an unknown
    value falls back to ``count`` (never raises), matching the defensive-config
    style used elsewhere. Ordered by descending score with a stable tie-break
    on ``entity_id``.
    """
    if weight not in _VALID_WEIGHTS:
        weight = "count"

    entities = store.list_entities()
    by_id = {ent.id: ent for ent in entities}
    topic_entity_ids = set(_substring_entity_ids(entities, topic))

    # Candidate claims: FTS hits on the topic, plus every claim that references
    # an entity whose name/alias matches the topic.
    fetch = max(limit * 5, 50)
    fts_claim_ids = {
        cid
        for kind, cid, _snip, _score in index_db.search(store.kb_dir, topic, limit=fetch)
        if kind == "claim"
    }

    now = utcnow()
    counts: dict[str, int] = {}
    citations: dict[str, set[str]] = {}
    scores: dict[str, float] = {}
    top_claims: dict[str, list[tuple[float, str]]] = {}

    for claim in store.list_claims():
        if claim.status in _EXCLUDED_STATUSES:
            continue
        matched = claim.id in fts_claim_ids or bool(
            set(claim.entities) & topic_entity_ids
        )
        if not matched:
            continue
        contrib = _claim_weight(claim, weight, now)
        for eid in claim.entities:
            if eid not in by_id:
                continue  # dangling reference - skip (graph gate should prevent)
            counts[eid] = counts.get(eid, 0) + 1
            citations.setdefault(eid, set()).update(claim.evidence)
            scores[eid] = scores.get(eid, 0.0) + contrib
            top_claims.setdefault(eid, []).append((contrib, claim.id))

    rows: list[dict[str, Any]] = []
    for eid, count in counts.items():
        if count < min_claims:
            continue
        ent = by_id[eid]
        ranked = sorted(top_claims[eid], key=lambda item: (-item[0], item[1]))
        rows.append(
            {
                "entity_id": eid,
                "name": ent.name,
                "type": str(ent.type),
                "claim_count": count,
                "citation_count": len(citations.get(eid, set())),
                "score": round(scores[eid], 6),
                "top_claim_ids": [cid for _w, cid in ranked[:3]],
            }
        )

    rows.sort(key=lambda row: (-row["score"], -row["claim_count"], row["entity_id"]))
    return rows[:limit]
