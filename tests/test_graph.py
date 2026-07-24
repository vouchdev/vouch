"""Graph traversal — neighbors and context expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import context, graph, health, lifecycle
from vouch.models import (
    Claim,
    Entity,
    EntityType,
    Page,
    PageType,
    Relation,
    RelationType,
)
from vouch.storage import ArtifactNotFoundError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_find_neighbors_via_relation(store: KBStore) -> None:
    store.put_entity(Entity(id="auth", name="Auth", type=EntityType.SYSTEM))
    store.put_entity(Entity(id="jwt", name="JWT", type=EntityType.CONCEPT))
    store.put_relation(Relation(
        id="auth-uses-jwt",
        source="auth",
        relation=RelationType.USES,
        target="jwt",
    ))
    result = graph.find_neighbors(store, "auth", depth=1)
    assert result["kind"] == "entity"
    assert {n["id"] for n in result["nodes"]} == {"jwt"}
    assert result["edges"][0]["relation"] == "uses"


def test_find_neighbors_depth_two(store: KBStore) -> None:
    for eid in ("a", "b", "c"):
        store.put_entity(Entity(id=eid, name=eid.upper(), type=EntityType.CONCEPT))
    store.put_relation(Relation(
        id="a-b", source="a", relation=RelationType.DEPENDS_ON, target="b",
    ))
    store.put_relation(Relation(
        id="b-c", source="b", relation=RelationType.DEPENDS_ON, target="c",
    ))
    one_hop = graph.find_neighbors(store, "a", depth=1)
    assert {n["id"] for n in one_hop["nodes"]} == {"b"}

    two_hop = graph.find_neighbors(store, "a", depth=2)
    assert {n["id"] for n in two_hop["nodes"]} == {"b", "c"}


def test_find_neighbors_rel_type_filter(store: KBStore) -> None:
    store.put_entity(Entity(id="a", name="A", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="b", name="B", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="c", name="C", type=EntityType.CONCEPT))
    store.put_relation(Relation(
        id="uses", source="a", relation=RelationType.USES, target="b",
    ))
    store.put_relation(Relation(
        id="blocks", source="a", relation=RelationType.BLOCKS, target="c",
    ))
    result = graph.find_neighbors(
        store, "a", depth=1, rel_types=["uses"],
    )
    assert {n["id"] for n in result["nodes"]} == {"b"}


def test_find_neighbors_claim_structural_links(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_entity(Entity(id="auth-svc", name="Auth", type=EntityType.SYSTEM))
    store.put_claim(Claim(
        id="jwt-fact",
        text="Auth uses JWT",
        evidence=[src.id],
        entities=["auth-svc"],
    ))
    result = graph.find_neighbors(store, "jwt-fact", depth=1)
    assert {n["id"] for n in result["nodes"]} == {"auth-svc"}
    assert result["edges"][0]["relation"] == "mentions"


def test_find_neighbors_excludes_superseded_claims(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="old", text="v1", evidence=[src.id]))
    store.put_claim(Claim(id="new", text="v2", evidence=[src.id]))
    lifecycle.supersede(store, old_claim_id="old", new_claim_id="new", actor="r")
    result = graph.find_neighbors(store, "new", depth=1)
    assert {n["id"] for n in result["nodes"]} == set()
    assert "old" not in {n["id"] for n in result["nodes"]}
    # the edge to the excluded neighbor must not leak either -- a client
    # that trusts `nodes` as the visible set would otherwise see a "supersedes"
    # edge pointing at a claim id it was never told exists.
    assert result["edges"] == []


def test_find_neighbors_unknown_node_raises(store: KBStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        graph.find_neighbors(store, "missing", depth=1)


def test_context_expand_graph_adds_neighbors(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_entity(Entity(id="auth", name="Auth", type=EntityType.SYSTEM))
    store.put_claim(Claim(
        id="jwt-claim",
        text="JWT tokens secure the API",
        evidence=[src.id],
    ))
    store.put_relation(Relation(
        id="claim-uses-auth",
        source="jwt-claim",
        relation=RelationType.REFERENCES,
        target="auth",
    ))
    health.rebuild_index(store)

    pack = context.build_context_pack(
        store, query="JWT tokens", limit=5, expand_graph=True,
    )
    ids = {it["id"] for it in pack["items"]}
    assert "jwt-claim" in ids
    assert "auth" in ids
    assert any(it["backend"] == "graph" for it in pack["items"])
    assert any("graph expansion" in w for w in pack["warnings"])


def test_context_expand_graph_includes_page_claims(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="detail fact", evidence=[src.id]))
    store.put_page(Page(
        id="overview",
        title="Overview",
        type=PageType.CONCEPT,
        body="Summary",
        claims=["c1"],
    ))
    health.rebuild_index(store)

    pack = context.build_context_pack(
        store, query="Overview", limit=5, expand_graph=True,
    )
    ids = {it["id"] for it in pack["items"]}
    assert "overview" in ids
    assert "c1" in ids


def test_jsonl_kb_neighbors(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch.jsonl_server import handle_request

    monkeypatch.chdir(store.root)
    store.put_entity(Entity(id="x", name="X", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="y", name="Y", type=EntityType.CONCEPT))
    store.put_relation(Relation(
        id="x-y", source="x", relation=RelationType.USES, target="y",
    ))
    resp = handle_request({
        "id": "n1",
        "method": "kb.neighbors",
        "params": {"node_id": "x", "depth": 1},
    })
    assert resp["ok"] is True
    assert {n["id"] for n in resp["result"]["nodes"]} == {"y"}
