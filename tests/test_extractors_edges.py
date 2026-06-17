"""Auto-extracted typed edges on approved pages (issue #224)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.extractors.edges import AUTO_EXTRACTOR_ACTOR, extract_wikilinks
from vouch.models import (
    Entity,
    EntityType,
    ProposalKind,
    ProposalStatus,
    RelationType,
)
from vouch.proposals import approve, propose_page, reject_auto_extracted
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_extract_wikilinks_dedupes_and_preserves_order() -> None:
    body = "see [[alice]] and [[bob]], also [[alice]] again"
    assert extract_wikilinks(body) == ["alice", "bob"]


def test_approving_page_with_wikilinks_files_mentions_proposals(
    store: KBStore,
) -> None:
    store.put_entity(Entity(id="alice", name="Alice", type=EntityType.PERSON))
    store.put_entity(Entity(id="bob", name="Bob", type=EntityType.PERSON))
    store.put_entity(Entity(id="carol", name="Carol", type=EntityType.PERSON))
    pr = propose_page(
        store, title="Team",
        body="works with [[alice]], [[bob]], and [[carol]]",
        proposed_by="agent-a",
    )
    page = approve(store, pr.id, approved_by="reviewer")

    pending = store.list_proposals(ProposalStatus.PENDING)
    edges = [p for p in pending if p.kind == ProposalKind.RELATION]
    assert len(edges) == 3
    assert {e.payload["target"] for e in edges} == {"alice", "bob", "carol"}
    assert all(e.payload["source"] == page.id for e in edges)
    assert all(e.payload["relation"] == RelationType.MENTIONS.value for e in edges)
    assert all(e.proposed_by == AUTO_EXTRACTOR_ACTOR for e in edges)


def test_approving_page_with_entities_and_sources_files_typed_edges(
    store: KBStore,
) -> None:
    store.put_entity(Entity(id="acme", name="Acme", type=EntityType.COMPANY))
    src = store.put_source(b"doc")
    pr = propose_page(
        store, title="Acme overview", body="no links here",
        entity_ids=["acme"], source_ids=[src.id], proposed_by="agent-a",
    )
    approve(store, pr.id, approved_by="reviewer")

    pending = store.list_proposals(ProposalStatus.PENDING)
    edges = {p.payload["relation"]: p for p in pending if p.kind == ProposalKind.RELATION}
    assert edges[RelationType.RELATES_TO.value].payload["target"] == "acme"
    assert edges[RelationType.DERIVED_FROM.value].payload["target"] == src.id


def test_dangling_wikilink_is_skipped_not_an_error(store: KBStore) -> None:
    pr = propose_page(
        store, title="Orphan", body="mentions [[ghost-entity]] only",
        proposed_by="agent-a",
    )
    # Approval must not raise even though the wiki-link target doesn't exist.
    approve(store, pr.id, approved_by="reviewer")
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert [p for p in pending if p.kind == ProposalKind.RELATION] == []


def test_approving_page_without_links_files_no_edges(store: KBStore) -> None:
    pr = propose_page(store, title="Plain", body="no links", proposed_by="agent-a")
    approve(store, pr.id, approved_by="reviewer")
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert [p for p in pending if p.kind == ProposalKind.RELATION] == []


def test_reject_auto_extracted_bulk_rejects_only_extractor_proposals(
    store: KBStore,
) -> None:
    store.put_entity(Entity(id="alice", name="Alice", type=EntityType.PERSON))
    pr = propose_page(
        store, title="Team", body="works with [[alice]]", proposed_by="agent-a",
    )
    page = approve(store, pr.id, approved_by="reviewer")

    rejected = reject_auto_extracted(store, rejected_by="reviewer", page_id=page.id)
    assert len(rejected) == 1
    assert rejected[0].status == ProposalStatus.REJECTED

    pending = store.list_proposals(ProposalStatus.PENDING)
    assert [p for p in pending if p.kind == ProposalKind.RELATION] == []
