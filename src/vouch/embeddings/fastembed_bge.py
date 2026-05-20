"""fastembed BAAI/bge-small-en-v1.5 adapter (no-torch path).

384-dim, ONNX-backed. Install via `pip install vouch[embeddings-fast]`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from .base import Embedder, register

MODEL_NAME = "fastembed/bge-small-en-v1.5"


class FastembedBgeEmbedder(Embedder):
    name = MODEL_NAME
    version = "v1"
    dim = 384

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
        self._model: Any = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

    def encode(self, text: str) -> np.ndarray:
        vec = next(iter(self._model.embed([text])))
        arr = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr /= norm
        return arr

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        mat = np.stack([np.asarray(v, dtype=np.float32)
                        for v in self._model.embed(list(texts))])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (mat / norms).astype(np.float32)


register(MODEL_NAME, FastembedBgeEmbedder)
