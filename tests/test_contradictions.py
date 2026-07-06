"""Contradiction scanner: heuristic scan + advisory `contradicts` proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import lifecycle
from vouch.contradictions import find_candidates, scan
from vouch.models import (
    Claim,
    ClaimStatus,
    Entity,
    EntityType,
    ProposalStatus,
    Relation,
    RelationType,
)
from vouch.proposals import approve, propose_relation
from vouch.storage import KBStore

_TEXT_A = "the payments queue processes jobs synchronously"
_TEXT_B = "the payments queue does not process jobs synchronously"


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _seed_conflicting_pair(
    store: KBStore, *, id_a: str = "c-a", id_b: str = "c-b", entity: str = "svc-payments",
) -> tuple[Claim, Claim]:
    src = store.put_source(b"evidence")
    store.put_entity(Entity(id=entity, name="Payments", type=EntityType.SYSTEM))
    a = store.put_claim(Claim(id=id_a, text=_TEXT_A, evidence=[src.id], entities=[entity]))
    b = store.put_claim(Claim(id=id_b, text=_TEXT_B, evidence=[src.id], entities=[entity]))
    return a, b


# --- find_candidates --------------------------------------------------------


def test_finds_same_entity_opposite_polarity_pair(store: KBStore) -> None:
    _seed_conflicting_pair(store)
    candidates = find_candidates(store, threshold=0.3)
    assert len(candidates) == 1
    c = candidates[0]
    assert {c.claim_a, c.claim_b} == {"c-a", "c-b"}
    assert c.entity == "svc-payments"
    assert c.score >= 0.3


def test_ignores_pairs_without_shared_entity(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    store.put_entity(Entity(id="svc-a", name="A", type=EntityType.SYSTEM))
    store.put_entity(Entity(id="svc-b", name="B", type=EntityType.SYSTEM))
    store.put_claim(Claim(id="c-a", text=_TEXT_A, evidence=[src.id], entities=["svc-a"]))
    store.put_claim(Claim(id="c-b", text=_TEXT_B, evidence=[src.id], entities=["svc-b"]))
    assert find_candidates(store, threshold=0.0) == []


def test_ignores_same_polarity_pair(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    store.put_entity(Entity(id="svc-payments", name="Payments", type=EntityType.SYSTEM))
    store.put_claim(
        Claim(id="c-a", text=_TEXT_A, evidence=[src.id], entities=["svc-payments"]),
    )
    store.put_claim(
        Claim(id="c-b", text=_TEXT_A, evidence=[src.id], entities=["svc-payments"]),
    )
    assert find_candidates(store, threshold=0.0) == []


def test_threshold_filters_low_overlap_pairs(store: KBStore) -> None:
    _seed_conflicting_pair(store)
    assert find_candidates(store, threshold=0.99) == []


def test_entity_filter_restricts_scan(store: KBStore) -> None:
    _seed_conflicting_pair(store, id_a="c-a", id_b="c-b", entity="svc-payments")
    src = store.put_source(b"evidence")
    store.put_entity(Entity(id="svc-other", name="Other", type=EntityType.SYSTEM))
    store.put_claim(
        Claim(id="c-c", text=_TEXT_A, evidence=[src.id], entities=["svc-other"]),
    )
    store.put_claim(
        Claim(id="c-d", text=_TEXT_B, evidence=[src.id], entities=["svc-other"]),
    )
    candidates = find_candidates(store, threshold=0.3, entity="svc-payments")
    assert {(c.claim_a, c.claim_b) for c in candidates} == {("c-a", "c-b")}


def test_inactive_claims_excluded(store: KBStore) -> None:
    a, _b = _seed_conflicting_pair(store)
    a.status = ClaimStatus.ARCHIVED
    store.update_claim(a)
    assert find_candidates(store, threshold=0.0) == []


def test_skips_pair_already_cross_linked_via_contradicts(store: KBStore) -> None:
    a, b = _seed_conflicting_pair(store)
    lifecycle.contradict(store, claim_a=a.id, claim_b=b.id, actor="reviewer")
    assert find_candidates(store, threshold=0.0) == []


def test_skips_pair_with_existing_approved_relation_edge(store: KBStore) -> None:
    a, b = _seed_conflicting_pair(store)
    store.put_relation(
        Relation(id=f"{a.id}--contradicts--{b.id}", source=a.id,
                 relation=RelationType.CONTRADICTS, target=b.id),
    )
    assert find_candidates(store, threshold=0.0) == []


def test_skips_pair_with_existing_pending_proposal(store: KBStore) -> None:
    a, b = _seed_conflicting_pair(store)
    propose_relation(store, src=a.id, relation="contradicts", target=b.id, proposed_by="agent")
    assert find_candidates(store, threshold=0.0) == []


# --- scan (dry-run vs. write) -----------------------------------------------


def test_dry_run_writes_nothing(store: KBStore) -> None:
    _seed_conflicting_pair(store)
    rows = scan(store, threshold=0.3, dry_run=True, proposed_by="vouch-contradict-scan")
    assert len(rows) == 1
    assert "proposal_id" not in rows[0]
    assert store.list_proposals(ProposalStatus.PENDING) == []
    assert store.list_relations() == []


def test_no_dry_run_files_one_proposal_per_pair(store: KBStore) -> None:
    _seed_conflicting_pair(store)
    rows = scan(store, threshold=0.3, dry_run=False, proposed_by="vouch-contradict-scan")
    assert len(rows) == 1
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].payload["relation"] == "contradicts"
    assert {pending[0].payload["source"], pending[0].payload["target"]} == {"c-a", "c-b"}
    assert rows[0]["proposal_id"] == pending[0].id


def test_scan_never_mutates_claim_status_or_writes_relation(store: KBStore) -> None:
    a, b = _seed_conflicting_pair(store)
    scan(store, threshold=0.3, dry_run=False, proposed_by="vouch-contradict-scan")
    assert store.get_claim(a.id).status != ClaimStatus.CONTESTED
    assert store.get_claim(b.id).status != ClaimStatus.CONTESTED
    assert store.get_claim(a.id).contradicts == []
    assert store.get_claim(b.id).contradicts == []
    assert store.list_relations() == []


def test_repeated_scan_does_not_duplicate_pending_proposal(store: KBStore) -> None:
    _seed_conflicting_pair(store)
    scan(store, threshold=0.3, dry_run=False, proposed_by="vouch-contradict-scan")
    rows = scan(store, threshold=0.3, dry_run=False, proposed_by="vouch-contradict-scan")
    assert rows == []
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 1


def test_limit_caps_number_of_pairs_proposed(store: KBStore) -> None:
    _seed_conflicting_pair(store, id_a="c-a", id_b="c-b", entity="svc-payments")
    src = store.put_source(b"evidence")
    store.put_entity(Entity(id="svc-billing", name="Billing", type=EntityType.SYSTEM))
    store.put_claim(
        Claim(id="c-c", text=_TEXT_A, evidence=[src.id], entities=["svc-billing"]),
    )
    store.put_claim(
        Claim(id="c-d", text=_TEXT_B, evidence=[src.id], entities=["svc-billing"]),
    )
    rows = scan(store, threshold=0.3, dry_run=False, limit=1, proposed_by="vouch-contradict-scan")
    assert len(rows) == 1
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 1


# --- full pipeline: scan -> propose -> approve ------------------------------


def test_scan_propose_approve_lands_contradicts_edge(store: KBStore) -> None:
    _seed_conflicting_pair(store)
    rows = scan(store, threshold=0.3, dry_run=False, proposed_by="vouch-contradict-scan")
    proposal_id = rows[0]["proposal_id"]
    rel = approve(store, proposal_id, approved_by="reviewer")
    assert isinstance(rel, Relation)
    assert rel.relation == RelationType.CONTRADICTS
    assert store.get_relation(rel.id) == rel
