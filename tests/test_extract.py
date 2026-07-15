"""Phase B — source → receipt-backed claims, the capture step with no human.

Segments an ingested source into quotable spans and files each as a claim that
quotes it verbatim, so every claim's receipt verifies by construction and
Phase D's gate can auto-approve it. Deterministic and llm-free: the receipt
check is the guardrail, so the pipeline runs in the base install and under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import extract, index_db, receipts
from vouch.models import ProposalStatus
from vouch.storage import KBStore

SOURCE = (
    b"The sky is blue and clear. Water boils at one hundred degrees celsius. "
    b"Grass is green in the spring."
)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_segment_source_splits_into_verbatim_spans() -> None:
    text = SOURCE.decode()
    segs = extract.segment_source(text)
    assert "The sky is blue and clear." in segs
    assert "Water boils at one hundred degrees celsius." in segs
    # every segment is a verbatim substring of the source it came from.
    for s in segs:
        assert s in text


def test_segment_source_drops_short_noise_and_dupes() -> None:
    text = (
        "ok. The very same sentence appears here twice in a row now. "
        "The very same sentence appears here twice in a row now. ###---***"
    )
    segs = extract.segment_source(text)
    assert "ok" not in segs  # below min length
    assert segs.count("The very same sentence appears here twice in a row now.") == 1
    assert all(any(c.isalpha() for c in s) for s in segs)  # no pure-symbol spans


def test_extract_receipt_claims_files_verifying_claims(store: KBStore) -> None:
    src = store.put_source(SOURCE)
    filed = extract.extract_receipt_claims(store, src.id, proposed_by="agent")
    assert len(filed) == 3
    # each filed claim carries a receipt that verifies against the source bytes.
    for res in filed:
        evidence_ids = store.get_proposal(res.id).payload["evidence"]
        assert receipts.evaluate_claim_receipts(store, evidence_ids).approve


def test_ingest_source_auto_approves_and_is_recallable(store: KBStore) -> None:
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: true\n", encoding="utf-8"
    )
    _src, approved = extract.ingest_source(store, SOURCE, proposed_by="agent")
    assert len(approved) == 3
    # durable with no human, and recallable.
    assert len(store.list_claims()) == 3
    hits = index_db.search(store.kb_dir, "boils")
    assert any(k == "claim" for k, *_ in hits)


def test_ingest_source_leaves_pending_when_gate_off(store: KBStore) -> None:
    _src, approved = extract.ingest_source(store, SOURCE, proposed_by="agent")
    assert approved == []
    # proposed and receipt-backed, but still waiting for a human.
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 3
