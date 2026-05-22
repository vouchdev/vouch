"""Embedding-based semantic retrieval for vouch.

Pluggable model adapters via the `register` / `get_embedder` registry in
`base`. Default adapter is `st_mpnet` (sentence-transformers
all-mpnet-base-v2) when installed via `pip install vouch[embeddings]`.

The base install of vouch has no hard dependency on this package -- the
modules are only imported when an embedding code path executes.
"""

from contextlib import suppress

from .base import (
    DEFAULT_MODEL_NAME,
    Embedder,
    content_hash,
    get_embedder,
    register,
)

# Auto-register the default adapter if sentence-transformers is installed.
# Each adapter import is best-effort -- failure means that adapter's optional
# dependency isn't installed, which is fine (a different adapter may be).
with suppress(ImportError):
    from . import st_mpnet  # noqa: F401

with suppress(ImportError):
    from . import st_minilm  # noqa: F401

with suppress(ImportError):
    from . import fastembed_bge  # noqa: F401

__all__ = [
    "DEFAULT_MODEL_NAME",
    "Embedder",
    "content_hash",
    "get_embedder",
    "register",
]
