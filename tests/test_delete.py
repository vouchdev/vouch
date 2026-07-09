"""Review-gated hard delete for durable artifacts (claim/page/entity/relation)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import audit, capabilities, index_db
from vouch.jsonl_server import handle_request
from vouch.models import (
    Claim,
    Entity,
    EntityType,
    Page,
    ProposalKind,
    ProposalStatus,
    Relation,
    RelationType,
)
from vouch.proposals import ProposalError, approve, check_approvable, propose_delete, referenced_by
from vouch.server import kb_propose_delete
from vouch.storage import ArtifactNotFoundError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _claim(store: KBStore, cid: str = "c1", text: str = "a claim") -> Claim:
    src = store.put_source(b"src-bytes")
    return store.put_claim(Claim(id=cid, text=text, evidence=[src.id]))


def test_delete_claim_removes_file(store: KBStore) -> None:
    _claim(store, "c1")
    assert store._claim_path("c1").exists()
    store.delete_claim("c1")
    assert not store._claim_path("c1").exists()
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")


def test_delete_claim_missing_raises(store: KBStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.delete_claim("nope")


def test_delete_page_removes_file(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="hi"))
    assert store._page_path("p1").exists()
    store.delete_page("p1")
    assert not store._page_path("p1").exists()


def test_delete_entity_removes_file(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    store.delete_entity("e1")
    assert not store._entity_path("e1").exists()


def test_delete_relation_removes_file(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    rel = store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    store.delete_relation(rel.id)
    assert not store._relation_path(rel.id).exists()


def test_deindex_removes_fts_and_prov(store: KBStore) -> None:
    _claim(store, "c1", "searchable claim text")
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id="c1", text="searchable claim text",
            type="observation", status="working", tags=[],
        )
        index_db.index_prov_edge(conn, src_id="c1", dst_id="src-x", kind="cites")
        index_db.index_prov_edge(conn, src_id="other", dst_id="c1", kind="cites")
        conn.execute(
            "INSERT INTO embedding_index "
            "(kind, id, vec, content_hash, model, model_version, dim, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim", "c1", b"\x00\x00\x80?", "hash-c1", "test-model", "v1", 1,
             "2026-07-09T00:00:00Z"),
        )
    # sanity: the fts and embedding rows are present
    with index_db.open_db(store.kb_dir) as conn:
        pre = conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0]
        assert pre == 1
        pre_emb = conn.execute(
            "SELECT count(*) FROM embedding_index WHERE kind='claim' AND id='c1'"
        ).fetchone()[0]
        assert pre_emb == 1

    with index_db.open_db(store.kb_dir) as conn:
        index_db.deindex(conn, kind="claim", id="c1")

    with index_db.open_db(store.kb_dir) as conn:
        assert conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0] == 0
        prov = conn.execute(
            "SELECT count(*) FROM prov_edges WHERE src_id='c1' OR dst_id='c1'"
        ).fetchone()[0]
        assert prov == 0
        emb = conn.execute(
            "SELECT count(*) FROM embedding_index WHERE kind='claim' AND id='c1'"
        ).fetchone()[0]
        assert emb == 0


def test_deindex_relation_only_touches_embedding_and_prov(store: KBStore) -> None:
    # relations have no FTS table; deindex must not raise for them.
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_prov_edge(conn, src_id="r1", dst_id="c2", kind="edge")
        index_db.deindex(conn, kind="relation", id="r1")
        assert conn.execute(
            "SELECT count(*) FROM prov_edges WHERE src_id='r1' OR dst_id='r1'"
        ).fetchone()[0] == 0


def test_proposalkind_has_delete() -> None:
    assert ProposalKind.DELETE.value == "delete"


def test_claim_referenced_by_page(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    refs = referenced_by(store, "claim", "c1")
    assert any("p1" in r for r in refs)


def test_claim_referenced_by_relation_and_supersede(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c2--supports--c1", source="c2",
        relation=RelationType.SUPPORTS, target="c1",
    ))
    refs = referenced_by(store, "claim", "c1")
    assert any("relation" in r for r in refs)


def test_unreferenced_claim_is_deletable(store: KBStore) -> None:
    _claim(store, "lonely")
    assert referenced_by(store, "claim", "lonely") == []


def test_entity_referenced_by_claim(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    src = store.put_source(b"s")
    store.put_claim(Claim(id="c1", text="mentions e1", evidence=[src.id], entities=["e1"]))
    refs = referenced_by(store, "entity", "e1")
    assert any("c1" in r for r in refs)


def test_relation_never_blocked(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    # nothing points at an edge → always deletable
    assert referenced_by(store, "relation", "c1--supports--c2") == []


def test_referenced_by_unknown_kind_raises(store: KBStore) -> None:
    with pytest.raises(ProposalError):
        referenced_by(store, "source", "x")


def test_propose_delete_files_pending(store: KBStore) -> None:
    _claim(store, "c1", "delete me")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    assert pr.kind is ProposalKind.DELETE
    assert pr.status is ProposalStatus.PENDING
    assert pr.payload["target_kind"] == "claim"
    assert pr.payload["id"] == "c1"
    assert pr.payload["snapshot"]["text"] == "delete me"
    # still pending in the queue
    assert any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))


def test_propose_delete_unknown_target_raises(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown claim id"):
        propose_delete(store, target_kind="claim", target_id="ghost", proposed_by="a")


def test_propose_delete_bad_kind_raises(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown target_kind"):
        propose_delete(store, target_kind="source", target_id="x", proposed_by="a")


def test_propose_delete_referenced_claim_blocked(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ProposalError, match="referenced by"):
        propose_delete(store, target_kind="claim", target_id="c1", proposed_by="a")


def test_propose_delete_claim_block_hints_supersede(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ProposalError, match="supersede"):
        propose_delete(store, target_kind="claim", target_id="c1", proposed_by="a")


def test_propose_delete_dry_run_writes_nothing(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(
        store, target_kind="claim", target_id="c1",
        proposed_by="a", dry_run=True,
    )
    assert store.list_proposals(ProposalStatus.PENDING) == []
    assert pr.id  # id is still returned for preview


def _propose_and_approve_delete(store: KBStore, kind: str, tid: str) -> None:
    pr = propose_delete(store, target_kind=kind, target_id=tid, proposed_by="agent")
    approve(store, pr.id, approved_by="reviewer")


def test_approve_delete_removes_claim_and_indexes(store: KBStore) -> None:
    _claim(store, "c1", "gone soon")
    _propose_and_approve_delete(store, "claim", "c1")
    assert not store._claim_path("c1").exists()
    with index_db.open_db(store.kb_dir) as conn:
        assert conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0] == 0
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "claim.delete" in events


def test_approve_delete_page(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="x"))
    _propose_and_approve_delete(store, "page", "p1")
    assert not store._page_path("p1").exists()
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "page.delete" in events


def test_approve_delete_entity(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    _propose_and_approve_delete(store, "entity", "e1")
    assert not store._entity_path("e1").exists()
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "entity.delete" in events


def test_approve_delete_relation(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    _propose_and_approve_delete(store, "relation", "c1--supports--c2")
    assert not store._relation_path("c1--supports--c2").exists()
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "relation.delete" in events


def test_approve_rechecks_reference_added_after_propose(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    # a page starts referencing c1 AFTER the proposal was filed
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ProposalError, match="still referenced"):
        approve(store, pr.id, approved_by="reviewer")
    # target survives, proposal stays pending
    assert store._claim_path("c1").exists()
    assert any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))


def test_approve_delete_idempotent_when_already_gone(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    store.delete_claim("c1")  # simulate a crash-retry: file already removed
    result = approve(store, pr.id, approved_by="reviewer")
    assert result.id == "c1"
    # proposal is finalized (moved out of pending)
    assert not any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))


def test_approve_delete_idempotent_path_deindexes(store: KBStore) -> None:
    """Guards a crash landing between deleter() and deindex(): the retry must
    still converge the derived index, not just short-circuit on the missing
    file. Without the fix, this hits the early return in the
    ArtifactNotFoundError branch and the stale claims_fts row survives."""
    _claim(store, "c1", "gone soon")
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id="c1", text="gone soon",
            type="observation", status="working", tags=[],
        )
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    # simulate a crash after the file unlink but before the original deindex
    store.delete_claim("c1")
    with index_db.open_db(store.kb_dir) as conn:
        pre = conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0]
        assert pre == 1  # sanity: the fts row outlives the file
    approve(store, pr.id, approved_by="reviewer")
    with index_db.open_db(store.kb_dir) as conn:
        assert conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0] == 0


def test_delete_forbids_self_approval(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="same")
    with pytest.raises(ProposalError, match="forbidden_self_approval"):
        approve(store, pr.id, approved_by="same")


def _trusted_agent_with_protected_voice(store: KBStore) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg["review"] = {"approver_role": "trusted-agent"}
    cfg["page_kinds"] = {"voice": {"protected": True}}
    store.config_path.write_text(yaml.safe_dump(cfg))


def test_delete_protected_page_blocks_self_approval_despite_trusted_agent(
    store: KBStore,
) -> None:
    """Deleting a protected page must need a distinct reviewer, exactly like
    editing one — otherwise the trusted-agent opt-out lets a proposer remove
    a policy-bearing page that it could not have changed on its own."""
    _trusted_agent_with_protected_voice(store)
    store.put_page(Page(id="p1", title="email voice", body="b", type="voice"))
    pr = propose_delete(store, target_kind="page", target_id="p1", proposed_by="agent")
    reason = check_approvable(store, pr.id, approved_by="agent")
    assert reason is not None and "protected" in reason
    with pytest.raises(ProposalError, match="protected"):
        approve(store, pr.id, approved_by="agent")
    # the page survives and the proposal stays pending
    assert store._page_path("p1").exists()
    assert any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))
    # a distinct reviewer can still approve the delete
    approve(store, pr.id, approved_by="reviewer")
    assert not store._page_path("p1").exists()


def test_delete_protected_page_guard_holds_when_target_already_gone(
    store: KBStore,
) -> None:
    """The idempotent already-gone path finalizes via the snapshot, so the
    protected-kind gate must read the kind from the snapshot too."""
    _trusted_agent_with_protected_voice(store)
    store.put_page(Page(id="p1", title="email voice", body="b", type="voice"))
    pr = propose_delete(store, target_kind="page", target_id="p1", proposed_by="agent")
    store.delete_page("p1")  # crash-retry: file already removed
    with pytest.raises(ProposalError, match="protected"):
        approve(store, pr.id, approved_by="agent")
    # a distinct reviewer still finalizes the idempotent approve
    result = approve(store, pr.id, approved_by="reviewer")
    assert result.id == "p1"


def test_delete_unprotected_page_keeps_trusted_agent_opt_out(store: KBStore) -> None:
    _trusted_agent_with_protected_voice(store)
    store.put_page(Page(id="p2", title="scratch notes", body="b"))
    pr = propose_delete(store, target_kind="page", target_id="p2", proposed_by="agent")
    approve(store, pr.id, approved_by="agent")
    assert not store._page_path("p2").exists()


def test_check_approvable_flags_referenced_delete(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    reason = check_approvable(store, pr.id, approved_by="reviewer")
    assert reason is not None and "referenced by" in reason


def test_method_registered_in_capabilities() -> None:
    assert "kb.propose_delete" in capabilities.METHODS


def test_jsonl_propose_delete_end_to_end(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", "kill via jsonl")
    resp = handle_request({
        "id": "r1",
        "method": "kb.propose_delete",
        "params": {"target_kind": "claim", "target_id": "c1"},
    })
    assert resp["ok"] is True, resp
    result = resp["result"]
    assert result["kind"] == "delete"
    assert result["status"] == "pending"
    assert result["proposal_id"]


def test_mcp_propose_delete_referenced_claim_raises_value_error(
    store: KBStore, monkeypatch,
) -> None:
    """The mcp tool must translate ProposalError -> ValueError like its siblings."""
    monkeypatch.chdir(store.root)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ValueError, match="referenced by"):
        kb_propose_delete(target_kind="claim", target_id="c1")


def test_mcp_propose_delete_dry_run_writes_nothing(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", "dry run me")
    result = kb_propose_delete(target_kind="claim", target_id="c1", dry_run=True)
    assert result["dry_run"] is True
    assert result["status"] == "pending"
    assert result["proposal_id"]
    assert store.list_proposals(ProposalStatus.PENDING) == []
