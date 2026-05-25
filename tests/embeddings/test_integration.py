"""End-to-end tests that load the REAL embedding model.

Marked @pytest.mark.integration -- excluded from the default test run.
Run with: pytest -m integration
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


@pytest.mark.integration
def test_st_mpnet_loads_and_encodes() -> None:
    from vouch.embeddings.st_mpnet import STMpnetEmbedder
    e = STMpnetEmbedder()
    vec = e.encode("hello world")
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (768,)
    assert vec.dtype == np.float32
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-3


@pytest.mark.integration
def test_st_mpnet_semantic_disjoint() -> None:
    """Semantic similarity > lexical-only baseline."""
    from vouch.embeddings.st_mpnet import STMpnetEmbedder
    e = STMpnetEmbedder()
    q = e.encode("how do users authenticate")
    a = e.encode("login flow uses session cookies signed by the API")
    b = e.encode("the sun is large")
    sim_a = float(q @ a)
    sim_b = float(q @ b)
    assert sim_a > sim_b


@pytest.mark.integration
def test_st_minilm_loads_and_encodes() -> None:
    from vouch.embeddings.st_minilm import STMinilmEmbedder
    e = STMinilmEmbedder()
    vec = e.encode("hello world")
    assert vec.shape == (384,)
    assert vec.dtype == np.float32


@pytest.mark.integration
def test_semantic_search_finds_lexically_disjoint_claim(tmp_path: Path) -> None:
    """The headline acceptance criterion from the spec."""
    from vouch import index_db
    from vouch.models import Claim
    from vouch.storage import KBStore

    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="auth-claim",
        text="login flow uses session cookies signed by the API",
        evidence=[src.id],
    ))
    store.put_claim(Claim(
        id="unrelated",
        text="the sun is large and hot",
        evidence=[src.id],
    ))
    hits = index_db.search_semantic(
        store.kb_dir, "how do we authenticate users", limit=5,
    )
    assert hits[0][1] == "auth-claim"


@pytest.mark.integration
def test_fastembed_bge_loads_and_encodes() -> None:
    pytest.importorskip("fastembed")
    from vouch.embeddings.fastembed_bge import FastembedBgeEmbedder
    e = FastembedBgeEmbedder()
    vec = e.encode("hello world")
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
