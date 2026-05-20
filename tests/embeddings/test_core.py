"""Core embedder ABC + registry + content hashing."""

from __future__ import annotations

import numpy as np
import pytest

from tests.embeddings._fakes import MockEmbedder
from vouch.embeddings import (
    Embedder,
    content_hash,
    get_embedder,
    register,
)


def test_content_hash_is_stable() -> None:
    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_content_hash_differs_on_text_change() -> None:
    assert content_hash("a") != content_hash("b")


def test_embedder_abc_requires_encode() -> None:
    class Incomplete(Embedder):
        name = "incomplete"
        version = "0"
        dim = 1
    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_mock_embedder_returns_correct_shape() -> None:
    e = MockEmbedder(dim=8)
    vec = e.encode("hello")
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (8,)
    assert vec.dtype == np.float32


def test_mock_embedder_batched_encode() -> None:
    e = MockEmbedder(dim=8)
    mat = e.encode_batch(["a", "b", "c"])
    assert mat.shape == (3, 8)


def test_mock_embedder_is_deterministic() -> None:
    e = MockEmbedder(dim=16)
    assert np.array_equal(e.encode("same"), e.encode("same"))


def test_registry_round_trip() -> None:
    register("test-adapter", lambda: MockEmbedder(dim=4))
    e = get_embedder("test-adapter")
    assert e.dim == 4
    assert e.name == "mock"


def test_registry_unknown_name() -> None:
    with pytest.raises(KeyError, match="unknown embedder"):
        get_embedder("does-not-exist")
