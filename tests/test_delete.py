"""Review-gated hard delete for durable artifacts (claim/page/entity/relation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import index_db
from vouch.models import Claim, Entity, EntityType, Page, ProposalKind, Relation, RelationType
from vouch.proposals import ProposalError, referenced_by
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
    # sanity: the fts row is present
    with index_db.open_db(store.kb_dir) as conn:
        pre = conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0]
        assert pre == 1

    with index_db.open_db(store.kb_dir) as conn:
        index_db.deindex(conn, kind="claim", id="c1")

    with index_db.open_db(store.kb_dir) as conn:
        assert conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0] == 0
        prov = conn.execute(
            "SELECT count(*) FROM prov_edges WHERE src_id='c1' OR dst_id='c1'"
        ).fetchone()[0]
        assert prov == 0


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
