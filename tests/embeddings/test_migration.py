"""Model-identity mismatch detection + backfill."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.embeddings._fakes import MockEmbedder
from vouch import index_db
from vouch.embeddings import register
from vouch.embeddings.migration import (
    backfill_embeddings,
    detect_mismatch,
)
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _register_default() -> None:
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_detect_mismatch_returns_none_for_empty_kb(store: KBStore) -> None:
    assert detect_mismatch(store.kb_dir) is None


def test_detect_mismatch_reports_model_change(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    index_db.set_embedding_meta(
        store.kb_dir, model="some-other-model", version="v2", dim=8,
    )
    mismatch = detect_mismatch(store.kb_dir)
    assert mismatch is not None
    assert mismatch["stored_model"] == "some-other-model"
    assert mismatch["current_model"] == "mock"


def test_rebuild_index_emits_mismatch_audit_event(store: KBStore) -> None:
    from vouch import health, index_db
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    index_db.set_embedding_meta(
        store.kb_dir, model="some-other-model", version="v9", dim=8,
    )
    health.rebuild_index(store)
    log_path = store.kb_dir / "audit.log.jsonl"
    text = log_path.read_text() if log_path.exists() else ""
    assert "embedding.model_mismatch" in text


def test_backfill_re_encodes_all_artifacts(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="alpha", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="beta", evidence=[src.id]))
    with index_db.open_db(store.kb_dir) as conn:
        conn.execute("DELETE FROM embedding_index")
    n = backfill_embeddings(store)
    assert n >= 2
    assert index_db.get_embedding(store.kb_dir, kind="claim", id="c1") is not None
