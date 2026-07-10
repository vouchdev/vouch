"""Read-only entity-connectivity report — vouch entities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import entity_graph
from vouch.cli import cli
from vouch.models import Claim, Entity, EntityType, Page, Relation, RelationType
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _ent(store: KBStore, eid: str, name: str) -> None:
    store.put_entity(Entity(id=eid, name=name, type=EntityType.CONCEPT))


def _rel(store: KBStore, rid: str, src: str, rel: RelationType, tgt: str) -> None:
    store.put_relation(Relation(id=rid, source=src, relation=rel, target=tgt))


def _row(report: entity_graph.EntityGraphReport, eid: str) -> entity_graph.EntityConnectivity:
    for r in report.most_connected + report.orphans:
        if r.id == eid:
            return r
    raise AssertionError(f"{eid} not in report")


def test_hub_connectivity_sums_all_sources(store: KBStore) -> None:
    src = store.put_source(b"x")
    _ent(store, "hub", "Hub")
    _ent(store, "other", "Other")
    store.put_claim(Claim(id="c1", text="a", evidence=[src.id], entities=["hub"]))
    store.put_claim(Claim(id="c2", text="b", evidence=[src.id], entities=["hub"]))
    _rel(store, "r1", "hub", RelationType.RELATES_TO, "other")
    store.put_page(Page(id="p1", title="w", entities=["hub"]))

    row = _row(entity_graph.build(store), "hub")
    assert row.claim_mentions == 2
    assert row.relations == 1
    assert row.page_references == 1
    assert row.connections == 4
    assert row.is_orphan is False


def test_self_loop_counts_once(store: KBStore) -> None:
    _ent(store, "e", "E")
    _rel(store, "r1", "e", RelationType.SIMILAR_TO, "e")
    assert _row(entity_graph.build(store), "e").relations == 1


def test_relation_increments_both_endpoints(store: KBStore) -> None:
    _ent(store, "a", "A")
    _ent(store, "b", "B")
    _rel(store, "r1", "a", RelationType.DEPENDS_ON, "b")
    report = entity_graph.build(store)
    assert _row(report, "a").relations == 1
    assert _row(report, "b").relations == 1


def test_non_entity_endpoint_is_not_counted_as_entity(store: KBStore) -> None:
    src = store.put_source(b"x")
    _ent(store, "e", "E")
    store.put_claim(Claim(id="c1", text="a", evidence=[src.id]))
    # a relation from an entity to a claim: the entity gets the edge, the claim
    # id must not surface as a phantom entity row.
    _rel(store, "r1", "e", RelationType.MENTIONS, "c1")
    report = entity_graph.build(store)
    assert report.entities_total == 1
    assert _row(report, "e").relations == 1


def test_claim_mention_deduped_per_claim(store: KBStore) -> None:
    src = store.put_source(b"x")
    _ent(store, "e", "E")
    # a claim naming the same entity twice counts the entity once.
    store.put_claim(Claim(id="c1", text="a", evidence=[src.id], entities=["e", "e"]))
    assert _row(entity_graph.build(store), "e").claim_mentions == 1


def test_orphan_entity(store: KBStore) -> None:
    src = store.put_source(b"x")
    _ent(store, "live", "Live")
    _ent(store, "orphan", "Orphan")
    store.put_claim(Claim(id="c1", text="a", evidence=[src.id], entities=["live"]))

    report = entity_graph.build(store)
    assert report.entities_total == 2
    assert report.connected_total == 1
    assert report.orphan_total == 1
    assert [r.id for r in report.orphans] == ["orphan"]
    assert _row(report, "orphan").is_orphan is True


def test_most_connected_ordering_and_truncation(store: KBStore) -> None:
    src = store.put_source(b"x")
    for i in range(3):
        _ent(store, f"e{i}", f"E{i}")
        store.put_claim(Claim(id=f"c{i}", text="t", evidence=[src.id], entities=[f"e{i}"]))
    # give e0 an extra connection so it ranks first.
    _ent(store, "z", "Z")
    _rel(store, "r0", "e0", RelationType.RELATES_TO, "z")

    report = entity_graph.build(store, limit=1)
    assert report.most_connected[0].id == "e0"
    assert len(report.most_connected) == 1
    assert "... and" in entity_graph.render_text(report)


def test_to_dict_schema(store: KBStore) -> None:
    _ent(store, "e", "E")
    body = entity_graph.build(store).to_dict()
    assert set(body) == {
        "generated_at",
        "limit",
        "entities_total",
        "connected_total",
        "orphan_total",
        "most_connected",
        "orphans",
    }
    assert set(body["orphans"][0]) == {
        "id",
        "name",
        "type",
        "created_at",
        "claim_mentions",
        "relations",
        "page_references",
        "connections",
        "is_orphan",
    }


def test_empty_kb(store: KBStore) -> None:
    report = entity_graph.build(store)
    assert report.entities_total == 0
    assert report.most_connected == []
    assert "orphaned entities (0)" in entity_graph.render_text(report)


def test_cli_entities_json(store: KBStore) -> None:
    src = store.put_source(b"x")
    _ent(store, "e", "E")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id], entities=["e"]))
    result = CliRunner().invoke(cli, ["entities", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["connected_total"] == 1
    assert data["most_connected"][0]["id"] == "e"


def test_cli_entities_text_and_markdown(store: KBStore) -> None:
    _ent(store, "lonely", "Lonely")
    text = CliRunner().invoke(cli, ["entities"])
    assert text.exit_code == 0, text.output
    assert "orphaned entities (1)" in text.output
    assert "Lonely" in text.output
    md = CliRunner().invoke(cli, ["entities", "--format", "markdown"])
    assert md.exit_code == 0, md.output
    assert "# entity graph" in md.output
