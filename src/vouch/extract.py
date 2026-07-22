"""Source → receipt-backed claims: the capture step that needs no human.

Segments an ingested source into quotable spans and files each as a claim whose
citation is a byte-offset receipt into that source. Every claim therefore quotes
its source verbatim — the same mechanical property Phase D's gate auto-approves
(see ``proposals.auto_approve_receipts``). Extraction is deterministic and
llm-free by default: the receipt check is the guardrail, so a span that isn't
verbatim in the source is dropped rather than trusted, and the pipeline runs in
the base install and under test with no external command.

``ingest_source`` is the whole loop the user asked to see — store a document,
extract receipt-backed claims, and (when the gate is on) auto-approve them —
so "run vouch and it just captures the knowledge, no review" is one call.
"""

from __future__ import annotations

import re

from .models import Claim, Source
from .proposals import (
    ProposeClaimResult,
    auto_approve_receipts,
    default_scope,
    propose_quoted_claim,
)
from .storage import KBStore

# Split on newlines and sentence-ending punctuation. Each match is kept as an
# exact substring of the source so its receipt verifies; only surrounding
# whitespace is stripped (the inner run stays contiguous in the source).
_SEGMENT_RE = re.compile(r"[^\n.!?]+[.!?]?")

DEFAULT_MIN_CHARS = 16
DEFAULT_MAX_CHARS = 320
# A span that is mostly punctuation/markup rather than prose is noise, not a
# claim (rule lines, list bullets, symbol art). Require at least this share of
# the characters to be letters.
_MIN_LETTER_RATIO = 0.5


def _is_noise(segment: str) -> bool:
    letters = sum(c.isalpha() for c in segment)
    return letters < len(segment) * _MIN_LETTER_RATIO


# Words that carry no fact: a span made mostly of them is filler, not
# knowledge. Kept local (not imported from synthesize) so the ingest hot path
# stays clear of the synthesis/LLM import chain.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
        "from", "how", "in", "into", "is", "it", "its", "of", "on", "or",
        "that", "the", "their", "them", "then", "there", "these", "this", "to",
        "was", "were", "what", "when", "where", "which", "who", "why", "will",
        "with", "you", "your",
    }
)


def _content_words(span: str) -> list[str]:
    """Lowercased alphanumeric tokens of ``span`` minus short words/stopwords."""
    words: list[str] = []
    for raw in span.split():
        token = "".join(ch for ch in raw.lower() if ch.isalnum())
        if len(token) < 3 or token in _STOPWORDS:
            continue
        words.append(token)
    return words


def _span_score(span: str, doc_freq: dict[str, int]) -> float:
    """Information density of ``span``: sum over its distinct content words of
    ``1 / document-frequency``. A word repeated across many spans is less
    discriminating, so it counts for less; a rare, specific term counts for
    more. Stopword-heavy filler scores near zero.
    """
    return sum(1.0 / doc_freq.get(word, 1) for word in set(_content_words(span)))


def select_spans(
    segments: list[str],
    *,
    max_claims: int | None = None,
    budget_chars: int | None = None,
) -> list[str]:
    """Keep the most information-dense spans under a budget, in source order.

    Ranking is deterministic and llm-free (see ``_span_score``): fact-dense
    sentences outrank stopword-heavy filler. With no budget the input is
    returned unchanged — the unbudgeted baseline that captures every span.
    Selection only ever returns a *subset* of ``segments``, never a rewrite, so
    every kept span is still a verbatim substring of the source and its receipt
    still verifies by construction.
    """
    if max_claims is None and budget_chars is None:
        return segments
    doc_freq: dict[str, int] = {}
    for seg in segments:
        for word in set(_content_words(seg)):
            doc_freq[word] = doc_freq.get(word, 0) + 1
    # Rank by score (desc), ties broken by original position for determinism.
    ranked = sorted(
        range(len(segments)),
        key=lambda i: (-_span_score(segments[i], doc_freq), i),
    )
    kept: list[int] = []
    used = 0
    for i in ranked:
        if max_claims is not None and len(kept) >= max_claims:
            break
        length = len(segments[i])
        if budget_chars is not None and used + length > budget_chars:
            continue
        kept.append(i)
        used += length
    return [segments[i] for i in sorted(kept)]


def segment_source(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """Deterministic split into candidate claim spans, each a verbatim substring.

    Order-preserving and de-duplicated. Drops fragments that are too short, too
    long, or mostly markup. Whitespace-stripping is safe for the receipt: the
    stripped span is still a contiguous byte run in the source.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _SEGMENT_RE.finditer(text):
        segment = match.group().strip()
        if not (min_chars <= len(segment) <= max_chars):
            continue
        if _is_noise(segment) or segment in seen:
            continue
        seen.add(segment)
        out.append(segment)
    return out


def extract_receipt_claims(
    store: KBStore,
    source_id: str,
    *,
    proposed_by: str,
    limit: int | None = None,
    max_claims: int | None = None,
    budget_chars: int | None = None,
) -> list[ProposeClaimResult]:
    """File a receipt-backed claim for each quotable span in ``source_id``.

    Each segment becomes a claim that quotes itself verbatim, so its receipt
    verifies by construction. A segment that (defensively) fails the verbatim
    check — e.g. non-UTF-8 bytes mangled on decode — is dropped by
    ``propose_quoted_claim``. Returns the proposals actually filed.

    ``max_claims``/``budget_chars`` turn on density selection (``select_spans``):
    only the most informative spans are filed, so ingest keeps the facts worth a
    claim and drops filler instead of restating the whole document. ``limit`` is
    the older positional cap (first-N in document order, used by session-answer
    capture) and still applies after selection.
    """
    text = store.read_source_content(source_id).decode("utf-8", errors="replace")
    spans = segment_source(text)
    if max_claims is not None or budget_chars is not None:
        spans = select_spans(spans, max_claims=max_claims, budget_chars=budget_chars)
    filed: list[ProposeClaimResult] = []
    for segment in spans:
        if limit is not None and len(filed) >= limit:
            break
        result = propose_quoted_claim(
            store, text=segment, source_id=source_id, quote=segment,
            proposed_by=proposed_by,
        )
        if result is not None:
            filed.append(result)
    return filed


def ingest_source(
    store: KBStore,
    content: bytes,
    *,
    proposed_by: str,
    title: str | None = None,
    auto_approve: bool = True,
    max_claims: int | None = None,
    budget_chars: int | None = None,
) -> tuple[Source, list[Claim]]:
    """Run the whole capture loop on a document, no human in the loop.

    Store ``content`` as a source, extract receipt-backed claims from it, and
    (when ``auto_approve`` and ``review.auto_approve_on_receipt`` are both on)
    approve every one whose receipt verifies. Returns the source and the claims
    that became durable. With the gate off the claims are filed but left pending
    for a human — the review gate is never silently bypassed.

    ``max_claims``/``budget_chars`` bound the capture to the most informative
    spans (see ``extract_receipt_claims``); unset, every quotable span is kept.
    """
    source = store.put_source(content, title=title, scope=default_scope(store))
    extract_receipt_claims(
        store, source.id, proposed_by=proposed_by,
        max_claims=max_claims, budget_chars=budget_chars,
    )
    approved = auto_approve_receipts(store) if auto_approve else []
    return source, approved
