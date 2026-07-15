"""Mechanical receipt verification — the atom the fidelity pivot rests on.

A *receipt* is the byte-offset span carried by an ``Evidence``: the half-open
range ``[byte_start, byte_end)`` into the cited source's raw bytes, paired with
the ``quote`` those bytes are claimed to spell. Verifying a receipt is a pure
string comparison — decode the source slice, compare it to the quote. No LLM,
no judge, no heuristic. The quoted span is in the source at those offsets or it
is not.

This is what lets the review gate become arithmetic instead of a person: a
citation whose receipt verifies can be auto-approved, one whose receipt is
forged can be auto-rejected, and one that carries no receipt at all is not a
receipt-backed citation and cannot be trusted mechanically. Keeping the three
states distinct is deliberate — the gate treats them differently.

Byte offsets, not character offsets: sources are stored as raw content-addressed
bytes, and under UTF-8 a character index diverges from a byte index the moment a
multi-byte codepoint appears. The receipt indexes the artifact that was actually
hashed and persisted, so it must be expressed in that artifact's own units.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .models import Evidence

if TYPE_CHECKING:
    from .storage import KBStore


class ReceiptStatus(StrEnum):
    VERIFIED = "verified"  # the quote is exactly the source bytes at the span
    FORGED = "forged"  # a span is claimed, but it does not spell the quote
    NO_RECEIPT = "no_receipt"  # no byte-offset span to verify against


@dataclass(frozen=True)
class ReceiptResult:
    status: ReceiptStatus
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status is ReceiptStatus.VERIFIED


def verify_receipt(evidence: Evidence, source_bytes: bytes) -> ReceiptResult:
    """Check ``evidence``'s byte-offset receipt against the source's raw bytes.

    Returns ``NO_RECEIPT`` when the evidence carries no verifiable span (either
    offset missing, or no quote to compare), ``FORGED`` when a span is claimed
    but the bytes there do not decode to the quote (out of range, inverted,
    split codepoint, or plain mismatch all land here), and ``VERIFIED`` only
    when the decoded span equals the quote exactly.
    """
    start, end, quote = evidence.byte_start, evidence.byte_end, evidence.quote
    if start is None or end is None or quote is None:
        return ReceiptResult(ReceiptStatus.NO_RECEIPT, "no byte-offset span")
    if start > end or end > len(source_bytes):
        return ReceiptResult(
            ReceiptStatus.FORGED,
            f"span [{start}:{end}) out of range for {len(source_bytes)} bytes",
        )
    try:
        span = source_bytes[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return ReceiptResult(ReceiptStatus.FORGED, "span does not decode as utf-8")
    if span != quote:
        return ReceiptResult(ReceiptStatus.FORGED, "span does not match quote")
    return ReceiptResult(ReceiptStatus.VERIFIED)


def locate_span(source_bytes: bytes, quote: str) -> tuple[int, int] | None:
    """Byte offsets of the first exact occurrence of ``quote``, or None.

    The quote step of the retrieve-then-quote loop: a claim earns a receipt
    only if its quoted text appears verbatim in the source's raw bytes. The
    match is exact and case-sensitive by design — no normalization, no fuzzy
    match — because the receipt's whole value is that it is checkable by
    ``==``. A paraphrase does not appear verbatim, so it returns None and the
    caller drops the claim. Returns the first occurrence for determinism.
    """
    needle = quote.encode("utf-8")
    if not needle:
        return None
    idx = source_bytes.find(needle)
    if idx < 0:
        return None
    return (idx, idx + len(needle))


def _span_evidence_id(source_id: str, start: int, end: int) -> str:
    """A content-addressed id for a source span, so intake is idempotent.

    The same span in the same source always yields the same id — re-filing it
    reuses the existing Evidence rather than duplicating it — while distinct
    spans get distinct ids.
    """
    digest = hashlib.sha256(f"{source_id}:{start}:{end}".encode()).hexdigest()
    return f"ev-{digest[:16]}"


def receipt_for_quote(
    *,
    source_id: str,
    source_bytes: bytes,
    quote: str,
    evidence_id: str | None = None,
) -> Evidence | None:
    """Build an ``Evidence`` carrying a verifying receipt, or None to drop.

    Locates ``quote`` in ``source_bytes`` and, if found, returns an Evidence
    whose byte-offset receipt is guaranteed to verify against those same bytes.
    Returns None when the quote is not present verbatim — the mechanical form
    of "drops any claim it cannot quote." When ``evidence_id`` is omitted, a
    content-addressed id derived from the span is minted.
    """
    span = locate_span(source_bytes, quote)
    if span is None:
        return None
    start, end = span
    return Evidence(
        id=evidence_id or _span_evidence_id(source_id, start, end),
        source_id=source_id,
        locator=f"b{start}-{end}",
        quote=quote,
        byte_start=start,
        byte_end=end,
    )


@dataclass(frozen=True)
class ClaimReceiptVerdict:
    """Whether a claim's citations clear the mechanical gate.

    ``approve`` is True only when the claim cites at least one thing and every
    citation is a receipt that VERIFIES. This is the input to phase d's gate:
    receipt verifies -> auto-approve; anything else -> reject, with ``reasons``
    naming each citation that failed and why.
    """

    approve: bool
    reasons: tuple[str, ...] = ()


def evaluate_claim_receipts(
    store: KBStore, evidence_ids: list[str]
) -> ClaimReceiptVerdict:
    """Verdict on a claim's citations for the mechanical gate.

    Each citation must resolve to an Evidence whose receipt verifies. A bare
    source id (or unknown id) carries no byte-offset receipt and is rejected as
    such; a claim that cites nothing is rejected. No LLM, no judge — the verdict
    is the conjunction of per-citation string comparisons.
    """
    from .storage import ArtifactNotFoundError

    if not evidence_ids:
        return ClaimReceiptVerdict(False, ("claim cites nothing",))
    reasons: list[str] = []
    for eid in evidence_ids:
        try:
            evidence = store.get_evidence(eid)
        except ArtifactNotFoundError:
            reasons.append(f"{eid}: no receipt (bare source or unknown id)")
            continue
        result = verify_evidence(store, evidence)
        if result.status is not ReceiptStatus.VERIFIED:
            reasons.append(f"{eid}: {result.status.value}")
    return ClaimReceiptVerdict(not reasons, tuple(reasons))


def verify_evidence(store: KBStore, evidence: Evidence) -> ReceiptResult:
    """Verify ``evidence``'s receipt against its source's stored bytes.

    Loads the raw source content from the KB and delegates to
    ``verify_receipt``. A receipt whose source is not in the KB cannot be
    checked at all, which is never the same as verified — the gate must reject
    it, so it is reported ``FORGED`` with the reason rather than raising.
    """
    from .storage import ArtifactNotFoundError

    if evidence.byte_start is None or evidence.byte_end is None or evidence.quote is None:
        return ReceiptResult(ReceiptStatus.NO_RECEIPT, "no byte-offset span")
    try:
        source_bytes = store.read_source_content(evidence.source_id)
    except ArtifactNotFoundError:
        return ReceiptResult(
            ReceiptStatus.FORGED, f"source {evidence.source_id} not in kb"
        )
    return verify_receipt(evidence, source_bytes)
