"""sentence-transformers/all-MiniLM-L6-v2 adapter.

384-dim, smaller and faster than mpnet-base, slightly lower quality.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from .base import Embedder, register

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


class STMinilmEmbedder(Embedder):
    name = MODEL_NAME
    version = "v1"
    dim = 384

    def __init__(self, device: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        self._model: Any = SentenceTransformer(self.name, device=device or "cpu")

    def encode(self, text: str) -> np.ndarray:
        vec = self._model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        mat = self._model.encode(
            list(texts), convert_to_numpy=True,
            normalize_embeddings=True, batch_size=64,
        )
        return np.asarray(mat, dtype=np.float32)


register(MODEL_NAME, STMinilmEmbedder)
