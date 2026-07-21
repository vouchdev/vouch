"""Gated federation import: inbound knowledge lands as PENDING proposals.

The load-bearing invariant (ROADMAP.md step 10): "any data path that lands
writes without a receiving-side proposal is wrong." `import_as_proposals` is the
gated counterpart to `import_apply` -- it files inbound claims as pending
proposals via `proposals.propose_claim`, so nothing reaches `decided/` without
this KB's own `proposals.approve()`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import bundle, proposals
from vouch.models import Claim, ProposalKind, ProposalStatus
from vouch.storage import ArtifactNotFoundError, KBStore, read_or_create_instance_id


def _kb_with_claim(root: Path, *, claim_id: str, text: str) -> KBStore:
    store = KBStore.init(root)
    src = store.put_source(text.encode(), title="doc")
    store.put_claim(Claim(id=claim_id, text=text, evidence=[src.id], tags=["seed-tag"]))
    return store


def _knowledge_bundle(store: KBStore, dest: Path) -> Path:
    # A hub push ships knowledge only -- no decided/ (the origin's own proposal
    # records) and no sessions.
    bundle.export(store.kb_dir, dest=dest, exclude=("decided", "sessions"))
    return dest


def test_import_as_proposals_lands_claims_as_pending_not_committed(tmp_path: Path) -> None:
    src = _kb_with_claim(tmp_path / "a", claim_id="c1", text="advisory locks are session scoped")
    bundle_path = _knowledge_bundle(src, tmp_path / "a.tar.gz")

    dest = KBStore.init(tmp_path / "b")
    result = bundle.import_as_proposals(dest.kb_dir, bundle_path, origin_kb="kb-alice")

    # The claim did NOT land in the committed store...
    with pytest.raises(ArtifactNotFoundError):
        dest.get_claim("c1")
    # ...it is a PENDING claim proposal instead.
    claim_props = [
        p for p in dest.list_proposals(ProposalStatus.PENDING) if p.kind == ProposalKind.CLAIM
    ]
    assert len(claim_props) == 1
    assert result["proposed"]
    assert result["origin_kb"] == "kb-alice"
    # provenance: the proposing actor names the origin KB.
    assert "kb-alice" in claim_props[0].proposed_by


def test_imported_proposal_carries_origin_and_approves_into_a_claim(tmp_path: Path) -> None:
    src = _kb_with_claim(tmp_path / "a", claim_id="c1", text="advisory locks are session scoped")
    bundle_path = _knowledge_bundle(src, tmp_path / "a.tar.gz")
    dest = KBStore.init(tmp_path / "b")
    result = bundle.import_as_proposals(dest.kb_dir, bundle_path, origin_kb="kb-alice")

    pid = result["proposed"][0]
    approved = proposals.approve(dest, pid, approved_by="human")

    # Only after THIS KB's approve does it become a durable claim...
    landed = dest.get_claim(approved.id)
    assert "advisory locks" in landed.text
    # ...carrying the origin-KB provenance tag that survives approval.
    assert "origin:kb-alice" in landed.tags


def test_import_as_proposals_writes_nothing_to_decided(tmp_path: Path) -> None:
    src = _kb_with_claim(tmp_path / "a", claim_id="c1", text="a durable fact worth sharing")
    bundle_path = _knowledge_bundle(src, tmp_path / "a.tar.gz")
    dest = KBStore.init(tmp_path / "b")

    decided_dir = dest.kb_dir / "decided"
    before = {p.name for p in decided_dir.glob("*")} if decided_dir.exists() else set()
    bundle.import_as_proposals(dest.kb_dir, bundle_path, origin_kb="kb-alice")
    after = {p.name for p in decided_dir.glob("*")} if decided_dir.exists() else set()

    # The gate held: not a single decided artifact was written on import.
    assert before == after


def test_import_as_proposals_registers_sources_so_claims_can_cite_them(tmp_path: Path) -> None:
    src = _kb_with_claim(tmp_path / "a", claim_id="c1", text="cited to a real source here")
    bundle_path = _knowledge_bundle(src, tmp_path / "a.tar.gz")
    dest = KBStore.init(tmp_path / "b")

    result = bundle.import_as_proposals(dest.kb_dir, bundle_path, origin_kb="kb-alice")
    # Sources are substrate, registered directly so the claim proposal can cite
    # them -- the same shape inbox.scan uses before it proposes. propose_claim
    # would have raised if a cited id did not resolve locally, so a returned
    # proposal is itself proof the source landed; assert it on disk too.
    assert result["sources_registered"] >= 1
    assert result["proposed"]
    sources_dir = dest.kb_dir / "sources"
    assert sources_dir.exists() and any(sources_dir.iterdir())


def test_export_stamps_instance_id_and_import_reads_it_as_origin(tmp_path: Path) -> None:
    src = _kb_with_claim(tmp_path / "a", claim_id="c1", text="a fact from a real kb")
    origin_id = read_or_create_instance_id(src.kb_dir)  # the id export will stamp
    bundle_path = _knowledge_bundle(src, tmp_path / "a.tar.gz")

    dest = KBStore.init(tmp_path / "b")
    # No explicit origin_kb: provenance must come from the bundle manifest kb_id.
    result = bundle.import_as_proposals(dest.kb_dir, bundle_path)
    assert result["origin_kb"] == origin_id

    approved = proposals.approve(dest, result["proposed"][0], approved_by="human")
    assert f"origin:{origin_id}" in dest.get_claim(approved.id).tags


def test_conformance_gate_holds_only_approve_makes_it_durable(tmp_path: Path) -> None:
    """The ROADMAP step-10 invariant, as a test: the federation receive path
    lands nothing in the committed store; only proposals.approve() does."""
    src = _kb_with_claim(tmp_path / "a", claim_id="c1", text="a fact to federate")
    bundle_path = _knowledge_bundle(src, tmp_path / "a.tar.gz")
    dest = KBStore.init(tmp_path / "b")

    result = bundle.import_as_proposals(dest.kb_dir, bundle_path, origin_kb="kb-alice")
    # The committed claim file does NOT exist after import...
    assert not (dest.kb_dir / "claims" / "c1.yaml").exists()
    with pytest.raises(ArtifactNotFoundError):
        dest.get_claim("c1")
    # ...and only THIS KB's approve makes it durable.
    proposals.approve(dest, result["proposed"][0], approved_by="human")
    assert (dest.kb_dir / "claims" / "c1.yaml").exists()
    assert dest.get_claim("c1").text == "a fact to federate"


def test_instance_id_is_stable_and_per_kb(tmp_path: Path) -> None:
    a = KBStore.init(tmp_path / "a")
    b = KBStore.init(tmp_path / "b")
    id_a1 = read_or_create_instance_id(a.kb_dir)
    id_a2 = read_or_create_instance_id(a.kb_dir)
    id_b = read_or_create_instance_id(b.kb_dir)
    assert id_a1 == id_a2  # stable across calls
    assert id_a1 != id_b   # distinct per KB
