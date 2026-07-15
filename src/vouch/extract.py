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
) -> list[ProposeClaimResult]:
    """File a receipt-backed claim for each quotable span in ``source_id``.

    Each segment becomes a claim that quotes itself verbatim, so its receipt
    verifies by construction. A segment that (defensively) fails the verbatim
    check — e.g. non-UTF-8 bytes mangled on decode — is dropped by
    ``propose_quoted_claim``. Returns the proposals actually filed.
    """
    text = store.read_source_content(source_id).decode("utf-8", errors="replace")
    filed: list[ProposeClaimResult] = []
    for segment in segment_source(text):
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
) -> tuple[Source, list[Claim]]:
    """Run the whole capture loop on a document, no human in the loop.

    Store ``content`` as a source, extract receipt-backed claims from it, and
    (when ``auto_approve`` and ``review.auto_approve_on_receipt`` are both on)
    approve every one whose receipt verifies. Returns the source and the claims
    that became durable. With the gate off the claims are filed but left pending
    for a human — the review gate is never silently bypassed.
    """
    source = store.put_source(content, title=title)
    extract_receipt_claims(store, source.id, proposed_by=proposed_by)
    approved = auto_approve_receipts(store) if auto_approve else []
    return source, approved
