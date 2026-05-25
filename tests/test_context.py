"""Context pack assembly — quality gate semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import context, health
from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _mock_embedder() -> None:
    # MockEmbedder requires numpy. Skip the dependent tests cleanly when
    # CI's base [dev] install doesn't include the optional [embeddings]
    # extras, rather than failing at module-import collection time.
    pytest.importorskip("numpy")
    from tests.embeddings._fakes import MockEmbedder
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_context_pack_has_quality_metadata(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="JWT is used", evidence=[src.id]))
    health.rebuild_index(store)
    pack = context.build_context_pack(store, query="JWT", require_citations=True)
    assert pack["quality"]["items"] >= 1
    assert pack["quality"]["require_citations"] is True
    assert pack["quality"]["ok"] is True


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
    assert pack["quality"]["budget_truncated"]
    assert pack["quality"]["budget_omitted_items"] >= 1
    assert not pack["quality"]["ok"]


def test_context_pack_min_items_failure(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="orphan", evidence=[src.id]))
    health.rebuild_index(store)
    pack = context.build_context_pack(store, query="orphan", min_items=5)
    assert not pack["quality"]["ok"]
    assert "min_items" in pack["quality"]["failed"]


def test_build_context_pack_uses_semantic_default(tmp_path: Path) -> None:
    from tests.embeddings._fakes import MockEmbedder
    from vouch.context import build_context_pack
    from vouch.embeddings import register
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    from vouch.models import Claim
    from vouch.storage import KBStore

    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="exact query string", evidence=[src.id]))
    pack = build_context_pack(store, query="exact query string", limit=5)
    assert any(item["id"] == "c1" for item in pack.get("items", []))


def test_build_context_pack_explain_flag_returns_score_breakdown(
    tmp_path: Path,
) -> None:
    from tests.embeddings._fakes import MockEmbedder
    from vouch.context import build_context_pack
    from vouch.embeddings import register
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    from vouch.models import Claim
    from vouch.storage import KBStore

    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="hello", evidence=[src.id]))
    pack = build_context_pack(store, query="hello", limit=5, explain=True)
    assert "explain" in pack
    assert any("backend" in row for row in pack["explain"])
