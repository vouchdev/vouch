"""Answer-mode synthesis over the review-gated KB.

`kb.context` returns a *ranked list* of relevant items; `kb.synthesize`
answers a query in prose, but only from APPROVED (durable) claims, with an
inline `[claim_id]` citation behind every sentence. It never invents a
sentence that isn't traceable to a claim, reports the query topics it found
no claim for in an explicit `gaps` block, and grades its own confidence from
the lifecycle status of the claims it cited.

The default synthesis is deterministic — no LLM in the loop. `llm=True`
opts into the generative backend: the deployment-configured LLM command
(``compile.llm_cmd``, the same one the wiki compiler uses) drafts prose
grounded in retrieved *pages* and approved claims. The division of labor
stays llm-wiki's: the LLM writes, code verifies — every ``[id]`` citation
in the draft must resolve to a source that was actually offered, or it is
stripped; an answer left with no verifiable citation is returned empty
rather than uncited. Requesting `llm=True` without a configured command
raises rather than silently degrading.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from . import llm_draft
from .context import build_context_pack
from .models import Claim, ClaimStatus, Page, PageStatus
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


# LLM-backend grounding budgets. Pages are the product (llm-wiki): they get
# the deep budget; claims ground the sentences pages haven't compiled yet.
_LLM_PAGE_LIMIT = 6
_LLM_CLAIM_LIMIT = 12
_LLM_PAGE_CHARS = 4000

# Bracketed tokens in the draft. Anything bracketed that is not an offered
# source id is stripped — the model must not mint provenance.
_MARKER_RE = re.compile(r"\s*\[([^\[\]]+)\]")


def _llm_grounding(
    store: KBStore, query: str, depth: int
) -> tuple[list[Page], list[Claim]]:
    """Retrieve the pages and approved claims the LLM may cite.

    Retrieval-first via the context pack; when the index surfaces no page for
    the query, fall back to the most recently updated pages so the chat still
    checks the wiki on small or sparsely-indexed KBs.
    """
    pack = build_context_pack(store, query=query, limit=max(depth * 4, 12))
    items = pack["items"] if isinstance(pack, dict) else pack.items
    page_ids: list[str] = []
    claim_ids: list[str] = []
    for item in items:
        kind = item["type"] if isinstance(item, dict) else item.type
        iid = item["id"] if isinstance(item, dict) else item.id
        bucket = page_ids if kind == "page" else claim_ids if kind == "claim" else None
        if bucket is not None and iid not in bucket:
            bucket.append(iid)

    pages: list[Page] = []
    for pid in page_ids:
        if len(pages) >= _LLM_PAGE_LIMIT:
            break
        try:
            page = store.get_page(pid)
        except ArtifactNotFoundError:
            continue
        if page.status != PageStatus.ARCHIVED:
            pages.append(page)
    if not pages:
        live = [p for p in store.list_pages() if p.status != PageStatus.ARCHIVED]
        live.sort(key=lambda p: p.updated_at, reverse=True)
        pages = live[:_LLM_PAGE_LIMIT]

    claims: list[Claim] = []
    for cid in claim_ids:
        if len(claims) >= _LLM_CLAIM_LIMIT:
            break
        try:
            claims.append(store.get_claim(cid))
        except ArtifactNotFoundError:
            continue
    return pages, claims


def _llm_prompt(
    query: str, pages: list[Page], claims: list[Claim], max_chars: int
) -> str:
    lines = [
        "You answer a question from a review-gated knowledge base.",
        "Use ONLY the sources below — never outside knowledge.",
        "After every sentence, cite the id of the supporting source in square "
        "brackets, e.g. [some-id]. Only ids listed below count as citations.",
        "If part of the question is not covered by the sources, do not guess — "
        "name that topic in gaps instead.",
        f"Keep the answer under {max_chars} characters.",
        "Output exactly one JSON object and nothing else, no code fence:",
        '{"answer": "…", "gaps": ["uncovered topic", "…"]}',
        "",
        f"Question: {query}",
        "",
        "Pages:",
    ]
    for page in pages:
        body = page.body.strip()
        if len(body) > _LLM_PAGE_CHARS:
            body = body[:_LLM_PAGE_CHARS] + " …"
        lines.append(f"[{page.id}] {page.title}\n{body}\n")
    if not pages:
        lines.append("(none)")
    lines.append("Claims:")
    for claim in claims:
        lines.append(f"[{claim.id}] ({claim.status.value}) {claim.text}")
    if not claims:
        lines.append("(none)")
    return "\n".join(lines)


def _llm_synthesize(
    store: KBStore, *, query: str, depth: int, max_chars: int
) -> dict[str, Any]:
    # The LLM command is deployment config shared with the wiki compiler —
    # one knob (compile.llm_cmd) turns on every generative feature.
    from .compile import load_config

    cfg = load_config(store)
    if not cfg.llm_cmd:
        raise ValueError(
            "llm synthesis is not configured — set compile.llm_cmd in "
            '.vouch/config.yaml, e.g.\ncompile:\n  llm_cmd: "claude -p --model sonnet"'
        )

    pages, claims = _llm_grounding(store, query, depth)
    if not pages and not claims:
        return {
            "query": query,
            "answer": "",
            "claims": [],
            "pages": [],
            "gaps": _salient_terms(query),
            "_meta": {
                "synthesis_confidence": _confidence([]),
                "synthesis_backend": "llm",
            },
        }

    prompt = _llm_prompt(query, pages, claims, max_chars)
    try:
        raw = llm_draft.run_llm(
            cfg.llm_cmd, prompt,
            timeout_seconds=cfg.timeout_seconds, label="compile.llm_cmd",
        )
        data = llm_draft.parse_object(raw, noun="synthesis")
    except llm_draft.LLMDraftError as e:
        raise ValueError(str(e)) from e

    answer = str(data.get("answer") or "").strip()
    gaps = [str(g) for g in data.get("gaps") or [] if str(g).strip()]

    # Code verifies what the model drafted: strip any bracketed token that is
    # not an offered source id, then truncate to budget on a citation boundary
    # so no dangling half-citation survives.
    offered = {p.id for p in pages} | {c.id for c in claims}
    dropped: list[str] = []

    def _keep(m: re.Match[str]) -> str:
        if m.group(1) in offered:
            return m.group(0)
        dropped.append(m.group(1))
        return ""

    answer = _MARKER_RE.sub(_keep, answer)
    answer = re.sub(r" +([.,;:])", r"\1", re.sub(r" {2,}", " ", answer)).strip()
    if len(answer) > max_chars:
        cut = answer.rfind("]", 0, max_chars)
        answer = answer[: cut + 1] if cut > 0 else answer[:max_chars]

    cited = [m.group(1) for m in _MARKER_RE.finditer(answer)]
    cited = list(dict.fromkeys(cited))
    if not cited:
        # an uncited draft is a guess — the KB stays silent instead
        answer = ""
        gaps = gaps or _salient_terms(query)

    claim_by_id = {c.id: c for c in claims}
    cited_claims = [cid for cid in cited if cid in claim_by_id]
    cited_pages = [cid for cid in cited if cid not in claim_by_id]
    meta: dict[str, Any] = {
        "synthesis_confidence": _confidence(
            [claim_by_id[cid].status for cid in cited_claims]
        ),
        "synthesis_backend": "llm",
    }
    if dropped:
        meta["dropped_citations"] = dropped
    return {
        "query": query,
        "answer": answer,
        "claims": cited_claims,
        "pages": cited_pages,
        "gaps": gaps,
        "_meta": meta,
    }


def synthesize(
    store: KBStore,
    *,
    query: str,
    depth: int = 3,
    max_chars: int = 4000,
    llm: bool = False,
) -> dict[str, Any]:
    """Answer `query` from the review-gated KB, with inline citations.

    Returns a dict with `query`, `answer` (citation-bearing prose, possibly
    empty), `claims` (the cited claim ids), `pages` (cited page ids — always
    empty on the deterministic path), `gaps` (query topics no source covered)
    and `_meta.synthesis_confidence`. With `llm=True` the answer is drafted
    by the deployment-configured LLM grounded in pages and approved claims;
    every citation is still verified mechanically.
    """
    if llm:
        return _llm_synthesize(
            store, query=query, depth=depth, max_chars=max_chars,
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
        "pages": [],
        "gaps": gaps,
        "_meta": {
            "synthesis_confidence": _confidence(statuses),
            "synthesis_backend": "deterministic",
        },
    }
