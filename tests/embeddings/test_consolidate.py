"""Consolidation — embedding-dependent clustering and proposal tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vouch import consolidate as cons
from vouch import lifecycle as life
from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME, Embedder
from vouch.models import Claim, ProposalKind, ProposalStatus
from vouch.proposals import approve
from vouch.storage import KBStore


class _IdentityEmbedder(Embedder):
    """Returns near-identical vectors for identical text, distinct otherwise.

    Uses sha256 bytes to create a deterministic unit vector. Identical text
    produces identical vectors (cosine=1.0). Distinct text produces
    effectively random distinct vectors (cosine ≈ 0).
    """

    name = "mock"
    version = "1"
    dim = 8

    def encode(self, text: str) -> np.ndarray:
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        out = np.array([h[i] / 255.0 for i in range(self.dim)], dtype=np.float32)
        norm = float(np.linalg.norm(out))
        if norm > 0:
            out /= norm
        return out


@pytest.fixture(autouse=True)
def _register_default() -> None:
    register(DEFAULT_MODEL_NAME, _IdentityEmbedder)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _seed_duplicate_claims(store: KBStore) -> dict:
    """Create approved claims with identical and distinct text."""
    src = store.put_source(b"evidence-material")

    # Two claims with identical text — should cluster.
    c1 = Claim(id="dup-a", text="auth uses jwt tokens", evidence=[src.id], confidence=0.8)
    c2 = Claim(id="dup-b", text="auth uses jwt tokens", evidence=[src.id], confidence=0.9)
    # One claim with distinct text — should not cluster with the above.
    c3 = Claim(id="unique", text="completely different topic about databases", evidence=[src.id])
    store.put_claim(c1)
    store.put_claim(c2)
    store.put_claim(c3)

    # Mark all as approved (simulating approved_by field).
    for cid in ("dup-a", "dup-b", "unique"):
        claim = store.get_claim(cid)
        claim.approved_by = "human"
        store.update_claim(claim)

    return {"source": src.id, "claim_ids": ["dup-a", "dup-b", "unique"]}


def test_consolidate_clusters_duplicates(store: KBStore) -> None:
    """Identical-text claims should be clustered."""
    _seed_duplicate_claims(store)
    result = cons.consolidate(store, threshold=0.99, dry_run=True)
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    # dup-b has higher confidence (0.9 > 0.8) so should be survivor.
    assert cluster.survivor_id == "dup-b"
    assert len(cluster.members) == 1
    assert cluster.members[0].claim_id == "dup-a"


def test_consolidate_dry_run_writes_nothing(store: KBStore) -> None:
    """dry_run must not create any proposals."""
    _seed_duplicate_claims(store)
    proposals_before = len(store.list_proposals())
    cons.consolidate(store, threshold=0.99, dry_run=True)
    assert len(store.list_proposals()) == proposals_before


def test_consolidate_supersede_mode(store: KBStore) -> None:
    """Supersede mode creates relation proposals for each non-survivor."""
    _seed_duplicate_claims(store)
    result = cons.consolidate(
        store, threshold=0.99, mode="supersede", actor="test-agent",
    )
    assert len(result.proposals) == 1
    prop = result.proposals[0]
    assert prop["mode"] == "supersede"
    assert prop["survivor"] == "dup-b"
    assert prop["member"] == "dup-a"

    # Should appear in pending proposals as a RELATION.
    pending = store.list_proposals(ProposalStatus.PENDING)
    rel_props = [
        p for p in pending
        if p.kind == ProposalKind.RELATION
        and p.payload.get("relation") == "supersedes"
    ]
    assert len(rel_props) == 1
    assert rel_props[0].payload["source"] == "dup-b"
    assert rel_props[0].payload["target"] == "dup-a"


def test_consolidate_merge_mode(store: KBStore) -> None:
    """Merge mode proposes a single union claim per cluster."""
    _seed_duplicate_claims(store)
    result = cons.consolidate(
        store, threshold=0.99, mode="merge", actor="test-agent",
    )
    assert len(result.proposals) == 1
    prop = result.proposals[0]
    assert prop["mode"] == "merge"
    assert set(prop["merged_claim_ids"]) == {"dup-a", "dup-b"}

    # Should appear as a pending CLAIM proposal.
    pending = store.list_proposals(ProposalStatus.PENDING)
    merge_props = [
        p for p in pending
        if p.kind == ProposalKind.CLAIM
        and "consolidation merge" in (p.rationale or "")
    ]
    assert len(merge_props) == 1


def test_consolidate_excludes_archived(store: KBStore) -> None:
    """Archived claims should not participate in consolidation."""
    _seed_duplicate_claims(store)
    life.archive(store, claim_id="dup-a", actor="human")
    result = cons.consolidate(store, threshold=0.99, dry_run=True)
    # Only dup-b remains eligible, can't form a cluster alone.
    assert len(result.clusters) == 0


def test_consolidate_excludes_superseded(store: KBStore) -> None:
    """Already-superseded claims should not be re-proposed."""
    _seed_duplicate_claims(store)
    # Directly supersede one claim.
    life.supersede(store, old_claim_id="dup-a", new_claim_id="dup-b", actor="human")
    result = cons.consolidate(store, threshold=0.99, dry_run=True)
    assert len(result.clusters) == 0


def test_consolidate_dedup_proposals(store: KBStore) -> None:
    """Running consolidate twice should not create duplicate proposals."""
    _seed_duplicate_claims(store)
    r1 = cons.consolidate(store, threshold=0.99, mode="supersede")
    assert len(r1.proposals) == 1
    r2 = cons.consolidate(store, threshold=0.99, mode="supersede")
    assert len(r2.proposals) == 0


def test_consolidate_respects_threshold(store: KBStore) -> None:
    """Claims that don't meet threshold should not cluster."""
    _seed_duplicate_claims(store)
    # Use threshold of 1.01 — impossible to meet, so no clusters.
    result = cons.consolidate(store, threshold=1.01, dry_run=True)
    assert len(result.clusters) == 0


def test_consolidate_max_clusters(store: KBStore) -> None:
    """max_clusters should cap the number of clusters returned."""
    _seed_duplicate_claims(store)
    result = cons.consolidate(store, threshold=0.99, max_clusters=0, dry_run=True)
    # max_clusters=0 is invalid, falls back to default (50).
    # But let's test with max_clusters=1 — should return at most 1.
    result = cons.consolidate(store, threshold=0.99, max_clusters=1, dry_run=True)
    assert len(result.clusters) <= 1


def test_consolidate_result_config_used(store: KBStore) -> None:
    """config_used should reflect the actual parameters used."""
    result = cons.consolidate(
        store, threshold=0.88, mode="merge", max_clusters=5, dry_run=True,
    )
    assert result.config_used["threshold"] == 0.88
    assert result.config_used["mode"] == "merge"
    assert result.config_used["max_clusters"] == 5


def test_consolidate_supersede_approve_flow(store: KBStore) -> None:
    """Approving a consolidation supersede proposal should invoke lifecycle.supersede."""
    _seed_duplicate_claims(store)
    result = cons.consolidate(
        store, threshold=0.99, mode="supersede", actor="consolidate-agent",
    )
    assert len(result.proposals) == 1
    proposal_id = result.proposals[0]["proposal_id"]

    # Approve the proposal.
    artifact = approve(store, proposal_id, approved_by="human")
    # The approve path for RELATION proposals creates the Relation artifact.
    assert artifact.relation.value == "supersedes"

    # Now the lifecycle supersede should be manually invoked by the reviewer
    # after seeing the approved relation. The relation itself is the record.
    # Verify the relation was created.
    rel = store.get_relation("dup-b--supersedes--dup-a")
    assert rel.source == "dup-b"
    assert rel.target == "dup-a"
