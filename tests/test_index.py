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


def test_rebuild_index_keeps_good_claims_when_one_yaml_is_corrupt(
    store: KBStore,
) -> None:
    """One bad YAML file must not wipe the index for the healthy claims (#159)."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="good", text="healthy claim kept", evidence=[src.id]))
    store.put_claim(Claim(id="bad", text="about to be corrupted", evidence=[src.id]))

    health.rebuild_index(store)
    assert index_db.stats(store.kb_dir)["claims"] == 2

    # Simulate a truncated/half-written file (the repro from the issue).
    (store.kb_dir / "claims" / "bad.yaml").write_text("{ broken: yaml: [")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        health.rebuild_index(store)

    assert any("bad.yaml" in str(w.message) for w in caught), (
        "expected a warning naming the skipped artifact"
    )
    # The healthy claim is still searchable; only the corrupt one dropped out.
    hits = index_db.search(store.kb_dir, "healthy")
    assert any(k == "claim" and rid == "good" for k, rid, *_ in hits)
    assert index_db.stats(store.kb_dir)["claims"] == 1


def test_rebuild_index_is_atomic_on_hard_failure(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the rebuild blows up, the previous index survives untouched."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="survives a failed rebuild", evidence=[src.id]))
    health.rebuild_index(store)
    assert index_db.stats(store.kb_dir)["claims"] == 1

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(index_db, "index_claim", boom)
    with pytest.raises(RuntimeError):
        health.rebuild_index(store)

    # Live index untouched and no temp files left lying around.
    assert index_db.stats(store.kb_dir)["claims"] == 1
    assert not list(store.kb_dir.glob("*.db.tmp"))


def test_doctor_detects_blown_index(store: KBStore) -> None:
    """doctor() warns when state.db is empty but artifacts exist on disk."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="detectable claim", evidence=[src.id]))

    health.rebuild_index(store)
    # reset() empties the index in place, reproducing the blown-index state
    # without deleting the file (mirrors a crashed pre-fix rebuild).
    index_db.reset(store.kb_dir)

    report = health.doctor(store)
    assert "index_blown" in {f.code for f in report.findings}
