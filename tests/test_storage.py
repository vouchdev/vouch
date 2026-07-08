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
    Proposal,
    ProposalKind,
    ProposalStatus,
    Relation,
    RelationType,
)
from vouch.proposals import (
    ProposalError,
    approve,
    check_approvable,
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
    reject,
)
from vouch.storage import (
    KBNotFoundError,
    KBStore,
    _yaml_dump,
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


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_claim_model_rejects_empty_text(store: KBStore, bad: str) -> None:
    """Regression for #155: same shape as the #81 evidence validator, on the
    text field. Empty / whitespace-only text is rejected at the model layer so
    direct construction, store.put_claim, and bundle import all inherit it."""
    src = store.put_source(b"e")
    with pytest.raises(ValidationError, match="text must not be empty"):
        Claim(id="c1", text=bad, evidence=[src.id])


def test_put_claim_rejects_empty_text(store: KBStore) -> None:
    src = store.put_source(b"e")
    with pytest.raises(ValidationError, match="text must not be empty"):
        store.put_claim(Claim(id="c1", text="   ", evidence=[src.id]))
    assert not (store.kb_dir / "claims" / "c1.yaml").exists()


def test_update_claim_rejects_empty_text(store: KBStore) -> None:
    """A previously-populated claim cannot be mutated down to blank text and
    silently re-persisted — update_claim re-validates via model_validate."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="cited", evidence=[src.id]))
    persisted_before = (store.kb_dir / "claims" / "c1.yaml").read_text()

    c = store.get_claim("c1")
    c.text = "   "

    with pytest.raises(ValidationError, match="text must not be empty"):
        store.update_claim(c)
    assert (store.kb_dir / "claims" / "c1.yaml").read_text() == persisted_before


def test_claim_text_preserves_surrounding_whitespace_when_non_blank(
    store: KBStore,
) -> None:
    """The validator gates on emptiness only — it must not strip content."""
    src = store.put_source(b"e")
    c = Claim(id="c1", text="  padded text  ", evidence=[src.id])
    assert c.text == "  padded text  "


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


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_page_model_rejects_empty_title(bad: str) -> None:
    """Regression for #155 — empty / whitespace titles rejected on the model."""
    with pytest.raises(ValidationError, match="title must not be empty"):
        Page(id="p1", title=bad)


def test_put_page_rejects_empty_title(store: KBStore) -> None:
    with pytest.raises(ValidationError, match="title must not be empty"):
        store.put_page(Page(id="p1", title="   "))
    assert not (store.kb_dir / "pages" / "p1.md").exists()


# --- entities + relations -------------------------------------------------


def test_entity_round_trip(store: KBStore) -> None:
    e = store.put_entity(Entity(id="proj-foo", name="Foo", type=EntityType.PROJECT))
    back = store.get_entity(e.id)
    assert back.name == "Foo"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_entity_model_rejects_empty_name(bad: str) -> None:
    """Regression for #155 — empty / whitespace names rejected on the model."""
    with pytest.raises(ValidationError, match="name must not be empty"):
        Entity(id="e1", name=bad, type=EntityType.CONCEPT)


def test_put_entity_rejects_empty_name(store: KBStore) -> None:
    with pytest.raises(ValidationError, match="name must not be empty"):
        store.put_entity(Entity(id="e1", name="   ", type=EntityType.CONCEPT))
    assert not (store.kb_dir / "entities" / "e1.yaml").exists()


def test_relation_round_trip(store: KBStore) -> None:
    store.put_entity(Entity(id="a", name="A", type=EntityType.PROJECT))
    store.put_entity(Entity(id="b", name="B", type=EntityType.PROJECT))
    rel = Relation(id="a-uses-b", source="a", relation=RelationType.USES, target="b")
    store.put_relation(rel)
    assert store.get_relation("a-uses-b").relation == RelationType.USES
    assert [r.id for r in store.relations_from("a")] == ["a-uses-b"]
    assert [r.id for r in store.relations_to("b")] == ["a-uses-b"]


# --- graph integrity: relation / page write paths reject dangling refs ---
#
# The `dangling_relation` shape used to be reported by `health.lint` after
# the fact (`src/vouch/health.py:135-145`) but no writer enforced it, so
# every approve / lifecycle / bundle path silently landed broken edges.
# These regressions cover the structural counterpart of the #81 fix on
# the Relation + Page surface.


def test_put_relation_rejects_unknown_source_endpoint(store: KBStore) -> None:
    store.put_entity(Entity(id="real-target", name="T", type=EntityType.PROJECT))
    rel = Relation(id="r1", source="ghost", relation=RelationType.USES, target="real-target")
    with pytest.raises(ValueError, match="unknown source endpoint"):
        store.put_relation(rel)
    assert not (store.kb_dir / "relations" / "r1.yaml").exists()


def test_put_relation_rejects_unknown_target_endpoint(store: KBStore) -> None:
    store.put_entity(Entity(id="real-src", name="S", type=EntityType.PROJECT))
    rel = Relation(id="r2", source="real-src", relation=RelationType.USES, target="ghost")
    with pytest.raises(ValueError, match="unknown target endpoint"):
        store.put_relation(rel)
    assert not (store.kb_dir / "relations" / "r2.yaml").exists()


def test_put_relation_rejects_unknown_evidence_ref(store: KBStore) -> None:
    store.put_entity(Entity(id="x", name="X", type=EntityType.PROJECT))
    store.put_entity(Entity(id="y", name="Y", type=EntityType.PROJECT))
    rel = Relation(
        id="r3", source="x", relation=RelationType.USES, target="y",
        evidence=["ghost-source-or-evidence"],
    )
    with pytest.raises(ValueError, match="unknown source/evidence"):
        store.put_relation(rel)
    assert not (store.kb_dir / "relations" / "r3.yaml").exists()


def test_put_relation_accepts_source_id_as_endpoint(store: KBStore) -> None:
    # The Relation endpoint surface is documented in the README §"Object
    # model" as 'entities / claims / pages'; in practice Source ids are
    # also valid graph nodes (sources back claims and crystallize into
    # pages). Keep that surface explicit here.
    src = store.put_source(b"source-as-node", title="t")
    rel = Relation(
        id=f"r-{src.id[:8]}",
        source=src.id, relation=RelationType.REFERENCES, target=src.id,
        evidence=[src.id],
    )
    store.put_relation(rel)
    assert store.get_relation(rel.id).source == src.id


def test_put_relation_idempotent_validates_endpoints_on_first_write(
    store: KBStore,
) -> None:
    # Lifecycle ops (supersede / contradict) reach `put_relation_idempotent`
    # with both endpoints loaded via `get_claim`, so they always satisfy
    # the new gate. The negative case here proves a hand-built call with
    # a dangling endpoint is still rejected.
    store.put_entity(Entity(id="ok", name="OK", type=EntityType.PROJECT))
    rel = Relation(id="r-id", source="ok", relation=RelationType.USES, target="ghost")
    with pytest.raises(ValueError, match="unknown target endpoint"):
        store.put_relation_idempotent(rel)
    assert not (store.kb_dir / "relations" / "r-id.yaml").exists()


def test_put_relation_idempotent_skips_revalidation_on_existing_file(
    store: KBStore, tmp_path: Path,
) -> None:
    # If a relation already exists on disk, `put_relation_idempotent`
    # converges without re-checking endpoints. This protects supersede /
    # contradict retries from spurious failures if the linked claim was
    # later archived or retracted.
    store.put_entity(Entity(id="p", name="P", type=EntityType.PROJECT))
    store.put_entity(Entity(id="q", name="Q", type=EntityType.PROJECT))
    rel = Relation(id="r-pq", source="p", relation=RelationType.USES, target="q")
    store.put_relation(rel)
    # Now "remove" target from the KB.
    (store.kb_dir / "entities" / "q.yaml").unlink()
    # Repeat call must not raise even though target no longer exists.
    store.put_relation_idempotent(rel)


def test_put_page_rejects_unknown_entity_ref(store: KBStore) -> None:
    page = Page(id="p-bad-ent", title="t", body="b", entities=["ghost-entity"])
    with pytest.raises(ValueError, match="unknown entity"):
        store.put_page(page)
    assert not (store.kb_dir / "pages" / "p-bad-ent.md").exists()


def test_put_page_rejects_unknown_source_ref(store: KBStore) -> None:
    page = Page(id="p-bad-src", title="t", body="b", sources=["ghost-src"])
    with pytest.raises(ValueError, match="unknown source"):
        store.put_page(page)
    assert not (store.kb_dir / "pages" / "p-bad-src.md").exists()


def test_put_page_accepts_resolvable_entity_and_source_refs(
    store: KBStore,
) -> None:
    src = store.put_source(b"hello", title="hi")
    store.put_entity(Entity(id="ent1", name="E", type=EntityType.CONCEPT))
    page = Page(
        id="p-ok", title="t", body="b",
        entities=["ent1"], sources=[src.id],
    )
    store.put_page(page)
    assert store.get_page("p-ok").entities == ["ent1"]
    assert store.get_page("p-ok").sources == [src.id]


# --- claim graph references (#196) ---------------------------------------
#
# put_claim already rejects unresolvable `evidence`; these cover the Claim's
# *other* four reference fields — entities / supersedes / superseded_by /
# contradicts — which #124 left unchecked even though fsck declares dangling
# supersedes/superseded_by/contradicts as error-severity findings.


def test_put_claim_rejects_unknown_entity_ref(store: KBStore) -> None:
    src = store.put_source(b"e")
    bad = Claim(id="c-ent", text="t", evidence=[src.id], entities=["ghost"])
    with pytest.raises(ValueError, match="unknown entity"):
        store.put_claim(bad)
    assert not (store.kb_dir / "claims" / "c-ent.yaml").exists()


def test_put_claim_rejects_unknown_supersedes_ref(store: KBStore) -> None:
    src = store.put_source(b"e")
    bad = Claim(id="c-sup", text="t", evidence=[src.id], supersedes=["ghost"])
    with pytest.raises(ValueError, match="unknown claim"):
        store.put_claim(bad)
    assert not (store.kb_dir / "claims" / "c-sup.yaml").exists()


def test_put_claim_rejects_unknown_superseded_by_ref(store: KBStore) -> None:
    src = store.put_source(b"e")
    bad = Claim(id="c-sb", text="t", evidence=[src.id], superseded_by="ghost")
    with pytest.raises(ValueError, match="unknown claim"):
        store.put_claim(bad)
    assert not (store.kb_dir / "claims" / "c-sb.yaml").exists()


def test_put_claim_rejects_unknown_contradicts_ref(store: KBStore) -> None:
    src = store.put_source(b"e")
    bad = Claim(id="c-con", text="t", evidence=[src.id], contradicts=["ghost"])
    with pytest.raises(ValueError, match="unknown claim"):
        store.put_claim(bad)
    assert not (store.kb_dir / "claims" / "c-con.yaml").exists()


def test_put_claim_accepts_resolvable_graph_refs(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_entity(Entity(id="ent1", name="E", type=EntityType.CONCEPT))
    store.put_claim(Claim(id="base", text="b", evidence=[src.id]))
    ok = Claim(
        id="c-ok", text="t", evidence=[src.id],
        entities=["ent1"], contradicts=["base"],
    )
    store.put_claim(ok)
    assert store.get_claim("c-ok").entities == ["ent1"]
    assert store.get_claim("c-ok").contradicts == ["base"]


def test_update_claim_rejects_in_place_mutation_to_dangling_ref(
    store: KBStore,
) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    c = store.get_claim("c1")
    c.contradicts = ["ghost"]  # mutate after load — bypasses model validators
    with pytest.raises(ValueError, match="unknown claim"):
        store.update_claim(c)
    # On-disk claim is untouched.
    assert store.get_claim("c1").contradicts == []


def test_lifecycle_contradict_round_trips_after_guard(store: KBStore) -> None:
    """Honest lifecycle writes stay green: supersede/contradict load both
    ends via get_claim, so their links always resolve."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="a", text="a", evidence=[src.id]))
    store.put_claim(Claim(id="b", text="b", evidence=[src.id]))
    lifecycle.contradict(store, claim_a="a", claim_b="b", actor="tester")
    assert store.get_claim("a").contradicts == ["b"]
    assert store.get_claim("b").contradicts == ["a"]


# --- lifecycle atomicity + batch-approve precheck (Codex review) --------


def _write_poisoned_claim(store: KBStore, claim: Claim) -> None:
    """Plant a claim YAML directly, bypassing put_claim's ref guard."""
    (store.kb_dir / "claims" / f"{claim.id}.yaml").write_text(
        _yaml_dump(claim.model_dump(mode="json"))
    )


def test_supersede_atomic_when_new_has_legacy_dangling_ref(
    store: KBStore,
) -> None:
    """supersede must not leave old.superseded_by written when update_claim(new) raises."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="old", text="o", evidence=[src.id]))
    # Plant `new` with a legacy dangling entity ref straight to disk.
    _write_poisoned_claim(store, Claim(
        id="new", text="n", evidence=[src.id], entities=["ghost-entity"],
    ))

    audit_path = store.kb_dir / "audit.log.jsonl"
    audit_before = audit_path.read_text() if audit_path.exists() else ""

    with pytest.raises(ValueError, match="unknown entity"):
        lifecycle.supersede(
            store, old_claim_id="old", new_claim_id="new", actor="tester",
        )

    # Atomicity: `old` was not touched.
    old_after = store.get_claim("old")
    assert old_after.status == ClaimStatus.WORKING
    assert old_after.superseded_by is None
    # No relation written.
    assert not (
        store.kb_dir / "relations" / "new--supersedes--old.yaml"
    ).exists()
    # No `claim.supersede` audit event recorded.
    audit_after = audit_path.read_text() if audit_path.exists() else ""
    assert "claim.supersede" not in audit_after[len(audit_before):]


def test_contradict_atomic_when_b_has_legacy_dangling_ref(
    store: KBStore,
) -> None:
    """contradict must not leave a.contradicts written when update_claim(b) raises."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="a", text="a", evidence=[src.id]))
    _write_poisoned_claim(store, Claim(
        id="b", text="b", evidence=[src.id], entities=["ghost-entity"],
    ))

    audit_path = store.kb_dir / "audit.log.jsonl"
    audit_before = audit_path.read_text() if audit_path.exists() else ""

    with pytest.raises(ValueError, match="unknown entity"):
        lifecycle.contradict(
            store, claim_a="a", claim_b="b", actor="tester",
        )

    a_after = store.get_claim("a")
    assert a_after.status == ClaimStatus.WORKING
    assert a_after.contradicts == []
    assert not (
        store.kb_dir / "relations" / "a--contradicts--b.yaml"
    ).exists()
    audit_after = audit_path.read_text() if audit_path.exists() else ""
    assert "claim.contradict" not in audit_after[len(audit_before):]


def test_check_approvable_catches_claim_with_dangling_entity_ref(
    store: KBStore,
) -> None:
    """Batch precheck blocks a claim proposal whose entities won't resolve."""
    src = store.put_source(b"e")
    pr = propose_claim(
        store, text="t", evidence=[src.id],
        entities=["ghost-entity"],  # would-be dangling at approve time
        proposed_by="agent",
    )
    reason = check_approvable(store, pr.id, approved_by="reviewer")
    assert reason is not None
    assert "unknown entity" in reason


def test_check_approvable_catches_relation_proposal_filed_directly(
    store: KBStore,
) -> None:
    """Defense-in-depth for legacy proposals that bypassed propose_relation's gate."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="real", text="r", evidence=[src.id]))
    from vouch.proposals import new_proposal_id
    pr = Proposal(
        id=new_proposal_id(),
        kind=ProposalKind.RELATION,
        proposed_by="agent",
        payload={
            "id": "real--uses--ghost",
            "source": "real",
            "relation": "uses",
            "target": "ghost",
            "confidence": 0.7,
            "evidence": [],
        },
    )
    store.put_proposal(pr)
    reason = check_approvable(store, pr.id, approved_by="reviewer")
    assert reason is not None
    assert "ghost" in reason or "unknown" in reason


def test_check_approvable_clean_for_well_formed_proposal(
    store: KBStore,
) -> None:
    """Positive guard: an honest proposal still passes the precheck."""
    src = store.put_source(b"e")
    store.put_entity(Entity(id="ent-ok", name="E", type=EntityType.CONCEPT))
    pr = propose_claim(
        store, text="ok", evidence=[src.id], entities=["ent-ok"],
        proposed_by="agent",
    )
    assert check_approvable(store, pr.id, approved_by="reviewer") is None


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


def test_propose_claim_survives_similarity_import_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Base CI install has no numpy; similarity import must not break propose."""
    import builtins

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if fromlist and "find_similar_on_propose" in fromlist:
            raise ImportError("similarity unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    src = store.put_source(b"e")
    result = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    assert result.warnings == []
    assert result.id


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


def test_propose_relation_rejects_unknown_source(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c-real", text="t", evidence=[src.id]))
    with pytest.raises(ProposalError, match="unknown relation source endpoint"):
        propose_relation(
            store, src="ghost", relation="uses", target="c-real",
            proposed_by="a",
        )


def test_propose_relation_rejects_unknown_target(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c-real", text="t", evidence=[src.id]))
    with pytest.raises(ProposalError, match="unknown relation target endpoint"):
        propose_relation(
            store, src="c-real", relation="uses", target="ghost",
            proposed_by="a",
        )


def test_propose_relation_rejects_unknown_evidence_ref(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="t2", evidence=[src.id]))
    with pytest.raises(ProposalError, match="unknown source/evidence"):
        propose_relation(
            store, src="c1", relation="contradicts", target="c2",
            evidence=["nonexistent"], proposed_by="a",
        )


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


def test_propose_page_rejects_unknown_claim_ref(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown claim id"):
        propose_page(
            store, title="t", body="b",
            claim_ids=["ghost-claim"], proposed_by="a",
        )


def test_propose_page_rejects_unknown_entity_ref(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown entity id"):
        propose_page(
            store, title="t", body="b",
            entity_ids=["ghost-entity"], proposed_by="a",
        )


def test_propose_page_rejects_unknown_source_ref(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown source id"):
        propose_page(
            store, title="t", body="b",
            source_ids=["ghost-source"], proposed_by="a",
        )


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


# --- resilience: a single corrupt file must not break bulk listing --------


def test_list_proposals_skips_unreadable_file(store: KBStore) -> None:
    """One unparseable proposal file must not take down `vouch pending`."""
    src = store.put_source(b"evidence")
    good = propose_claim(store, text="good", evidence=[src.id],
                         proposed_by="agent")

    # a raw U+0080 (C1 control byte) — pyyaml's loader rejects it even though
    # its dumper would have escaped it. mirrors a hand-edited / mojibake file.
    corrupt = store.kb_dir / "proposed" / "20990101-000000-corrupt.yaml"
    corrupt.write_bytes(b"text: bad\xc2\x80value\n")

    pending = store.list_proposals(ProposalStatus.PENDING)
    assert [p.id for p in pending] == [good.id]


def test_list_claims_skips_unreadable_file(store: KBStore) -> None:
    """Same resilience for durable claim listing (vouch search/status)."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c-ok", text="x", evidence=[src.id]))
    (store.kb_dir / "claims" / "c-bad.yaml").write_bytes(
        b"text: bad\xc2\x80value\n")

    claims = store.list_claims()
    assert [c.id for c in claims] == ["c-ok"]
