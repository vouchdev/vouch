"""sentence-transformers/all-mpnet-base-v2 adapter.

768-dim, English, good quality at its tier. Default model for vouch.
Lazy-loaded: the import of sentence_transformers is deferred to __init__.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from .base import DEFAULT_MODEL_NAME, Embedder, register


class STMpnetEmbedder(Embedder):
    name = DEFAULT_MODEL_NAME
    version = "v1"
    dim = 768

    def __init__(self, device: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        self._model: Any = SentenceTransformer(
            self.name, device=device or "cpu",
        )

    def encode(self, text: str) -> np.ndarray:
        vec = self._model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return np.asarray(vec, dtype=np.float32)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        mat = self._model.encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            batch_size=32,
        )
        return np.asarray(mat, dtype=np.float32)


register(DEFAULT_MODEL_NAME, STMpnetEmbedder)
