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
