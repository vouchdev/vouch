"""Storage round-trip + review-gate + lifecycle + audit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vouch import audit, lifecycle
from vouch.models import (
    Claim,
    ClaimStatus,
    Entity,
    EntityType,
    Evidence,
    Page,
    PageType,
    ProposalStatus,
    Relation,
    RelationType,
)
from vouch.proposals import (
    ProposalError,
    approve,
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
    reject,
)
from vouch.storage import (
    KBNotFoundError,
    KBStore,
    discover_root,
    sha256_hex,
)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


# --- init / discovery -----------------------------------------------------


def test_init_creates_layout(tmp_path: Path) -> None:
    s = KBStore.init(tmp_path)
    for sub in ("claims", "pages", "sources", "entities", "relations",
                "evidence", "sessions", "proposed", "decided"):
        assert (s.kb_dir / sub).is_dir()
    assert s.config_path.exists()
    gi = (s.kb_dir / ".gitignore").read_text()
    assert "proposed/" in gi and "state.db" in gi


def test_discover_root_walks_up(tmp_path: Path) -> None:
    KBStore.init(tmp_path)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert discover_root(nested) == tmp_path


def test_discover_root_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(KBNotFoundError):
        discover_root(tmp_path)


def test_discover_root_honours_vouch_kb_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`VOUCH_KB_PATH=/abs/path/.vouch` skips the upward walk and returns the
    .vouch parent directly. Documented in adapters/generic-mcp/README — needed
    for Claude Desktop / hosts that launch the server with a default cwd."""
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    KBStore.init(kb_root)
    # Start the walk somewhere with no .vouch in the chain so the only way
    # discover_root can succeed is by honouring the env var.
    walk_start = tmp_path / "no-kb-here"
    walk_start.mkdir()
    monkeypatch.setenv("VOUCH_KB_PATH", str(kb_root / ".vouch"))
    assert discover_root(walk_start) == kb_root


def test_vouch_kb_path_missing_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_KB_PATH", str(tmp_path / "nope" / ".vouch"))
    with pytest.raises(KBNotFoundError, match="VOUCH_KB_PATH"):
        discover_root(tmp_path)


def test_vouch_kb_path_not_a_vouch_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var must point at a `.vouch` directory, not its parent — keeps
    the contract unambiguous and matches the documented example."""
    KBStore.init(tmp_path)
    monkeypatch.setenv("VOUCH_KB_PATH", str(tmp_path))  # parent, not .vouch
    with pytest.raises(KBNotFoundError, match="\\.vouch"):
        discover_root(tmp_path)


def test_vouch_kb_path_empty_falls_back_to_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicitly empty env var should not block normal discovery."""
    KBStore.init(tmp_path)
    monkeypatch.setenv("VOUCH_KB_PATH", "")
    assert discover_root(tmp_path) == tmp_path


# --- sources --------------------------------------------------------------


def test_source_dedupes_on_content_hash(store: KBStore) -> None:
    a = store.put_source(b"hello", title="first")
    b = store.put_source(b"hello", title="second")
    assert a.id == b.id == sha256_hex(b"hello")
    assert store.get_source(a.id).title == "first"


def test_source_hash_field_mirrors_id(store: KBStore) -> None:
    s = store.put_source(b"x")
    assert s.hash == s.id


# --- claims ---------------------------------------------------------------


def test_claim_requires_existing_source(store: KBStore) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        store.put_claim(Claim(id="x", text="t", evidence=["0" * 64]))


def test_claim_round_trip(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    claim = Claim(id="auth-uses-jwt", text="auth uses JWT", evidence=[src.id])
    store.put_claim(claim)
    back = store.get_claim(claim.id)
    assert back.text == claim.text
    assert back.evidence == [src.id]
    assert back.status == ClaimStatus.WORKING
    assert back.type.value == "observation"


def test_claim_can_be_updated(store: KBStore) -> None:
    src = store.put_source(b"e")
    c = store.put_claim(Claim(id="c1", text="orig", evidence=[src.id]))
    c.status = ClaimStatus.STABLE
    store.update_claim(c)
    assert store.get_claim("c1").status == ClaimStatus.STABLE


def test_claim_model_rejects_empty_evidence() -> None:
    """Regression for #81: the 'claims must cite sources' guarantee
    (README §'Why this exists' point 3; CONTRIBUTING §'Things we
    won't merge') is now enforced on the Claim model itself, so
    every write path inherits the check instead of relying on
    proposals.propose_claim alone."""
    with pytest.raises(ValidationError, match="cite at least one"):
        Claim(id="c1", text="uncited", evidence=[])


def test_put_claim_rejects_empty_evidence(store: KBStore) -> None:
    """Regression for #81: store.put_claim is a direct write path
    that used to silently accept Claim(evidence=[]) because the
    only existence-check loop iterated zero times. The model-level
    validator now fires before put_claim is even called."""
    with pytest.raises(ValidationError, match="cite at least one"):
        store.put_claim(Claim(id="c1", text="uncited", evidence=[]))
    assert not (store.kb_dir / "claims" / "c1.yaml").exists()


def test_update_claim_rejects_empty_evidence(store: KBStore) -> None:
    """Regression for #81: a previously-cited claim cannot be mutated
    down to evidence=[] and silently re-persisted. The model's field
    validator only fires at construction time, so update_claim
    re-validates via Claim.model_validate(claim.model_dump()) before
    writing — otherwise in-place mutation would bypass the gate."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="cited", evidence=[src.id]))
    persisted_before = (store.kb_dir / "claims" / "c1.yaml").read_text()

    c = store.get_claim("c1")
    c.evidence = []  # in-place mutation alone doesn't trigger validation

    with pytest.raises(ValidationError, match="cite at least one"):
        store.update_claim(c)
    assert (store.kb_dir / "claims" / "c1.yaml").read_text() == persisted_before


# --- pages ----------------------------------------------------------------


def test_page_with_frontmatter_round_trip(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    page = Page(
        id="overview", title="Auth overview",
        type=PageType.CONCEPT,
        body="# Overview\n\nBody with *markdown*.",
        claims=["c1"], tags=["auth"],
    )
    store.put_page(page)
    back = store.get_page(page.id)
    assert back.title == page.title
    assert back.body == page.body
    assert back.claims == ["c1"]
    assert back.tags == ["auth"]
    assert back.type == PageType.CONCEPT


# --- entities + relations -------------------------------------------------


def test_entity_round_trip(store: KBStore) -> None:
    e = store.put_entity(Entity(id="proj-foo", name="Foo", type=EntityType.PROJECT))
    back = store.get_entity(e.id)
    assert back.name == "Foo"


def test_relation_round_trip(store: KBStore) -> None:
    store.put_entity(Entity(id="a", name="A", type=EntityType.PROJECT))
    store.put_entity(Entity(id="b", name="B", type=EntityType.PROJECT))
    rel = Relation(id="a-uses-b", source="a", relation=RelationType.USES, target="b")
    store.put_relation(rel)
    assert store.get_relation("a-uses-b").relation == RelationType.USES
    assert [r.id for r in store.relations_from("a")] == ["a-uses-b"]
    assert [r.id for r in store.relations_to("b")] == ["a-uses-b"]


# --- evidence -------------------------------------------------------------


def test_evidence_requires_source(store: KBStore) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        store.put_evidence(Evidence(id="e1", source_id="0" * 64,
                                    locator="L1-L5"))


def test_evidence_can_back_claim(store: KBStore) -> None:
    src = store.put_source(b"raw doc")
    ev = store.put_evidence(Evidence(id="e1", source_id=src.id,
                                     locator="L1-L5", quote="snippet"))
    # Claim cites the Evidence id, not the Source id — both forms work.
    store.put_claim(Claim(id="c1", text="t", evidence=[ev.id]))
    assert store.get_claim("c1").evidence == ["e1"]


# --- proposals ------------------------------------------------------------


def test_propose_claim_requires_evidence(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="at least one"):
        propose_claim(store, text="t", evidence=[], proposed_by="agent")


def test_propose_claim_rejects_unknown_source(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown"):
        propose_claim(store, text="t", evidence=["0" * 64], proposed_by="agent")


def test_propose_claim_dry_run_does_not_persist(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="hi", evidence=[src.id],
                       proposed_by="agent", dry_run=True)
    # File was never written.
    assert not (store.kb_dir / "proposed" / f"{pr.id}.yaml").exists()
    # But the audit log captured the dry-run.
    events = list(audit.read_events(store.kb_dir))
    assert any(e.event.endswith("dry_run") for e in events)


def test_approve_promotes_proposal_to_claim(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(
        store, text="auth uses JWT", evidence=[src.id], proposed_by="claude-code",
    )
    artifact = approve(store, pr.id, approved_by="plind-junior")
    assert isinstance(artifact, Claim)
    assert artifact.approved_by == "plind-junior"
    assert store.get_claim(artifact.id).text == "auth uses JWT"
    after = store.get_proposal(pr.id)
    assert after.status == ProposalStatus.APPROVED
    assert not (store.kb_dir / "proposed" / f"{pr.id}.yaml").exists()
    assert (store.kb_dir / "decided" / f"{pr.id}.yaml").exists()


def test_approve_writes_audit(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="u")
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "proposal.claim.create" in events
    assert "proposal.claim.approve" in events


def test_double_approve_rejected(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="u")
    with pytest.raises(ProposalError, match="not pending"):
        approve(store, pr.id, approved_by="u")


def test_approve_refuses_to_overwrite_existing_artifact(store: KBStore) -> None:
    # Regression for #11: approve() wrote the artifact before moving the
    # proposal to decided/. A crash between the two steps would leave the
    # proposal PENDING with the artifact already on disk, and a retry would
    # silently overwrite it with new approved_by / created_at metadata.
    src = store.put_source(b"e")
    store.put_claim(
        Claim(id="auth-uses-jwt", text="prior write", evidence=[src.id],
              approved_by="first-reviewer")
    )
    pr = propose_claim(
        store, text="auth uses JWT", evidence=[src.id], proposed_by="agent",
        slug_hint="auth-uses-jwt",
    )
    with pytest.raises(ProposalError, match="already exists"):
        approve(store, pr.id, approved_by="second-reviewer")
    survivor = store.get_claim("auth-uses-jwt")
    assert survivor.text == "prior write"
    assert survivor.approved_by == "first-reviewer"
    assert store.get_proposal(pr.id).status == ProposalStatus.PENDING


def test_reject_requires_reason(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    with pytest.raises(ProposalError, match="reason"):
        reject(store, pr.id, rejected_by="u", reason="   ")


def test_reject_recorded_with_reason(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    reject(store, pr.id, rejected_by="u", reason="duplicate of c1")
    after = store.get_proposal(pr.id)
    assert after.status == ProposalStatus.REJECTED
    assert after.decision_reason == "duplicate of c1"


def test_propose_entity_then_approve(store: KBStore) -> None:
    pr = propose_entity(store, name="My Project", entity_type="project",
                        proposed_by="a")
    e = approve(store, pr.id, approved_by="u")
    assert isinstance(e, Entity)
    assert e.type == EntityType.PROJECT


def test_propose_relation_then_approve(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="t2", evidence=[src.id]))
    pr = propose_relation(store, src="c1", relation="contradicts",
                          target="c2", proposed_by="a")
    r = approve(store, pr.id, approved_by="u")
    assert isinstance(r, Relation)
    assert r.relation == RelationType.CONTRADICTS


def test_propose_page_round_trip_through_approval(store: KBStore) -> None:
    src = store.put_source(b"e")
    claim = store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    pr = propose_page(
        store, title="Overview", body="body",
        claim_ids=[claim.id], proposed_by="a",
    )
    artifact = approve(store, pr.id, approved_by="u")
    assert isinstance(artifact, Page)
    assert store.get_page(artifact.id).body == "body"


# --- lifecycle ------------------------------------------------------------


def test_supersede_links_both_claims(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="old", text="old version", evidence=[src.id]))
    store.put_claim(Claim(id="new", text="new version", evidence=[src.id]))
    old, new = lifecycle.supersede(
        store, old_claim_id="old", new_claim_id="new", actor="u",
    )
    assert old.status == ClaimStatus.SUPERSEDED
    assert old.superseded_by == "new"
    assert "old" in new.supersedes
    # Graph relation also written.
    rels = store.list_relations()
    assert any(r.relation == RelationType.SUPERSEDES for r in rels)


def test_supersede_idempotent(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="old", text="o", evidence=[src.id]))
    store.put_claim(Claim(id="new", text="n", evidence=[src.id]))
    lifecycle.supersede(store, old_claim_id="old", new_claim_id="new", actor="u")
    lifecycle.supersede(store, old_claim_id="old", new_claim_id="new", actor="u")
    assert store.get_claim("new").supersedes == ["old"]


def test_contradict_marks_both_contested(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="a", text="x", evidence=[src.id]))
    store.put_claim(Claim(id="b", text="not x", evidence=[src.id]))
    a, b, rel = lifecycle.contradict(store, claim_a="a", claim_b="b", actor="u")
    assert a.status == ClaimStatus.CONTESTED
    assert b.status == ClaimStatus.CONTESTED
    assert "b" in a.contradicts and "a" in b.contradicts
    assert rel.relation == RelationType.CONTRADICTS


def test_archive_changes_status(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    c = lifecycle.archive(store, claim_id="c1", actor="u")
    assert c.status == ClaimStatus.ARCHIVED


def test_confirm_bumps_last_confirmed_at(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    c = lifecycle.confirm(store, claim_id="c1", actor="u")
    assert c.last_confirmed_at is not None
    assert c.status == ClaimStatus.ACTIONABLE  # promoted from working


def test_cite_resolves_source_and_evidence(store: KBStore) -> None:
    src = store.put_source(b"raw", title="design doc")
    ev = store.put_evidence(Evidence(id="ev1", source_id=src.id,
                                     locator="L1", quote="hello"))
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id, ev.id]))
    citations = lifecycle.cite(store, "c1")
    assert len(citations) == 2
    kinds = {
        c.get("kind") if isinstance(c, dict) else "evidence"
        for c in citations
    }
    assert "source" in kinds and "evidence" in kinds
