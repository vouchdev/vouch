"""Mechanical receipt verification — the atom of the fidelity pivot.

A byte-offset receipt is the pair (byte_start, byte_end) on an Evidence: the
half-open byte range into the cited Source's raw bytes that the recorded
``quote`` was taken from. Verification is pure string comparison against the
stored source bytes — no LLM, no judge. Either the quoted span is in the
source at those offsets or it is not.
"""

from __future__ import annotations

import pytest

from vouch.models import Evidence
from vouch.receipts import (
    ReceiptStatus,
    evaluate_claim_receipts,
    locate_span,
    receipt_for_quote,
    verify_evidence,
    verify_receipt,
)
from vouch.storage import KBStore


def _ev(source_id: str = "s1", **kw: object) -> Evidence:
    kw.setdefault("id", "e1")
    kw.setdefault("locator", "receipt")
    return Evidence(source_id=source_id, **kw)  # type: ignore[arg-type]


def test_evidence_carries_byte_offset_receipt() -> None:
    ev = Evidence(
        id="e1",
        source_id="s1",
        locator="b4-15",
        quote="quick brown",
        byte_start=4,
        byte_end=15,
    )
    assert ev.byte_start == 4
    assert ev.byte_end == 15
    # the receipt must survive a json round-trip (it is persisted to yaml)
    round_tripped = Evidence.model_validate(ev.model_dump(mode="json"))
    assert round_tripped.byte_start == 4
    assert round_tripped.byte_end == 15


def test_evidence_without_offsets_defaults_to_no_receipt() -> None:
    ev = Evidence(id="e1", source_id="s1", locator="L10-L20")
    assert ev.byte_start is None
    assert ev.byte_end is None


SOURCE = b"the quick brown fox jumps over the lazy dog"


def test_receipt_verified_when_quote_matches_span() -> None:
    # "quick brown" occupies bytes [4:15)
    ev = _ev(quote="quick brown", byte_start=4, byte_end=15)
    result = verify_receipt(ev, SOURCE)
    assert result.status is ReceiptStatus.VERIFIED
    assert result.verified is True


def test_receipt_forged_when_quote_does_not_match_span() -> None:
    # the span [4:15) really says "quick brown", not "lazy sloth"
    ev = _ev(quote="lazy sloth", byte_start=4, byte_end=15)
    result = verify_receipt(ev, SOURCE)
    assert result.status is ReceiptStatus.FORGED
    assert result.verified is False


def test_receipt_forged_when_offsets_out_of_range() -> None:
    ev = _ev(quote="whatever", byte_start=40, byte_end=999)
    result = verify_receipt(ev, SOURCE)
    assert result.status is ReceiptStatus.FORGED
    assert result.verified is False


def test_receipt_forged_when_offsets_inverted() -> None:
    ev = _ev(quote="brown", byte_start=15, byte_end=4)
    result = verify_receipt(ev, SOURCE)
    assert result.status is ReceiptStatus.FORGED


def test_no_receipt_when_offsets_absent() -> None:
    ev = _ev(quote="quick brown")  # a quote, but no byte offsets
    result = verify_receipt(ev, SOURCE)
    assert result.status is ReceiptStatus.NO_RECEIPT
    assert result.verified is False


def test_no_receipt_when_quote_absent() -> None:
    # offsets but nothing claimed to be there — nothing to string-compare
    ev = _ev(byte_start=4, byte_end=15)
    result = verify_receipt(ev, SOURCE)
    assert result.status is ReceiptStatus.NO_RECEIPT


def test_receipt_uses_byte_offsets_not_char_offsets() -> None:
    # "café — au lait": 'é' is 2 bytes (0xc3 0xa9), '—' is 3 bytes (em dash).
    # "au lait" starts at char index 7 but byte index 10. A char-offset
    # verifier would slice the wrong span; a byte-offset verifier is correct.
    source = "café — au lait".encode()
    assert source[10:17] == b"au lait"
    ev = _ev(quote="au lait", byte_start=10, byte_end=17)
    assert verify_receipt(ev, source).status is ReceiptStatus.VERIFIED
    # the char-offset [7:14) would grab "— au l" region -> forged
    wrong = _ev(quote="au lait", byte_start=7, byte_end=14)
    assert verify_receipt(wrong, source).status is ReceiptStatus.FORGED


def test_receipt_forged_when_span_splits_a_codepoint() -> None:
    source = "é".encode()  # two bytes 0xc3 0xa9
    ev = _ev(quote="é", byte_start=0, byte_end=1)  # half a codepoint
    # undecodable span cannot match any quote -> forged, never a crash
    assert verify_receipt(ev, source).status is ReceiptStatus.FORGED


@pytest.fixture
def store(tmp_path, monkeypatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def test_verify_evidence_loads_source_bytes_and_verifies(store: KBStore) -> None:
    src = store.put_source(b"the quick brown fox", title="t")
    ev = store.put_evidence(
        Evidence(
            id="e1",
            source_id=src.id,
            locator="b4-15",
            quote="quick brown",
            byte_start=4,
            byte_end=15,
        )
    )
    assert verify_evidence(store, ev).status is ReceiptStatus.VERIFIED


def test_verify_evidence_forged_against_real_source(store: KBStore) -> None:
    src = store.put_source(b"the quick brown fox", title="t")
    ev = Evidence(
        id="e2", source_id=src.id, locator="x",
        quote="slow green turtle", byte_start=4, byte_end=15,
    )
    assert verify_evidence(store, ev).verified is False


def test_verify_evidence_not_verified_when_source_missing(store: KBStore) -> None:
    # a receipt pointing at a source that isn't in the KB cannot be checked;
    # the gate must never read that as verified.
    ev = Evidence(
        id="e3", source_id="does-not-exist",
        locator="x", quote="anything", byte_start=0, byte_end=3,
    )
    result = verify_evidence(store, ev)
    assert result.verified is False


# ---- the quote step: locate a span, or drop what cannot be quoted ----


def test_locate_span_returns_byte_offsets_of_exact_occurrence() -> None:
    assert locate_span(SOURCE, "quick brown") == (4, 15)


def test_locate_span_returns_none_when_quote_absent() -> None:
    # the drop trigger: a claim that cannot quote its source
    assert locate_span(SOURCE, "slow green turtle") is None


def test_locate_span_returns_none_for_empty_quote() -> None:
    assert locate_span(SOURCE, "") is None


def test_locate_span_finds_first_occurrence() -> None:
    src = b"ab ab ab"
    assert locate_span(src, "ab") == (0, 2)


def test_locate_span_uses_byte_offsets_for_multibyte_source() -> None:
    source = "café — au lait".encode()
    start, end = locate_span(source, "au lait")  # type: ignore[misc]
    assert (start, end) == (10, 17)
    assert source[start:end].decode() == "au lait"


def test_receipt_for_quote_produces_a_verifying_receipt() -> None:
    # the invariant that ties the two halves together: anything the locator
    # produces must verify against the same bytes.
    ev = receipt_for_quote(
        source_id="s1", source_bytes=SOURCE, quote="lazy dog", evidence_id="e9"
    )
    assert ev is not None
    assert ev.quote == "lazy dog"
    assert verify_receipt(ev, SOURCE).status is ReceiptStatus.VERIFIED


def test_receipt_for_quote_drops_unquotable_claim() -> None:
    ev = receipt_for_quote(
        source_id="s1", source_bytes=SOURCE, quote="not in here", evidence_id="e9"
    )
    assert ev is None


def test_receipt_for_quote_mints_deterministic_id_by_default() -> None:
    # no evidence_id given -> a content-addressed id, so re-filing the same
    # span in the same source is idempotent (same id), and distinct spans get
    # distinct ids.
    a = receipt_for_quote(source_id="s1", source_bytes=SOURCE, quote="lazy dog")
    b = receipt_for_quote(source_id="s1", source_bytes=SOURCE, quote="lazy dog")
    c = receipt_for_quote(source_id="s1", source_bytes=SOURCE, quote="quick brown")
    assert a is not None and b is not None and c is not None
    assert a.id == b.id
    assert c.id != a.id


# ---- the claim-level verdict: the input to phase d's mechanical gate ----


def _quoted_evidence(store: KBStore, source_id: str, quote: str) -> str:
    ev = receipt_for_quote(source_id=source_id, source_bytes=SOURCE, quote=quote)
    assert ev is not None
    store.put_evidence(ev)
    return ev.id


def test_claim_verdict_approves_when_every_citation_verifies(store: KBStore) -> None:
    src = store.put_source(SOURCE, title="t")
    e1 = _quoted_evidence(store, src.id, "quick brown")
    e2 = _quoted_evidence(store, src.id, "lazy dog")
    verdict = evaluate_claim_receipts(store, [e1, e2])
    assert verdict.approve is True


def test_claim_verdict_rejects_when_any_citation_is_forged(store: KBStore) -> None:
    src = store.put_source(SOURCE, title="t")
    good = _quoted_evidence(store, src.id, "quick brown")
    forged = store.put_evidence(
        Evidence(id="forged", source_id=src.id, locator="x",
                 quote="not in source", byte_start=0, byte_end=5)
    )
    verdict = evaluate_claim_receipts(store, [good, forged.id])
    assert verdict.approve is False
    assert any("forged" in r for r in verdict.reasons)


def test_claim_verdict_rejects_bare_source_citation_as_no_receipt(
    store: KBStore,
) -> None:
    # citing a source id directly carries no byte-offset receipt -> reject
    src = store.put_source(SOURCE, title="t")
    verdict = evaluate_claim_receipts(store, [src.id])
    assert verdict.approve is False


def test_claim_verdict_rejects_empty_citation_list(store: KBStore) -> None:
    verdict = evaluate_claim_receipts(store, [])
    assert verdict.approve is False
