"""Conftest for the vouch.embeddings test suite.

The embedding test modules import numpy at module-import time (via test
fixtures and the MockEmbedder test double). When CI runs the base
`pip install -e '.[dev]'` without the optional `[embeddings]` extras,
numpy isn't installed and collection of every test file in this
directory fails with ModuleNotFoundError.

`pytest.importorskip("numpy")` at conftest scope tells pytest to skip
the entire directory if numpy isn't importable, which lets the rest of
the suite run cleanly. Once the embeddings extras (or numpy explicitly)
are installed, the skip is a no-op and the tests run normally.
"""

from collections.abc import Iterator

import pytest

pytest.importorskip("numpy")


@pytest.fixture(autouse=True)
def _isolate_embedder_registry() -> Iterator[None]:
    """Snapshot and restore the global adapter registry around each test.

    Tests here register a MockEmbedder as the default adapter. `_REGISTRY`
    is module-global, so without this the registration leaks into later
    top-level tests (e.g. test_cli search-backend-label tests) and flips
    their expected backend from fts5/substring to embedding.
    """
    from vouch.embeddings import base
    saved = dict(base._REGISTRY)
    try:
        yield
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(saved)
