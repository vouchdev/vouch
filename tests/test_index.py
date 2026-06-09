"""SQLite FTS5 index — rebuild, search, special characters."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from vouch import health, index_db
from vouch.models import Claim, Entity, EntityType, Page
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_index_rebuild_then_search(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="JWT tokens are stateless",
                          evidence=[src.id]))
    store.put_page(Page(id="p1", title="Auth", body="overview of auth"))
    store.put_entity(Entity(id="e1", name="JWT", type=EntityType.CONCEPT))
    health.rebuild_index(store)
    hits = index_db.search(store.kb_dir, "JWT")
    kinds = {k for k, *_ in hits}
    assert "claim" in kinds
    assert "entity" in kinds


def test_search_handles_special_chars(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="quote \"this\" and (parens)",
                          evidence=[src.id]))
    health.rebuild_index(store)
    # Should not crash on the FTS5-special characters.
    hits = index_db.search(store.kb_dir, 'quote "this"')
    assert any(k == "claim" for k, *_ in hits)


def test_index_via_approve(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="indexed automatically",
                       evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="u")
    hits = index_db.search(store.kb_dir, "indexed")
    assert any(k == "claim" for k, *_ in hits)


def test_rebuild_index_preserves_good_claims_when_one_yaml_is_corrupt(
    store: KBStore,
) -> None:
    """A bad YAML must not destroy the index for healthy claims (Option A fix)."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="good", text="healthy claim kept", evidence=[src.id]))
    store.put_claim(Claim(id="bad", text="will be corrupted", evidence=[src.id]))

    # Build a clean initial index so state.db exists.
    health.rebuild_index(store)
    assert index_db.stats(store.kb_dir)["claims"] == 2

    # Corrupt one YAML file to simulate a truncated write.
    (store.kb_dir / "claims" / "bad.yaml").write_text("{ broken: yaml: [")

    # rebuild_index must warn about the skipped file, not crash.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        health.rebuild_index(store)

    assert any("bad.yaml" in str(w.message) or "bad" in str(w.message) for w in caught), (
        "expected a warning naming the skipped artifact"
    )

    # The healthy claim must still be searchable.
    hits = index_db.search(store.kb_dir, "healthy")
    assert any(k == "claim" and row_id == "good" for k, row_id, *_ in hits), (
        "healthy claim disappeared from index after corrupt-sibling rebuild"
    )


def test_doctor_detects_blown_index(store: KBStore) -> None:
    """doctor() should warn when state.db is empty but artifact files exist."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="detectable claim", evidence=[src.id]))

    # Build the index so state.db exists, then wipe FTS rows to simulate the bug.
    health.rebuild_index(store)
    index_db.reset(store.kb_dir)

    report = health.doctor(store)
    codes = {f.code for f in report.findings}
    assert "index_blown" in codes
