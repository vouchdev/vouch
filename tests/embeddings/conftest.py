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

import pytest

pytest.importorskip("numpy")
