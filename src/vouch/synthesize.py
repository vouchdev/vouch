"""Answer-mode synthesis over the review-gated KB.

`kb.context` returns a *ranked list* of relevant items; `kb.synthesize`
answers a query in prose, but only from APPROVED (durable) claims, with an
inline `[claim_id]` citation behind every sentence. It never invents a
sentence that isn't traceable to a claim, reports the query topics it found
no claim for in an explicit `gaps` block, and grades its own confidence from
the lifecycle status of the claims it cited.

The synthesis is deterministic in v1 — there is no LLM in the loop. The
`llm` flag is reserved so the wire shape is stable when an opt-in generative
backend lands; passing `llm=True` raises rather than silently degrading.
"""

from __future__ import annotations

from typing import Any, Literal

from .context import build_context_pack
from .models import Claim, ClaimStatus
from .storage import ArtifactNotFoundError, KBStore

Confidence = Literal["high", "medium", "low"]

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
        "from", "how", "in", "into", "is", "it", "its", "of", "on", "or",
        "the", "their", "them", "then", "there", "these", "this", "to", "was",
        "were", "what", "when", "where", "which", "who", "why", "will", "with",
        "you", "your",
    }
)


def _salient_terms(query: str) -> list[str]:
    """Lowercased, de-duplicated, order-preserving content words of the query."""
    seen: set[str] = set()
    terms: list[str] = []
    for raw in query.split():
        token = "".join(ch for ch in raw.lower() if ch.isalnum())
        if len(token) < 3 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _clause(text: str) -> str:
    """One short, single-clause rendering of a claim's text."""
    clause = text.strip().split("\n", 1)[0].strip()
    for sep in (". ", "; ", " — ", " - "):
        head = clause.split(sep, 1)[0]
        if head:
            clause = head
    clause = clause.rstrip(".;,")
    return clause


def _covers(term: str, *claims: Claim) -> bool:
    return any(term in c.text.lower() for c in claims)


def _confidence(statuses: list[ClaimStatus]) -> Confidence:
    if any(s == ClaimStatus.CONTESTED for s in statuses):
        return "low"
    if any(s in (ClaimStatus.WORKING, ClaimStatus.ACTIONABLE) for s in statuses):
        return "medium"
    if statuses and all(s == ClaimStatus.STABLE for s in statuses):
        return "high"
    return "medium"


def synthesize(
    store: KBStore,
    *,
    query: str,
    depth: int = 3,
    max_chars: int = 4000,
    llm: bool = False,
) -> dict[str, Any]:
    """Answer `query` from approved claims only, with inline citations.

    Returns a dict with `query`, `answer` (citation-bearing prose, possibly
    empty), `claims` (the cited claim ids), `gaps` (query topics no approved
    claim covered) and `_meta.synthesis_confidence`.
    """
    if llm:
        raise ValueError(
            "llm synthesis backend not configured; "
            "deterministic synthesis is the default"
        )

    pack = build_context_pack(store, query=query, limit=depth)
    items = pack["items"] if isinstance(pack, dict) else pack.items

    approved: list[Claim] = []
    seen_ids: set[str] = set()
    for item in items:
        if (item["type"] if isinstance(item, dict) else item.type) != "claim":
            continue
        cid = item["id"] if isinstance(item, dict) else item.id
        if cid in seen_ids:
            continue
        try:
            claim = store.get_claim(cid)
        except ArtifactNotFoundError:
            continue
        seen_ids.add(cid)
        approved.append(claim)

    sentences: list[str] = []
    cited: list[str] = []
    statuses: list[ClaimStatus] = []
    used = 0
    for claim in approved:
        sentence = f"{_clause(claim.text)} [{claim.id}]."
        projected = used + len(sentence) + (1 if sentences else 0)
        if projected > max_chars:
            break
        sentences.append(sentence)
        cited.append(claim.id)
        statuses.append(claim.status)
        used = projected

    answer = " ".join(sentences)
    cited_claims = [c for c in approved if c.id in set(cited)]
    gaps = [
        term
        for term in _salient_terms(query)
        if not (cited_claims and _covers(term, *cited_claims))
    ]

    return {
        "query": query,
        "answer": answer,
        "claims": cited,
        "gaps": gaps,
        "_meta": {"synthesis_confidence": _confidence(statuses)},
    }
