"""Receipt-backed intake: propose_quoted_claim wires the span locator into the
gated write path, so a filed claim carries a verifiable byte-offset receipt or
is dropped for being unquotable.
"""

from __future__ import annotations

import pytest

from vouch.proposals import propose_quoted_claim
from vouch.receipts import ReceiptStatus, verify_evidence
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path, monkeypatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def test_propose_quoted_claim_files_a_verifying_receipt(store: KBStore) -> None:
    src = store.put_source(b"the sky is blue on a clear day", title="t")
    result = propose_quoted_claim(
        store,
        text="the sky is blue",
        source_id=src.id,
        quote="sky is blue",
        proposed_by="tester",
    )
    assert result is not None
    # exactly one receipt-backed evidence, and it verifies against the source
    evs = store.list_evidence()
    assert len(evs) == 1
    assert verify_evidence(store, evs[0]).status is ReceiptStatus.VERIFIED
    # the proposal cites that evidence
    assert evs[0].id in result.proposal.payload["evidence"]


def test_propose_quoted_claim_drops_unquotable_claim(store: KBStore) -> None:
    src = store.put_source(b"the sky is blue", title="t")
    result = propose_quoted_claim(
        store,
        text="the grass is green",
        source_id=src.id,
        quote="grass is green",  # not in the source
        proposed_by="tester",
    )
    assert result is None
    # nothing filed, no evidence stored
    assert store.list_evidence() == []
    assert store.list_proposals() == []


def test_propose_quoted_claim_is_idempotent_on_repeated_span(store: KBStore) -> None:
    src = store.put_source(b"the sky is blue", title="t")
    kw = dict(source_id=src.id, quote="sky is blue", proposed_by="tester")
    first = propose_quoted_claim(store, text="claim one", **kw)  # type: ignore[arg-type]
    second = propose_quoted_claim(store, text="claim two", **kw)  # type: ignore[arg-type]
    assert first is not None and second is not None
    # the span was stored once; both claims cite the same evidence
    assert len(store.list_evidence()) == 1
