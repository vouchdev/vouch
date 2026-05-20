"""End-to-end semantic search through KBStore + index_db."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import index_db
from vouch.embeddings import register
from vouch.models import Claim, Entity, EntityType, Page
from vouch.storage import KBStore
from tests.embeddings._fakes import MockEmbedder


@pytest.fixture(autouse=True)
def _use_mock_embedder() -> None:
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_put_claim_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="claim text", evidence=[src.id]))
    rec = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec is not None
    vec, ch, model = rec
    assert vec.shape == (8,)
    assert model == "mock"
