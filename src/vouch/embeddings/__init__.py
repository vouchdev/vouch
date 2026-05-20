"""Embedding-based semantic retrieval for vouch.

Pluggable model adapters via the `register` / `get_embedder` registry in
`base`. Default adapter is `st_mpnet` (sentence-transformers
all-mpnet-base-v2) when installed via `pip install vouch[embeddings]`.

The base install of vouch has no hard dependency on this package -- the
modules are only imported when an embedding code path executes.
"""

from .base import (
    DEFAULT_MODEL_NAME,
    Embedder,
    content_hash,
    get_embedder,
    register,
)

__all__ = [
    "DEFAULT_MODEL_NAME",
    "Embedder",
    "content_hash",
    "get_embedder",
    "register",
]
