"""Advisory contradiction scanner: flag approved claims that plausibly conflict.

Heuristic and read-only. Groups approved claims by shared ``Claim.entities``
and scores same-entity pairs whose text overlaps heavily but disagrees in
polarity (one asserts, the other negates) — a cheap proxy for "conflicting
values about the same subject". Nothing here mutates a Claim or writes a
Relation; surviving pairs are filed as ordinary ``contradicts`` relation
proposals via ``proposals.propose_relation``; every eventual edge or
``ClaimStatus.CONTESTED`` transition still requires a human ``kb.approve``
(or the pre-existing manual ``lifecycle.contradict``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import Claim, ClaimStatus, ProposalKind, ProposalStatus, RelationType
from .proposals import ProposalError, propose_relation
from .storage import KBStore

DEFAULT_THRESHOLD = 0.3

# No-longer-live assertions aren't worth flagging against.
_INACTIVE_STATUSES = {ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED}

_NEGATIONS = {
    "not", "no", "never", "none", "without", "isn't", "aren't", "wasn't",
    "weren't", "don't", "doesn't", "didn't", "won't", "wouldn't", "can't",
    "cannot", "couldn't", "shouldn't", "hasn't", "haven't", "hadn't",
}

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "for", "and", "or", "but", "with", "that",
    "this", "it", "as", "at", "by", "from", "into", "than", "then",
}

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _has_negation(text: str) -> bool:
    return bool(_tokens(text) & _NEGATIONS)


def _text_overlap(a: str, b: str) -> float:
    """Jaccard similarity of non-stopword tokens — a cheap same-topic proxy."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True)
class Candidate:
    """One candidate conflicting pair, scored and grouped by shared entity."""

    claim_a: str
    claim_b: str
    entity: str
    score: float


def _entity_groups(
    claims: list[Claim], *, entity: str | None,
) -> dict[str, list[Claim]]:
    groups: dict[str, list[Claim]] = {}
    for c in claims:
        for eid in c.entities:
            if entity is not None and eid != entity:
                continue
            groups.setdefault(eid, []).append(c)
    return groups


def _already_flagged_pairs(store: KBStore) -> set[frozenset[str]]:
    """Pairs that already have a contradiction on record, in any form.

    Covers claims cross-linked via `Claim.contradicts` (set by
    `lifecycle.contradict`), claims already joined by an approved
    `RelationType.CONTRADICTS` edge (e.g. a prior scan's proposal that was
    approved), and pairs with a still-pending `contradicts` relation
    proposal from an earlier scan run. Any one of these means re-proposing
    the pair would be a duplicate.
    """
    flagged: set[frozenset[str]] = set()
    for c in store.list_claims():
        for other in c.contradicts:
            flagged.add(frozenset({c.id, other}))
    for rel in store.list_relations():
        if rel.relation == RelationType.CONTRADICTS:
            flagged.add(frozenset({rel.source, rel.target}))
    for p in store.list_proposals(ProposalStatus.PENDING):
        if p.kind == ProposalKind.RELATION and p.payload.get("relation") == "contradicts":
            src = p.payload.get("source")
            target = p.payload.get("target")
            if src and target:
                flagged.add(frozenset({src, target}))
    return flagged


def find_candidates(
    store: KBStore, *,
    threshold: float = DEFAULT_THRESHOLD,
    entity: str | None = None,
) -> list[Candidate]:
    """Read-only scan for candidate conflicting claim pairs.

    Never mutates a Claim, writes a Relation, or touches `Claim.contradicts`.
    """
    claims = [c for c in store.list_claims() if c.status not in _INACTIVE_STATUSES]
    groups = _entity_groups(claims, entity=entity)
    flagged = _already_flagged_pairs(store)

    best: dict[frozenset[str], Candidate] = {}
    for eid, members in groups.items():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = frozenset({a.id, b.id})
                if pair in flagged:
                    continue
                # Same-topic + opposite polarity is the conflict signal;
                # same-topic + same polarity is corroboration or a near-dupe
                # (embeddings/dedup.py's job), not a contradiction.
                if _has_negation(a.text) == _has_negation(b.text):
                    continue
                score = _text_overlap(a.text, b.text)
                if score < threshold:
                    continue
                existing = best.get(pair)
                if existing is None or score > existing.score:
                    best[pair] = Candidate(
                        claim_a=a.id, claim_b=b.id, entity=eid, score=score,
                    )
    return sorted(best.values(), key=lambda c: c.score, reverse=True)


def scan(
    store: KBStore, *,
    threshold: float = DEFAULT_THRESHOLD,
    entity: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    proposed_by: str = "vouch-contradict-scan",
) -> list[dict[str, Any]]:
    """Scan for candidate pairs; file a `contradicts` proposal per pair unless dry-run.

    Dry-run (the default) never writes anything. Otherwise each surviving
    pair produces exactly one pending relation proposal via
    `proposals.propose_relation` — visible in `kb.list_pending`, decided only
    by a human `kb.approve`.
    """
    candidates = find_candidates(store, threshold=threshold, entity=entity)
    if limit is not None:
        candidates = candidates[:limit]

    rows: list[dict[str, Any]] = []
    for c in candidates:
        row: dict[str, Any] = {
            "claim_a": c.claim_a, "claim_b": c.claim_b,
            "entity": c.entity, "score": c.score,
        }
        if not dry_run:
            try:
                proposal = propose_relation(
                    store,
                    src=c.claim_a,
                    relation=RelationType.CONTRADICTS.value,
                    target=c.claim_b,
                    proposed_by=proposed_by,
                    rationale=(
                        f"contradict-scan: shared entity {c.entity!r}, "
                        f"score={c.score:.3f}"
                    ),
                )
            except ProposalError:
                # Endpoint vanished or a proposal landed mid-scan (race with
                # another run) — skip rather than fail the whole batch.
                continue
            row["proposal_id"] = proposal.id
        rows.append(row)
    return rows
