"""Review-gated hard delete for durable artifacts (claim/page/entity/relation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.models import Claim, Entity, EntityType, Page, Relation, RelationType
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
