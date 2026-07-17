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

# A sentence boundary is punctuation followed by whitespace (or a newline).
# A bare dot is NOT a boundary: periods inside "1.2.0", "cli.py:2550", or a
# markdown link have no trailing whitespace, and splitting on them minted
# mid-token shards like "0`, and let release." as claims.
_BOUNDARY_RE = re.compile(r"[.!?]+(?=\s)|\n")

DEFAULT_MIN_CHARS = 16
DEFAULT_MAX_CHARS = 320
# A span that is mostly punctuation/markup rather than prose is noise, not a
# claim (rule lines, list bullets, symbol art). Require at least this share of
# the characters to be letters.
_MIN_LETTER_RATIO = 0.5

_OPENING_CHARS = "\"'`([{"
_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


def _split_spans(text: str) -> list[str]:
    """Slice text at sentence boundaries; every span is a verbatim substring."""
    spans: list[str] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(text):
        spans.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        spans.append(text[start:])
    return spans


def _is_noise(segment: str) -> bool:
    letters = sum(c.isalpha() for c in segment)
    return letters < len(segment) * _MIN_LETTER_RATIO


def _is_claimworthy(segment: str) -> bool:
    """A span must read as a proposition, not a shard of mangled markup.

    Dangling backticks/brackets and punctuation-led starts are the signature
    of a segment cut mid-construct; single- or two-word spans carry no
    proposition. This gate is what keeps "receipt verifies" from being the
    only bar a claim has to clear.
    """
    first = segment[0]
    if not (first.isalnum() or first in _OPENING_CHARS):
        return False
    if segment.count("`") % 2:
        return False
    if any(segment.count(o) != segment.count(c) for o, c in _BRACKET_PAIRS):
        return False
    return len(segment.split()) >= 3


def segment_source(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """Deterministic split into candidate claim spans, each a verbatim substring.

    Order-preserving and de-duplicated. Drops fragments that are too short,
    too long, mostly markup, or not claim-worthy (unbalanced markup,
    punctuation-led, fewer than three words). Whitespace-stripping is safe for
    the receipt: the stripped span is still a contiguous byte run in the source.
    """
    seen: set[str] = set()
    out: list[str] = []
    for span in _split_spans(text):
        segment = span.strip()
        if not segment or not (min_chars <= len(segment) <= max_chars):
            continue
        if _is_noise(segment) or not _is_claimworthy(segment) or segment in seen:
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
