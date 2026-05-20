"""Embedder abstract base class + adapter registry.

Adapters live in sibling modules (st_mpnet, st_minilm, fastembed_bge).
They register themselves at import time via `register(name, factory)`.

The factory pattern (lambda over class instantiation) lets us defer
heavy imports (torch, onnxruntime) until the adapter is actually used.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import ClassVar

import numpy as np

DEFAULT_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"


class Embedder(ABC):
    """Abstract embedding model adapter."""

    name: ClassVar[str] = ""
    version: ClassVar[str] = ""
    dim: ClassVar[int] = 0

    @abstractmethod
    def encode(self, text: str) -> np.ndarray:
        """Return a unit-normalized embedding for `text`, shape (dim,) float32."""

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        """Default batched encode -- subclasses override for true batching."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self.encode(t) for t in texts])


def content_hash(text: str) -> str:
    """Stable sha256 hex of the embedded text. Used as the skip-if-unchanged key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_REGISTRY: dict[str, Callable[[], Embedder]] = {}


def register(name: str, factory: Callable[[], Embedder]) -> None:
    """Register an Embedder factory under `name`. Idempotent."""
    _REGISTRY[name] = factory


def get_embedder(name: str | None = None) -> Embedder:
    """Resolve an Embedder by adapter name. None -> DEFAULT_MODEL_NAME."""
    key = name or DEFAULT_MODEL_NAME
    if key not in _REGISTRY:
        raise KeyError(f"unknown embedder: {key}. Registered: {sorted(_REGISTRY)}")
    return _REGISTRY[key]()
