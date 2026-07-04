"""Backlink reconciliation — propose missing reverse relations (issue #307)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch.lifecycle import reconcile_backlinks
from vouch.models import Entity, EntityType, ProposalKind, ProposalStatus, RelationType
from vouch.proposals import approve, propose_relation
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _entity(store: KBStore, id_: str) -> Entity:
    e = Entity(id=id_, name=id_, type=EntityType.CONCEPT)
    store.put_entity(e)
    return e


def _approved_relation(store: KBStore, *, src: str, relation: str, target: str):
    pr = propose_relation(store, src=src, relation=relation, target=target, proposed_by="seed")
    return approve(store, pr.id, approved_by="reviewer")


def _pending_relation_proposals(store: KBStore) -> list:
    pending = store.list_proposals(ProposalStatus.PENDING)
    return [p for p in pending if p.kind == ProposalKind.RELATION]


def test_directed_gap_is_proposed(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    rel = _approved_relation(store, src="a", relation=RelationType.DEPENDS_ON.value, target="b")

    result = reconcile_backlinks(store)

    assert result.checked == 1
    assert len(result.proposed) == 1
    pr = result.proposed[0]
    assert pr.payload["source"] == "b"
    assert pr.payload["relation"] == RelationType.BLOCKS.value
    assert pr.payload["target"] == "a"
    assert pr.proposed_by == "reconcile"
    assert rel.id in (pr.rationale or "")
    # It's a real pending proposal on disk, not just an in-memory result.
    pending = _pending_relation_proposals(store)
    assert len(pending) == 1
    assert pending[0].id == pr.id


def test_already_mirrored_edge_is_skipped(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    _approved_relation(store, src="a", relation=RelationType.DEPENDS_ON.value, target="b")
    _approved_relation(store, src="b", relation=RelationType.BLOCKS.value, target="a")

    result = reconcile_backlinks(store)

    assert result.proposed == []
    assert result.skipped_existing == 2
    assert _pending_relation_proposals(store) == []


def test_symmetric_type_proposes_missing_mirror(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    _approved_relation(store, src="a", relation=RelationType.SIMILAR_TO.value, target="b")

    result = reconcile_backlinks(store)

    assert len(result.proposed) == 1
    pr = result.proposed[0]
    assert pr.payload["source"] == "b"
    assert pr.payload["relation"] == RelationType.SIMILAR_TO.value
    assert pr.payload["target"] == "a"


def test_symmetric_type_with_existing_mirror_is_not_duplicated(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    _approved_relation(store, src="a", relation=RelationType.CONTRADICTS.value, target="b")
    _approved_relation(store, src="b", relation=RelationType.CONTRADICTS.value, target="a")

    result = reconcile_backlinks(store)

    assert result.proposed == []
    assert result.skipped_existing == 2


def test_unmapped_relation_type_is_skipped(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    # `uses` has no default inverse — it must be skipped, not guessed.
    _approved_relation(store, src="a", relation=RelationType.USES.value, target="b")

    result = reconcile_backlinks(store)

    assert result.proposed == []
    assert result.checked == 0
    assert result.skipped_unmapped == 1


def test_dry_run_reports_without_writing(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    _approved_relation(store, src="a", relation=RelationType.DEPENDS_ON.value, target="b")

    result = reconcile_backlinks(store, dry_run=True)

    assert len(result.proposed) == 1
    assert result.dry_run is True
    # Reports the would-propose set, but nothing actually lands on disk.
    assert _pending_relation_proposals(store) == []


def test_limit_bounds_proposals_per_run(store: KBStore) -> None:
    _entity(store, "hub")
    for i in range(5):
        node = f"leaf{i}"
        _entity(store, node)
        _approved_relation(store, src="hub", relation=RelationType.DEPENDS_ON.value, target=node)

    result = reconcile_backlinks(store, limit=2)

    assert len(result.proposed) == 2


def test_rel_types_filters_scanned_edges(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    _entity(store, "c")
    _approved_relation(store, src="a", relation=RelationType.DEPENDS_ON.value, target="b")
    _approved_relation(store, src="a", relation=RelationType.SIMILAR_TO.value, target="c")

    result = reconcile_backlinks(store, rel_types=[RelationType.SIMILAR_TO.value])

    assert result.checked == 1
    assert len(result.proposed) == 1
    assert result.proposed[0].payload["relation"] == RelationType.SIMILAR_TO.value


def test_custom_inverse_map_from_config(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    store.config_path.write_text(
        yaml.safe_dump({"backlinks": {"inverse_map": {"uses": "uses"}}}),
        encoding="utf-8",
    )
    _approved_relation(store, src="a", relation=RelationType.USES.value, target="b")

    result = reconcile_backlinks(store)

    assert len(result.proposed) == 1
    assert result.proposed[0].payload["relation"] == RelationType.USES.value
    assert result.proposed[0].payload["target"] == "a"


def test_malformed_config_falls_back_to_default_map(store: KBStore) -> None:
    _entity(store, "a")
    _entity(store, "b")
    store.config_path.write_text("backlinks: not-a-mapping\n", encoding="utf-8")
    _approved_relation(store, src="a", relation=RelationType.DEPENDS_ON.value, target="b")

    result = reconcile_backlinks(store)

    assert len(result.proposed) == 1
    assert result.proposed[0].payload["relation"] == RelationType.BLOCKS.value
