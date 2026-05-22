"""Ingest-time duplicate detection."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME, Embedder
from vouch.embeddings.dedup import list_duplicates
from vouch.models import Claim
from vouch.storage import KBStore


class _HashEmbedder(Embedder):
    """Deterministic embedder using text length + hash byte — avoids float32 overflow."""

    name = "mock"
    version = "1"
    dim = 8

    def encode(self, text: str) -> np.ndarray:
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        out = np.array([h[i] / 255.0 for i in range(self.dim)], dtype=np.float32)
        norm = float(np.linalg.norm(out))
        if norm > 0:
            out /= norm
        return out


@pytest.fixture(autouse=True)
def _register_default() -> None:
    register(DEFAULT_MODEL_NAME, _HashEmbedder)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_identical_text_flagged_as_duplicate(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="exact same text", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="exact same text", evidence=[src.id]))
    dupes = list_duplicates(store.kb_dir)
    pairs = {(d["id"], d["near_id"]) for d in dupes}
    assert ("c2", "c1") in pairs or ("c1", "c2") in pairs


def test_disjoint_text_not_flagged(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="apples", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="zebras", evidence=[src.id]))
    dupes = list_duplicates(store.kb_dir)
    assert dupes == []
