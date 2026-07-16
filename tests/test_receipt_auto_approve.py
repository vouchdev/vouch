"""Phase D — receipt-gated auto-approve: the mechanical gate replaces the human.

With ``review.auto_approve_on_receipt`` on, a claim whose byte-offset receipts
all verify clears self-approval (the receipt *is* the reviewer, checked by
string comparison, no LLM). A claim that cites a bare source or a forged span
carries no verifiable receipt and still requires a human — the gate degrades to
asking, it never rubber-stamps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.models import ProposalStatus
from vouch.proposals import (
    ProposalError,
    approve,
    auto_approve_receipts,
    propose_claim,
    propose_quoted_claim,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _enable_receipt_gate(store: KBStore) -> None:
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: true\n", encoding="utf-8"
    )


def test_receipt_verified_claim_self_approves_when_gate_on(store: KBStore) -> None:
    _enable_receipt_gate(store)
    src = store.put_source(b"the sky is blue today")
    res = propose_quoted_claim(
        store, text="the sky is blue", source_id=src.id,
        quote="the sky is blue", proposed_by="agent-a",
    )
    assert res is not None
    # the proposer approves its OWN claim -- allowed only because the receipt
    # verifies against the source bytes.
    claim = approve(store, res.id, approved_by="agent-a")
    assert store.get_claim(claim.id).text == "the sky is blue"
    assert store.get_proposal(res.id).status is ProposalStatus.APPROVED


def test_bare_source_claim_still_needs_human_when_gate_on(store: KBStore) -> None:
    _enable_receipt_gate(store)
    src = store.put_source(b"unquotable content")
    # cites the bare source id -- no byte-offset receipt to verify.
    res = propose_claim(
        store, text="unquoted assertion", evidence=[src.id], proposed_by="agent-a",
    )
    with pytest.raises(ProposalError, match="forbidden_self_approval"):
        approve(store, res.id, approved_by="agent-a")


def test_receipt_claim_blocked_when_gate_off(store: KBStore) -> None:
    # default config: gate off -- a verifying receipt does not grant self-approval.
    src = store.put_source(b"the sky is blue")
    res = propose_quoted_claim(
        store, text="the sky is blue", source_id=src.id,
        quote="the sky is blue", proposed_by="agent-a",
    )
    assert res is not None
    with pytest.raises(ProposalError, match="forbidden_self_approval"):
        approve(store, res.id, approved_by="agent-a")


def test_human_can_still_approve_receipt_claim(store: KBStore) -> None:
    # a reviewer other than the proposer is always allowed, gate on or off.
    src = store.put_source(b"the sky is blue")
    res = propose_quoted_claim(
        store, text="the sky is blue", source_id=src.id,
        quote="the sky is blue", proposed_by="agent-a",
    )
    assert res is not None
    claim = approve(store, res.id, approved_by="a-human")
    assert store.get_claim(claim.id).text == "the sky is blue"


def test_auto_approve_receipts_drains_verified_leaves_unverified(
    store: KBStore,
) -> None:
    _enable_receipt_gate(store)
    src = store.put_source(b"alpha beta gamma")
    good = propose_quoted_claim(
        store, text="mentions beta", source_id=src.id, quote="beta",
        proposed_by="agent-a",
    )
    bare = propose_claim(
        store, text="bare claim", evidence=[src.id], proposed_by="agent-a",
    )
    assert good is not None

    approved = auto_approve_receipts(store)

    assert len(approved) == 1
    assert store.get_proposal(good.id).status is ProposalStatus.APPROVED
    # the bare-source claim is left pending for a human -- never rubber-stamped.
    assert store.get_proposal(bare.id).status is ProposalStatus.PENDING


def test_auto_approve_receipts_noop_when_gate_off(store: KBStore) -> None:
    src = store.put_source(b"alpha beta")
    propose_quoted_claim(
        store, text="mentions beta", source_id=src.id, quote="beta",
        proposed_by="agent-a",
    )
    assert auto_approve_receipts(store) == []
