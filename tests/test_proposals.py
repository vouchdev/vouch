"""Receipt-backed intake: propose_quoted_claim wires the span locator into the
gated write path, so a filed claim carries a verifiable byte-offset receipt or
is dropped for being unquotable.
"""

from __future__ import annotations

import pytest

from vouch.proposals import (
    ProposalError,
    propose_claim,
    propose_entity,
    propose_quoted_claim,
    propose_relation,
)
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


# propose-time model validation: an out-of-range confidence (or other
# model-level constraint violation) must be rejected at propose time, not
# filed as a proposal that can never pass approve() and sits stuck in the
# pending queue. Regression for the gap where propose_claim/propose_relation/
# propose_entity built their payload dict and handed it straight to
# _file_proposal with no check against the Claim/Relation/Entity model's own
# Field(ge=0.0, le=1.0) / enum constraints -- approve() (via Claim(**payload)
# etc.) was the only place that ever caught it, too late to give the
# proposer a usable error and too late to keep the pending queue clean.


def test_propose_claim_rejects_out_of_range_confidence(store: KBStore) -> None:
    src = store.put_source(b"evidence text", title="t")
    with pytest.raises(ProposalError, match="invalid claim payload"):
        propose_claim(
            store,
            text="a claim with an impossible confidence",
            evidence=[src.id],
            proposed_by="tester",
            confidence=1.5,
        )
    # nothing was filed -- the pending queue stays clean
    assert store.list_proposals() == []


def test_propose_claim_rejects_negative_confidence(store: KBStore) -> None:
    src = store.put_source(b"evidence text", title="t")
    with pytest.raises(ProposalError, match="invalid claim payload"):
        propose_claim(
            store,
            text="a claim with a negative confidence",
            evidence=[src.id],
            proposed_by="tester",
            confidence=-0.5,
        )
    assert store.list_proposals() == []


def test_propose_relation_rejects_out_of_range_confidence(store: KBStore) -> None:
    a = store.put_source(b"endpoint a", title="a")
    b = store.put_source(b"endpoint b", title="b")
    with pytest.raises(ProposalError, match="invalid relation payload"):
        propose_relation(
            store,
            src=a.id,
            relation="references",
            target=b.id,
            proposed_by="tester",
            confidence=2.0,
        )
    assert store.list_proposals() == []


def test_propose_entity_rejects_invalid_type(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="invalid entity payload"):
        propose_entity(
            store,
            name="a thing",
            entity_type="not-a-real-entity-type",
            proposed_by="tester",
        )
    assert store.list_proposals() == []
