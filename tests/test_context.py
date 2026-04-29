"""Context pack assembly — quality gate semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import context, health
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_context_pack_has_quality_metadata(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="JWT is used", evidence=[src.id]))
    health.rebuild_index(store)
    pack = context.build_context_pack(store, query="JWT", require_citations=True)
    assert pack.quality.items >= 1
    assert pack.quality.require_citations is True
    assert pack.quality.ok is True


def test_context_pack_max_chars_omits_items(store: KBStore) -> None:
    src = store.put_source(b"e")
    # Many short claims — total summary length > 100 chars.
    for i in range(20):
        store.put_claim(Claim(
            id=f"c{i}",
            text=f"lorem claim number {i} with extra padding text",
            evidence=[src.id],
        ))
    health.rebuild_index(store)
    pack = context.build_context_pack(store, query="lorem", max_chars=100,
                                      fail_on_budget_truncation=True)
    assert pack.quality.budget_truncated
    assert pack.quality.budget_omitted_items >= 1
    assert not pack.quality.ok


def test_context_pack_min_items_failure(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="orphan", evidence=[src.id]))
    health.rebuild_index(store)
    pack = context.build_context_pack(store, query="orphan", min_items=5)
    assert not pack.quality.ok
    assert "min_items" in pack.quality.failed
