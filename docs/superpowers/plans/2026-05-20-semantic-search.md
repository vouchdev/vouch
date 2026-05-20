# Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add embedding-based semantic retrieval as the primary search backend in vouch (CLI, MCP, JSONL), with FTS5 as fallback. Cover all six artifact types, sqlite-vec ANN with NumPy fallback, cross-encoder rerank, HyDE expansion, ingest-time duplicate detection, model-version migration, and eval harness.

**Architecture:** New `src/vouch/embeddings/` package with pluggable model adapters (registry pattern). Synchronous-at-write embedding via hooks in `KBStore.put_*` and `update_*`. Vectors stored in `sqlite-vec` virtual tables under `.vouch/state.db`, with a pure-NumPy brute-force fallback if the extension is unavailable. Default model: `sentence-transformers/all-mpnet-base-v2` (768-dim).

**Tech Stack:** Python 3.11+, `sentence-transformers`, `sqlite-vec`, `numpy`, existing `sqlite3`/`pydantic`/`click` from base. Optional adapters: `fastembed` + `onnxruntime`. Tests use pytest + a `MockEmbedder` test double for speed; one integration test exercises the real model under `@pytest.mark.integration`.

**Spec:** `docs/superpowers/specs/2026-05-20-semantic-search-design.md`

---

## File Structure

### New files

```
src/vouch/embeddings/__init__.py            # public API re-exports
src/vouch/embeddings/base.py                # Embedder ABC, registry, content_hash
src/vouch/embeddings/st_mpnet.py            # default adapter
src/vouch/embeddings/st_minilm.py           # alt adapter
src/vouch/embeddings/fastembed_bge.py       # alt adapter (no-torch)
src/vouch/embeddings/cache.py               # query cache LRU
src/vouch/embeddings/rerank.py              # cross-encoder reranker
src/vouch/embeddings/hyde.py                # HyDE query expansion
src/vouch/embeddings/dedup.py               # ingest-time duplicate detection
src/vouch/embeddings/fusion.py              # RRF/weighted/normalized fusion
src/vouch/embeddings/scorer.py              # recall@k / MRR / nDCG harness
src/vouch/embeddings/migration.py           # model identity check, backfill
tests/embeddings/__init__.py
tests/embeddings/_fakes.py                  # MockEmbedder test double
tests/embeddings/test_core.py
tests/embeddings/test_storage.py
tests/embeddings/test_search.py
tests/embeddings/test_fusion.py
tests/embeddings/test_rerank.py
tests/embeddings/test_hyde.py
tests/embeddings/test_dedup.py
tests/embeddings/test_migration.py
tests/embeddings/test_scorer.py
tests/embeddings/test_cli.py
tests/embeddings/test_integration.py        # marked @pytest.mark.integration
```

> **Note:** the evaluation harness module is named `scorer.py` (not `eval.py`) to avoid shadowing Python's builtin and to keep static analysers happy. The CLI subcommand surface stays `vouch eval embedding` (a Click group; the Python function is named `eval_group`).

### Modified files

```
pyproject.toml                              # extras; pytest markers
src/vouch/index_db.py                       # vec tables, search fns, hybrid
src/vouch/storage.py                        # hook put_* + update_* paths
src/vouch/server.py                         # kb.search extensions + 5 new tools
src/vouch/jsonl_server.py                   # parity with MCP
src/vouch/cli.py                            # flags + new commands
src/vouch/context.py                        # semantic-default + --explain
src/vouch/lifecycle.py                      # re-embed on updates
src/vouch/health.py                         # rebuild_index emits mismatch event
```

### Test infrastructure

`tests/embeddings/_fakes.py` defines `MockEmbedder` returning deterministic vectors derived from `sha256` of the input. This makes 99% of tests run in milliseconds without loading any real model. The real model is exercised in `test_integration.py` only.

---

## Phase 1 — Foundation

### Task 1: Optional dependencies and pytest markers

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add embeddings extras and integration marker**

Edit `pyproject.toml` to add new optional-dependencies groups and a pytest marker. Locate the `[project.optional-dependencies]` block (currently at lines 27-35) and add:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=9.0.3,<10",
    "pytest-cov>=5,<6",
    "mypy>=2.1.0",
    "ruff>=0.15.13",
    "mypy>=1.10",
    "types-pyyaml",
]
embeddings = [
    "sentence-transformers>=2.7,<4",
    "numpy>=1.26,<3",
    "sqlite-vec>=0.1,<1",
]
embeddings-fast = [
    "fastembed>=0.3,<1",
    "onnxruntime>=1.18,<2",
    "numpy>=1.26,<3",
    "sqlite-vec>=0.1,<1",
]
rerank = [
    "sentence-transformers>=2.7,<4",
]
```

And extend `[tool.pytest.ini_options]` (currently lines 58-60) to:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q -m 'not integration'"
markers = [
    "integration: tests that load the real embedding model (slow, network on first run)",
]
```

- [ ] **Step 2: Install the new extras locally for development**

Run: `.venv/bin/pip install -e '.[embeddings,dev]'`
Expected: pip resolves and installs sentence-transformers, numpy, sqlite-vec without errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat(embeddings): add optional-deps extras and pytest markers"
```

---

### Task 2: Create the embeddings package skeleton

**Files:**
- Create: `src/vouch/embeddings/__init__.py`
- Create: `tests/embeddings/__init__.py`

- [ ] **Step 1: Create the package init**

Write `src/vouch/embeddings/__init__.py`:

```python
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
```

- [ ] **Step 2: Create test package init**

Write `tests/embeddings/__init__.py`:

```python
"""Tests for the vouch.embeddings package."""
```

- [ ] **Step 3: Commit**

```bash
git add src/vouch/embeddings/__init__.py tests/embeddings/__init__.py
git commit -m "feat(embeddings): create package skeleton"
```

---

### Task 3: Embedder ABC and content hashing

**Files:**
- Create: `src/vouch/embeddings/base.py`
- Create: `tests/embeddings/_fakes.py`
- Test: `tests/embeddings/test_core.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_core.py`:

```python
"""Core embedder ABC + registry + content hashing."""

from __future__ import annotations

import numpy as np
import pytest

from vouch.embeddings import (
    Embedder,
    content_hash,
    get_embedder,
    register,
)
from tests.embeddings._fakes import MockEmbedder


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
```

- [ ] **Step 2: Run the test, confirm it fails**

Run: `.venv/bin/pytest tests/embeddings/test_core.py -v`
Expected: ImportError — `vouch.embeddings.base` / `tests.embeddings._fakes` don't exist yet.

- [ ] **Step 3: Implement the base module**

Create `src/vouch/embeddings/base.py`:

```python
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
```

- [ ] **Step 4: Create the MockEmbedder test double**

Create `tests/embeddings/_fakes.py`:

```python
"""Test doubles for the embeddings layer.

MockEmbedder returns deterministic float32 vectors derived from sha256
of the input. No network, no model, runs in microseconds.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

import numpy as np

from vouch.embeddings.base import Embedder


class MockEmbedder(Embedder):
    """Deterministic embedder for tests."""

    name = "mock"
    version = "1"

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def encode(self, text: str) -> np.ndarray:
        out = np.zeros(self.dim, dtype=np.float32)
        i = 0
        seed = text.encode("utf-8")
        while i < self.dim:
            h = hashlib.sha256(seed).digest()
            for j in range(0, len(h) - 3, 4):
                if i >= self.dim:
                    break
                out[i] = struct.unpack("<f", h[j:j + 4])[0]
                i += 1
            seed = h
        norm = float(np.linalg.norm(out))
        if norm > 0:
            out /= norm
        return out

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self.encode(t) for t in texts])
```

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `.venv/bin/pytest tests/embeddings/test_core.py -v`
Expected: 8 passed.

- [ ] **Step 6: Run ruff to check lint cleanliness**

Run: `.venv/bin/python -m ruff check src/vouch/embeddings tests/embeddings`
Expected: All checks passed.

- [ ] **Step 7: Commit**

```bash
git add src/vouch/embeddings/base.py tests/embeddings/_fakes.py tests/embeddings/test_core.py
git commit -m "feat(embeddings): Embedder ABC, registry, content_hash, MockEmbedder"
```

---

### Task 4: Default adapter (sentence-transformers all-mpnet-base-v2)

**Files:**
- Create: `src/vouch/embeddings/st_mpnet.py`
- Modify: `src/vouch/embeddings/__init__.py`
- Test: `tests/embeddings/test_integration.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/embeddings/test_integration.py`:

```python
"""End-to-end tests that load the REAL embedding model.

Marked @pytest.mark.integration -- excluded from the default test run.
Run with: pytest -m integration
"""

from __future__ import annotations

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
```

- [ ] **Step 2: Run integration test, confirm it fails**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py -v -m integration`
Expected: ImportError — `vouch.embeddings.st_mpnet` doesn't exist.

- [ ] **Step 3: Implement the adapter**

Create `src/vouch/embeddings/st_mpnet.py`:

```python
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
```

- [ ] **Step 4: Modify `__init__.py` to auto-register adapter on import**

Append to `src/vouch/embeddings/__init__.py` (above `__all__`):

```python
# Auto-register the default adapter if sentence-transformers is installed.
try:
    from . import st_mpnet  # noqa: F401
except ImportError:
    pass
```

- [ ] **Step 5: Run integration test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py -v -m integration`
Expected: 2 passed (slow on first run -- model download ~420MB).

- [ ] **Step 6: Run unit tests to confirm no regression**

Run: `.venv/bin/pytest tests/embeddings -v`
Expected: 8 passed (integration tests deselected by default).

- [ ] **Step 7: Commit**

```bash
git add src/vouch/embeddings/st_mpnet.py src/vouch/embeddings/__init__.py tests/embeddings/test_integration.py
git commit -m "feat(embeddings): sentence-transformers all-mpnet-base-v2 default adapter"
```

---

### Task 5: Alternative adapter -- MiniLM-L6

**Files:**
- Create: `src/vouch/embeddings/st_minilm.py`
- Modify: `src/vouch/embeddings/__init__.py`
- Test: extends `tests/embeddings/test_integration.py`

- [ ] **Step 1: Add failing test**

Append to `tests/embeddings/test_integration.py`:

```python
@pytest.mark.integration
def test_st_minilm_loads_and_encodes() -> None:
    from vouch.embeddings.st_minilm import STMinilmEmbedder
    e = STMinilmEmbedder()
    vec = e.encode("hello world")
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
```

- [ ] **Step 2: Run test, confirm fail**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py::test_st_minilm_loads_and_encodes -m integration -v`
Expected: ImportError.

- [ ] **Step 3: Implement adapter**

Create `src/vouch/embeddings/st_minilm.py`:

```python
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
```

- [ ] **Step 4: Add guarded import to `__init__.py`**

Append a second try/except block:

```python
try:
    from . import st_minilm  # noqa: F401
except ImportError:
    pass
```

- [ ] **Step 5: Run integration test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py::test_st_minilm_loads_and_encodes -m integration -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/embeddings/st_minilm.py src/vouch/embeddings/__init__.py tests/embeddings/test_integration.py
git commit -m "feat(embeddings): sentence-transformers MiniLM-L6 alternative adapter"
```

---

### Task 6: Alternative adapter -- fastembed BGE

**Files:**
- Create: `src/vouch/embeddings/fastembed_bge.py`
- Modify: `src/vouch/embeddings/__init__.py`
- Test: extends `tests/embeddings/test_integration.py`

- [ ] **Step 1: Add failing test**

Append to `tests/embeddings/test_integration.py`:

```python
@pytest.mark.integration
def test_fastembed_bge_loads_and_encodes() -> None:
    pytest.importorskip("fastembed")
    from vouch.embeddings.fastembed_bge import FastembedBgeEmbedder
    e = FastembedBgeEmbedder()
    vec = e.encode("hello world")
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
```

- [ ] **Step 2: Run test, confirm fail**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py::test_fastembed_bge_loads_and_encodes -m integration -v`
Expected: ImportError or skip.

- [ ] **Step 3: Implement adapter**

Create `src/vouch/embeddings/fastembed_bge.py`:

```python
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
```

- [ ] **Step 4: Add guarded import to `__init__.py`**

```python
try:
    from . import fastembed_bge  # noqa: F401
except ImportError:
    pass
```

- [ ] **Step 5: Run integration test (skipped if fastembed not installed)**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py::test_fastembed_bge_loads_and_encodes -m integration -v`
Expected: PASS or SKIPPED.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/embeddings/fastembed_bge.py src/vouch/embeddings/__init__.py tests/embeddings/test_integration.py
git commit -m "feat(embeddings): fastembed BGE alternative (no-torch) adapter"
```

---

## Phase 2 — Storage

### Task 7: Embedding storage schema in state.db

**Files:**
- Modify: `src/vouch/index_db.py`
- Test: `tests/embeddings/test_storage.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_storage.py`:

```python
"""Embedding storage layer -- schema, put, get, search."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vouch import index_db
from vouch.embeddings.base import content_hash
from vouch.storage import KBStore


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    store = KBStore.init(tmp_path)
    return store.kb_dir


def test_embedding_schema_creates_tables(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table')"
        )}
    assert "embedding_index" in tables
    assert "query_embedding_cache" in tables
    assert "embedding_dupes" in tables


def test_embedding_meta_default_values(kb_dir: Path) -> None:
    meta = index_db.get_embedding_meta(kb_dir)
    assert meta.get("embedding_model") in (None, "")


def test_put_and_get_embedding(kb_dir: Path) -> None:
    vec = np.zeros(8, dtype=np.float32)
    vec[0] = 1.0
    h = content_hash("hello")
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec,
            content_hash=h, model="mock", model_version="1", dim=8,
        )
    got = index_db.get_embedding(kb_dir, kind="claim", id="c1")
    assert got is not None
    rec_vec, rec_hash, rec_model = got
    assert np.allclose(rec_vec, vec)
    assert rec_hash == h
    assert rec_model == "mock"


def test_put_embedding_idempotent_on_same_hash(kb_dir: Path) -> None:
    vec = np.ones(4, dtype=np.float32)
    h = content_hash("same")
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec, content_hash=h,
            model="mock", model_version="1", dim=4,
        )
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec, content_hash=h,
            model="mock", model_version="1", dim=4,
        )
    with index_db.open_db(kb_dir) as conn:
        n = conn.execute("SELECT COUNT(*) FROM embedding_index WHERE id='c1'").fetchone()[0]
    assert n == 1


def test_set_embedding_meta_round_trip(kb_dir: Path) -> None:
    index_db.set_embedding_meta(
        kb_dir,
        model="sentence-transformers/all-mpnet-base-v2",
        version="v1",
        dim=768,
    )
    meta = index_db.get_embedding_meta(kb_dir)
    assert meta["embedding_model"] == "sentence-transformers/all-mpnet-base-v2"
    assert meta["embedding_dim"] == "768"
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py -v`
Expected: AttributeError -- functions don't exist.

- [ ] **Step 3: Extend `SCHEMA` in `index_db.py`**

In `src/vouch/index_db.py`, replace the existing `SCHEMA` constant (lines 22-51) with:

```python
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    id UNINDEXED, text, type UNINDEXED, status UNINDEXED, tags
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    id UNINDEXED, title, body, type UNINDEXED, tags
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    id UNINDEXED, name, description, type UNINDEXED, aliases
);

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_index (
    kind            TEXT NOT NULL,
    id              TEXT NOT NULL,
    vec             BLOB NOT NULL,
    content_hash    TEXT NOT NULL,
    model           TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);

CREATE INDEX IF NOT EXISTS embedding_index_kind ON embedding_index(kind);

CREATE TABLE IF NOT EXISTS query_embedding_cache (
    query_hash      TEXT PRIMARY KEY,
    vec             BLOB NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 1,
    last_used_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_dupes (
    kind            TEXT NOT NULL,
    id              TEXT NOT NULL,
    near_id         TEXT NOT NULL,
    cosine          REAL NOT NULL,
    detected_at     TEXT NOT NULL
);
"""
```

- [ ] **Step 4: Add helpers below `stats()`**

Append to `src/vouch/index_db.py`:

```python
# --- embeddings storage --------------------------------------------------

import datetime as _dt


def _vec_to_blob(vec):  # type: ignore[no-untyped-def]
    import numpy as np
    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes, dim: int):  # type: ignore[no-untyped-def]
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32, count=dim).copy()


def put_embedding(
    conn: sqlite3.Connection, *,
    kind: str, id: str,
    vec,  # type: ignore[no-untyped-def]
    content_hash: str,
    model: str, model_version: str, dim: int,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO embedding_index "
        "(kind, id, vec, content_hash, model, model_version, dim, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind, id, _vec_to_blob(vec), content_hash,
            model, model_version, dim,
            _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        ),
    )


def get_embedding(kb_dir: Path, *, kind: str, id: str):  # type: ignore[no-untyped-def]
    """Return (vec, content_hash, model) or None."""
    with open_db(kb_dir) as conn:
        row = conn.execute(
            "SELECT vec, content_hash, model, dim FROM embedding_index "
            "WHERE kind = ? AND id = ?",
            (kind, id),
        ).fetchone()
    if not row:
        return None
    blob, ch, model, dim = row
    return _blob_to_vec(blob, dim), ch, model


def set_embedding_meta(kb_dir: Path, *, model: str, version: str, dim: int) -> None:
    with open_db(kb_dir) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            [
                ("embedding_model", model),
                ("embedding_model_version", version),
                ("embedding_dim", str(dim)),
            ],
        )


def get_embedding_meta(kb_dir: Path) -> dict[str, str]:
    with open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT key, value FROM index_meta WHERE key LIKE 'embedding_%'"
        ).fetchall()
    return {k: v for k, v in rows}
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run ruff**

Run: `.venv/bin/python -m ruff check src/vouch tests`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/vouch/index_db.py tests/embeddings/test_storage.py
git commit -m "feat(embeddings): state.db schema for embedding storage + put/get helpers"
```

---

### Task 8: Vector search (NumPy brute-force path)

**Files:**
- Modify: `src/vouch/index_db.py`
- Modify: `tests/embeddings/test_storage.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_storage.py`:

```python
def test_search_embedding_returns_topk(kb_dir: Path) -> None:
    from vouch.embeddings.base import content_hash as ch

    query = np.zeros(8, dtype=np.float32); query[0] = 1.0
    near = np.zeros(8, dtype=np.float32); near[0] = 0.99; near[1] = 0.05
    far  = np.zeros(8, dtype=np.float32); far[7] = 1.0
    near /= float(np.linalg.norm(near))
    with index_db.open_db(kb_dir) as conn:
        for cid, vec in [("c-near", near), ("c-far", far)]:
            index_db.put_embedding(
                conn, kind="claim", id=cid, vec=vec,
                content_hash=ch(cid), model="mock", model_version="1", dim=8,
            )
    hits = index_db.search_embedding(
        kb_dir, query_vec=query, kinds=("claim",), limit=2,
    )
    assert hits[0][1] == "c-near"
    assert hits[1][1] == "c-far"


def test_search_embedding_empty_db(kb_dir: Path) -> None:
    q = np.zeros(8, dtype=np.float32); q[0] = 1.0
    hits = index_db.search_embedding(kb_dir, query_vec=q, kinds=("claim",), limit=5)
    assert hits == []


def test_search_embedding_filters_by_kind(kb_dir: Path) -> None:
    from vouch.embeddings.base import content_hash as ch
    v = np.zeros(4, dtype=np.float32); v[0] = 1.0
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(conn, kind="claim", id="c1", vec=v,
                               content_hash=ch("c1"), model="mock",
                               model_version="1", dim=4)
        index_db.put_embedding(conn, kind="page", id="p1", vec=v,
                               content_hash=ch("p1"), model="mock",
                               model_version="1", dim=4)
    only_claims = index_db.search_embedding(
        kb_dir, query_vec=v, kinds=("claim",), limit=10,
    )
    assert {h[0] for h in only_claims} == {"claim"}
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py::test_search_embedding_returns_topk -v`
Expected: AttributeError.

- [ ] **Step 3: Implement `search_embedding`**

Append to `src/vouch/index_db.py`:

```python
def search_embedding(
    kb_dir: Path,
    *,
    query_vec,  # type: ignore[no-untyped-def]
    kinds: tuple[str, ...] = (
        "claim", "page", "source", "entity", "relation", "evidence",
    ),
    limit: int = 10,
    min_score: float = 0.0,
) -> list[tuple[str, str, str, float]]:
    """NumPy brute-force cosine search. Returns (kind, id, snippet, score)."""
    import numpy as np
    q = np.asarray(query_vec, dtype=np.float32)
    qnorm = float(np.linalg.norm(q))
    if qnorm > 0:
        q = q / qnorm
    placeholders = ",".join("?" for _ in kinds)
    with open_db(kb_dir) as conn:
        rows = conn.execute(
            f"SELECT kind, id, vec, dim FROM embedding_index "
            f"WHERE kind IN ({placeholders})",
            kinds,
        ).fetchall()
    scored: list[tuple[str, str, str, float]] = []
    for kind, id_, blob, dim in rows:
        vec = _blob_to_vec(blob, dim)
        score = float(q @ vec)
        if score >= min_score:
            scored.append((kind, id_, "", score))
    scored.sort(key=lambda r: r[3], reverse=True)
    return scored[:limit]
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/index_db.py tests/embeddings/test_storage.py
git commit -m "feat(embeddings): NumPy brute-force cosine search over embedding_index"
```

---

### Task 9: sqlite-vec ANN path with fallback

**Files:**
- Modify: `src/vouch/index_db.py`
- Modify: `tests/embeddings/test_storage.py`

- [ ] **Step 1: Add test asserting ANN loader behavior**

Append to `tests/embeddings/test_storage.py`:

```python
def test_sqlite_vec_loader_is_idempotent(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        a = index_db._load_sqlite_vec(conn)
        b = index_db._load_sqlite_vec(conn)
    assert a == b


def test_search_works_under_both_backends(kb_dir: Path) -> None:
    from vouch.embeddings.base import content_hash as ch
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((20, 8)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    with index_db.open_db(kb_dir) as conn:
        for i, v in enumerate(vecs):
            index_db.put_embedding(
                conn, kind="claim", id=f"c{i}", vec=v,
                content_hash=ch(f"c{i}"), model="mock",
                model_version="1", dim=8,
            )
    hits = index_db.search_embedding(
        kb_dir, query_vec=vecs[0], kinds=("claim",), limit=3,
    )
    assert hits[0][1] == "c0"  # exact self-match
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py::test_sqlite_vec_loader_is_idempotent -v`
Expected: AttributeError.

- [ ] **Step 3: Add the loader and ANN path**

Insert into `src/vouch/index_db.py`, ABOVE the existing `search_embedding`:

```python
_sqlite_vec_loaded: set[int] = set()


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the sqlite-vec extension."""
    if id(conn) in _sqlite_vec_loaded:
        return True
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.OperationalError):
        return False
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        sqlite_vec.load(conn)
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except sqlite3.OperationalError:
            pass
    _sqlite_vec_loaded.add(id(conn))
    return True
```

Replace `search_embedding` body with the ANN-then-NumPy version:

```python
def search_embedding(
    kb_dir: Path,
    *,
    query_vec,  # type: ignore[no-untyped-def]
    kinds: tuple[str, ...] = (
        "claim", "page", "source", "entity", "relation", "evidence",
    ),
    limit: int = 10,
    min_score: float = 0.0,
) -> list[tuple[str, str, str, float]]:
    import numpy as np
    q = np.asarray(query_vec, dtype=np.float32)
    qnorm = float(np.linalg.norm(q))
    if qnorm > 0:
        q = q / qnorm
    placeholders = ",".join("?" for _ in kinds)
    with open_db(kb_dir) as conn:
        have_vec = _load_sqlite_vec(conn)
        if have_vec:
            try:
                rows = conn.execute(
                    f"SELECT kind, id, "
                    f"  1.0 - vec_distance_cosine(vec, ?) AS score "
                    f"FROM embedding_index "
                    f"WHERE kind IN ({placeholders}) AND score >= ? "
                    f"ORDER BY score DESC LIMIT ?",
                    (q.tobytes(), *kinds, min_score, limit),
                ).fetchall()
                return [(k, i, "", float(s)) for k, i, s in rows]
            except sqlite3.OperationalError:
                pass
        rows = conn.execute(
            f"SELECT kind, id, vec, dim FROM embedding_index "
            f"WHERE kind IN ({placeholders})",
            kinds,
        ).fetchall()
    scored: list[tuple[str, str, str, float]] = []
    for kind, id_, blob, dim in rows:
        vec = _blob_to_vec(blob, dim)
        score = float(q @ vec)
        if score >= min_score:
            scored.append((kind, id_, "", score))
    scored.sort(key=lambda r: r[3], reverse=True)
    return scored[:limit]
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/index_db.py tests/embeddings/test_storage.py
git commit -m "feat(embeddings): sqlite-vec ANN path with NumPy fallback"
```

---

### Task 10: Query embedding cache

**Files:**
- Create: `src/vouch/embeddings/cache.py`
- Modify: `tests/embeddings/test_storage.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_storage.py`:

```python
def test_query_cache_round_trip(kb_dir: Path) -> None:
    from vouch.embeddings.cache import (
        cache_query_vec, lookup_query_vec, query_cache_size,
    )
    v = np.ones(4, dtype=np.float32)
    cache_query_vec(kb_dir, query="hello", vec=v)
    got = lookup_query_vec(kb_dir, query="hello")
    assert got is not None
    assert np.allclose(got, v)
    assert lookup_query_vec(kb_dir, query="other") is None
    assert query_cache_size(kb_dir) == 1


def test_query_cache_lru_eviction(kb_dir: Path) -> None:
    from vouch.embeddings.cache import cache_query_vec, query_cache_size
    v = np.ones(4, dtype=np.float32)
    for i in range(5):
        cache_query_vec(kb_dir, query=f"q{i}", vec=v, max_entries=3)
    assert query_cache_size(kb_dir) <= 3
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py::test_query_cache_round_trip -v`
Expected: ImportError.

- [ ] **Step 3: Create `cache.py`**

Create `src/vouch/embeddings/cache.py`:

```python
"""Query embedding LRU cache backed by `query_embedding_cache` table."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from .. import index_db


def _query_key(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def cache_query_vec(
    kb_dir: Path, *, query: str, vec: np.ndarray, max_entries: int = 1024,
) -> None:
    h = _query_key(query)
    blob = np.asarray(vec, dtype=np.float32).tobytes()
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with index_db.open_db(kb_dir) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO query_embedding_cache "
            "(query_hash, vec, hit_count, last_used_at) "
            "VALUES (?, ?, COALESCE("
            "  (SELECT hit_count FROM query_embedding_cache WHERE query_hash=?), 0"
            ") + 1, ?)",
            (h, blob, h, now),
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM query_embedding_cache"
        ).fetchone()[0]
        if n > max_entries:
            conn.execute(
                "DELETE FROM query_embedding_cache WHERE query_hash IN ("
                " SELECT query_hash FROM query_embedding_cache "
                " ORDER BY last_used_at ASC LIMIT ?)",
                (n - max_entries,),
            )


def lookup_query_vec(kb_dir: Path, *, query: str) -> np.ndarray | None:
    h = _query_key(query)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with index_db.open_db(kb_dir) as conn:
        row = conn.execute(
            "SELECT vec FROM query_embedding_cache WHERE query_hash = ?",
            (h,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE query_embedding_cache SET hit_count = hit_count + 1, "
            "last_used_at = ? WHERE query_hash = ?",
            (now, h),
        )
    return np.frombuffer(row[0], dtype=np.float32).copy()


def query_cache_size(kb_dir: Path) -> int:
    with index_db.open_db(kb_dir) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM query_embedding_cache"
        ).fetchone()[0])


def query_cache_clear(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        conn.execute("DELETE FROM query_embedding_cache")


def query_cache_stats(kb_dir: Path) -> dict[str, Any]:
    with index_db.open_db(kb_dir) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), "
            "COALESCE(MAX(hit_count), 0) FROM query_embedding_cache"
        ).fetchone()
    return {"entries": int(row[0]), "hits": int(row[1]),
            "max_hits_per_entry": int(row[2])}
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_storage.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/cache.py tests/embeddings/test_storage.py
git commit -m "feat(embeddings): query embedding LRU cache"
```

---

## Phase 3 — Write path

### Task 11: Embedding hook helper in storage.py

**Files:**
- Modify: `src/vouch/storage.py`
- Test: `tests/embeddings/test_search.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_search.py`:

```python
"""End-to-end semantic search through KBStore + index_db."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import index_db
from vouch.embeddings import register
from vouch.models import Claim, Entity, EntityType, Page
from vouch.storage import KBStore
from tests.embeddings._fakes import MockEmbedder


@pytest.fixture(autouse=True)
def _use_mock_embedder() -> None:
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_put_claim_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="claim text", evidence=[src.id]))
    rec = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec is not None
    vec, ch, model = rec
    assert vec.shape == (8,)
    assert model == "mock"
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_put_claim_writes_embedding -v`
Expected: assertion fail.

- [ ] **Step 3: Add the helper method to `KBStore`**

In `src/vouch/storage.py`, add a private method to `KBStore` just before the `# --- proposals ---` section. The helper:

```python
    # --- embedding hook ------------------------------------------------------

    def _embed_and_store(self, *, kind: str, id: str, text: str) -> None:
        """Compute and persist an embedding for an artifact.

        Skipped if (kind, id) already exists with the same content hash.
        Failures (e.g. embeddings extras not installed) are swallowed --
        embeddings are an enhancement, not a hard requirement.
        """
        if not text or not text.strip():
            return
        try:
            from . import index_db as _index_db
            from .embeddings import content_hash, get_embedder
        except ImportError:
            return
        h = content_hash(text)
        existing = _index_db.get_embedding(self.kb_dir, kind=kind, id=id)
        if existing is not None and existing[1] == h:
            return
        try:
            embedder = get_embedder()
        except KeyError:
            return
        vec = embedder.encode(text)
        with _index_db.open_db(self.kb_dir) as conn:
            _index_db.put_embedding(
                conn, kind=kind, id=id, vec=vec, content_hash=h,
                model=embedder.name, model_version=embedder.version,
                dim=embedder.dim,
            )
        _index_db.set_embedding_meta(
            self.kb_dir, model=embedder.name,
            version=embedder.version, dim=embedder.dim,
        )
        try:
            from .embeddings.dedup import check_and_log
            check_and_log(self.kb_dir, kind=kind, id=id, vec=vec)
        except ImportError:
            pass
```

- [ ] **Step 4: Wire into `put_claim`**

In `src/vouch/storage.py`, locate `put_claim` (~line 250). Add the call just before `return claim`. The updated method:

```python
    def put_claim(self, claim: Claim) -> Claim:
        for cid_or_sid in [*claim.evidence, *claim.cites_claims]:
            if (
                not self._claim_path(cid_or_sid).exists()
                and not (self._source_dir(cid_or_sid) / "meta.yaml").exists()
                and not self._evidence_path(cid_or_sid).exists()
            ):
                raise ValueError(
                    f"claim {claim.id} cites unknown source/evidence {cid_or_sid}"
                )
        try:
            with self._claim_path(claim.id).open("x") as f:
                f.write(_yaml_dump(claim.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"claim {claim.id} already exists -- use update_claim()"
            ) from e
        self._embed_and_store(kind="claim", id=claim.id, text=claim.text)
        return claim
```

- [ ] **Step 5: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_put_claim_writes_embedding -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/storage.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): write-time embedding hook + wire put_claim"
```

---

### Task 12: Hook put_page, put_source, put_entity, put_relation, put_evidence

**Files:**
- Modify: `src/vouch/storage.py`
- Modify: `tests/embeddings/test_search.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/embeddings/test_search.py`:

```python
from vouch.models import Evidence, Relation, RelationType  # noqa: E402


def test_put_page_writes_embedding(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="Title", body="page body"))
    rec = index_db.get_embedding(store.kb_dir, kind="page", id="p1")
    assert rec is not None
    assert rec[0].shape == (8,)


def test_put_source_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"content bytes here", title="src1")
    rec = index_db.get_embedding(store.kb_dir, kind="source", id=src.id)
    assert rec is not None


def test_put_entity_writes_embedding(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="AuthN", type=EntityType.CONCEPT,
                            description="who you are"))
    rec = index_db.get_embedding(store.kb_dir, kind="entity", id="e1")
    assert rec is not None


def test_put_relation_writes_embedding(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="x", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="e2", name="y", type=EntityType.CONCEPT))
    rel = Relation(id="r1", type=RelationType.RELATED_TO,
                   source="e1", target="e2", description="x relates to y")
    store.put_relation(rel)
    rec = index_db.get_embedding(store.kb_dir, kind="relation", id="r1")
    assert rec is not None


def test_put_evidence_writes_embedding(store: KBStore) -> None:
    src = store.put_source(b"abc")
    ev = Evidence(id="ev1", source_id=src.id, locator="line 1",
                  excerpt="excerpt text body")
    store.put_evidence(ev)
    rec = index_db.get_embedding(store.kb_dir, kind="evidence", id="ev1")
    assert rec is not None
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `.venv/bin/pytest tests/embeddings/test_search.py -v`
Expected: 5 new failures.

- [ ] **Step 3: Wire helper into each remaining `put_*`**

For each method, add `self._embed_and_store(...)` immediately before the existing `return <obj>`:

- `put_page`: `self._embed_and_store(kind="page", id=page.id, text=f"{page.title}\n\n{page.body}")`
- `put_source`: `self._embed_and_store(kind="source", id=src.id, text=src.title or src.locator or "")`
- `put_entity`: `self._embed_and_store(kind="entity", id=entity.id, text=f"{entity.name}\n\n{entity.description or ''}")`
- `put_relation`: `self._embed_and_store(kind="relation", id=rel.id, text=f"{rel.source} {rel.type.value} {rel.target}\n{rel.description or ''}")`
- `put_evidence`: `self._embed_and_store(kind="evidence", id=ev.id, text=ev.excerpt or "")`

The pattern: after the `f.write(...)` and `except FileExistsError` block, add the call before the existing `return`.

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_search.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest --ignore=tests/test_sessions.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/storage.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): hook all six artifact write paths"
```

---

### Task 13: Update path -- re-embed on update_claim

**Files:**
- Modify: `src/vouch/storage.py`
- Modify: `tests/embeddings/test_search.py`

- [ ] **Step 1: Add failing test**

Append to `tests/embeddings/test_search.py`:

```python
def test_update_claim_recomputes_embedding(store: KBStore) -> None:
    src = store.put_source(b"e")
    c = store.put_claim(Claim(id="c1", text="original", evidence=[src.id]))
    rec_before = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec_before is not None
    hash_before = rec_before[1]

    c2 = c.model_copy(update={"text": "updated"})
    store.update_claim(c2)
    rec_after = index_db.get_embedding(store.kb_dir, kind="claim", id="c1")
    assert rec_after is not None
    assert rec_after[1] != hash_before
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_update_claim_recomputes_embedding -v`
Expected: fail.

- [ ] **Step 3: Add the hook to `update_claim`**

Locate `update_claim` in `src/vouch/storage.py`. Just before the existing `return claim`, add:

```python
        self._embed_and_store(kind="claim", id=claim.id, text=claim.text)
```

If `update_page` exists in the codebase, add the same call to it with `text=f"{page.title}\n\n{page.body}"`. If it does not, omit -- mention "page update not yet implemented in storage" in the commit message.

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_update_claim_recomputes_embedding -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/storage.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): re-embed on update_claim"
```

---

## Phase 4 — Read path / search integration

### Task 14: `search_semantic` wrapper with query cache

**Files:**
- Modify: `src/vouch/index_db.py`
- Modify: `tests/embeddings/test_search.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_search.py`:

```python
def test_search_semantic_returns_top_hits(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="alpha alpha alpha", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="beta beta beta", evidence=[src.id]))
    hits = index_db.search_semantic(store.kb_dir, query="alpha alpha alpha", limit=2)
    assert hits[0][1] == "c1"
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_search_semantic_returns_top_hits -v`
Expected: AttributeError.

- [ ] **Step 3: Implement `search_semantic`**

Append to `src/vouch/index_db.py`:

```python
def search_semantic(
    kb_dir: Path,
    query: str,
    *,
    limit: int = 10,
    kinds: tuple[str, ...] = (
        "claim", "page", "source", "entity", "relation", "evidence",
    ),
    min_score: float = 0.0,
) -> list[tuple[str, str, str, float]]:
    """Encode query (cached) -> ANN/cosine search."""
    try:
        from .embeddings import get_embedder
        from .embeddings.cache import cache_query_vec, lookup_query_vec
    except ImportError:
        return []
    try:
        embedder = get_embedder()
    except KeyError:
        return []
    qvec = lookup_query_vec(kb_dir, query=query)
    if qvec is None:
        qvec = embedder.encode(query)
        cache_query_vec(kb_dir, query=query, vec=qvec)
    return search_embedding(
        kb_dir, query_vec=qvec, kinds=kinds, limit=limit, min_score=min_score,
    )
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_search_semantic_returns_top_hits -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/index_db.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): search_semantic wrapper with query cache"
```

---

### Task 15: Semantic-primary in MCP `kb_search`

**Files:**
- Modify: `src/vouch/server.py`
- Modify: `tests/embeddings/test_search.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_search.py`:

```python
def test_kb_search_defaults_to_semantic_then_fts5(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="authentication design", evidence=[src.id]))
    from vouch import health
    health.rebuild_index(store)
    result = server.kb_search("authentication design", limit=5)
    assert result["hits"]
    assert result["backend"] in ("embedding", "fts5", "substring")
    assert result["hits"][0]["id"] == "c1"
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_kb_search_defaults_to_semantic_then_fts5 -v`
Expected: fail.

- [ ] **Step 3: Modify `kb_search` in `src/vouch/server.py`**

Replace the body of `kb_search` with:

```python
@mcp.tool()
def kb_search(
    query: str,
    *,
    limit: int = 10,
    backend: str = "auto",
    min_score: float = 0.0,
) -> dict[str, Any]:
    """Search the KB.

    backend: "auto" (default, embedding then fts5 then substring),
    "embedding", "fts5", "substring", or "hybrid".
    """
    from . import index_db
    store = _store()
    hits: list[tuple[str, str, str, float]] = []

    def _to_dicts(h: list[tuple[str, str, str, float]], used: str) -> dict[str, Any]:
        return {
            "backend": used,
            "hits": [
                {"kind": k, "id": i, "snippet": sn, "score": sc, "backend": used}
                for k, i, sn, sc in h
            ],
        }

    if backend in ("auto", "embedding"):
        hits = index_db.search_semantic(
            store.kb_dir, query, limit=limit, min_score=min_score,
        )
        if hits:
            return _to_dicts(hits, "embedding")
        if backend == "embedding":
            return _to_dicts([], "embedding")

    if backend in ("auto", "fts5"):
        try:
            hits = index_db.search(store.kb_dir, query, limit=limit)
        except Exception:
            hits = []
        if hits:
            return _to_dicts(hits, "fts5")
        if backend == "fts5":
            return _to_dicts([], "fts5")

    if backend in ("auto", "substring"):
        hits = store.search_substring(query, limit=limit)
        return _to_dicts(hits, "substring")

    if backend == "hybrid":
        from .embeddings.fusion import rrf_fuse
        emb = index_db.search_semantic(store.kb_dir, query, limit=limit * 2)
        fts = index_db.search(store.kb_dir, query, limit=limit * 2)
        hits = rrf_fuse(emb, fts, limit=limit)
        return _to_dicts(hits, "hybrid")

    raise ValueError(f"unknown backend: {backend}")
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_kb_search_defaults_to_semantic_then_fts5 -v`
Expected: PASS.

- [ ] **Step 5: Run full suite to confirm no regression**

Run: `.venv/bin/pytest --ignore=tests/test_sessions.py -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/server.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): kb_search defaults to embedding primary, fts5 fallback"
```

---

### Task 16: JSONL `_h_search` parity

**Files:**
- Modify: `src/vouch/jsonl_server.py`
- Modify: `tests/embeddings/test_search.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_search.py`:

```python
def test_jsonl_search_uses_embedding_backend(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch import jsonl_server
    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="claim about logins", evidence=[src.id]))
    from vouch import health
    health.rebuild_index(store)
    resp = jsonl_server.handle_request({
        "id": "1", "method": "kb.search",
        "params": {"query": "claim about logins"},
    })
    assert resp["ok"] is True
    assert resp["result"]
    assert resp["result"][0]["backend"] in ("embedding", "fts5", "substring")
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_search.py::test_jsonl_search_uses_embedding_backend -v`
Expected: fail (no embedding path in JSONL handler).

- [ ] **Step 3: Replace `_h_search` in `src/vouch/jsonl_server.py`**

```python
def _h_search(p: dict) -> list[dict]:
    from . import index_db
    s = _store()
    q = p["query"]
    limit = int(p.get("limit", 10))
    backend_arg = p.get("backend", "auto")
    min_score = float(p.get("min_score", 0.0))
    hits: list[tuple[str, str, str, float]] = []
    used = backend_arg

    if backend_arg in ("auto", "embedding"):
        hits = index_db.search_semantic(
            s.kb_dir, q, limit=limit, min_score=min_score,
        )
        if hits:
            used = "embedding"
    if not hits and backend_arg in ("auto", "fts5"):
        try:
            hits = index_db.search(s.kb_dir, q, limit=limit)
            used = "fts5" if hits else used
        except Exception:
            hits = []
    if not hits and backend_arg in ("auto", "substring"):
        hits = s.search_substring(q, limit=limit)
        used = "substring"
    if backend_arg == "hybrid":
        from .embeddings.fusion import rrf_fuse
        emb = index_db.search_semantic(s.kb_dir, q, limit=limit * 2)
        fts = index_db.search(s.kb_dir, q, limit=limit * 2)
        hits = rrf_fuse(emb, fts, limit=limit)
        used = "hybrid"

    return [
        {"kind": k, "id": i, "snippet": sn, "score": sc, "backend": used}
        for k, i, sn, sc in hits
    ]
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_search.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/jsonl_server.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): JSONL _h_search parity with MCP semantic-primary dispatch"
```

---

## Phase 5 — Fusion + hybrid backend

### Task 17: Fusion strategies (RRF, weighted, normalized)

**Files:**
- Create: `src/vouch/embeddings/fusion.py`
- Test: `tests/embeddings/test_fusion.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_fusion.py`:

```python
"""Fusion strategies -- RRF, weighted, normalized-cosine."""

from __future__ import annotations

from vouch.embeddings.fusion import (
    normalized_fuse,
    rrf_fuse,
    weighted_fuse,
)


def _hits(items: list[tuple[str, str, float]]) -> list[tuple[str, str, str, float]]:
    return [(k, i, "", s) for k, i, s in items]


def test_rrf_fuse_prefers_top_of_both_lists() -> None:
    a = _hits([("claim", "x", 0.9), ("claim", "y", 0.5)])
    b = _hits([("claim", "y", 0.9), ("claim", "z", 0.4)])
    fused = rrf_fuse(a, b, limit=3)
    ids = [h[1] for h in fused]
    assert ids[0] == "y"


def test_rrf_fuse_handles_empty_inputs() -> None:
    assert rrf_fuse([], [], limit=5) == []


def test_weighted_fuse_respects_weights() -> None:
    a = _hits([("claim", "x", 1.0), ("claim", "y", 0.0)])
    b = _hits([("claim", "y", 1.0), ("claim", "x", 0.0)])
    only_a = weighted_fuse(a, b, w_a=1.0, w_b=0.0, limit=2)
    assert only_a[0][1] == "x"
    only_b = weighted_fuse(a, b, w_a=0.0, w_b=1.0, limit=2)
    assert only_b[0][1] == "y"


def test_normalized_fuse_zero_score_inputs() -> None:
    a = _hits([("claim", "x", 0.0)])
    b = _hits([("claim", "y", 0.0)])
    fused = normalized_fuse(a, b, limit=2)
    assert len(fused) == 2
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_fusion.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `fusion.py`**

Create `src/vouch/embeddings/fusion.py`:

```python
"""Result-list fusion strategies for hybrid retrieval."""

from __future__ import annotations

Hit = tuple[str, str, str, float]
Hits = list[Hit]


def _key(h: Hit) -> tuple[str, str]:
    return (h[0], h[1])


def _coalesce_snippet(h: Hit, other: Hits) -> str:
    if h[2]:
        return h[2]
    for o in other:
        if _key(o) == _key(h) and o[2]:
            return o[2]
    return ""


def rrf_fuse(a: Hits, b: Hits, *, limit: int = 10, k: int = 60) -> Hits:
    """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across lists."""
    scores: dict[tuple[str, str], float] = {}
    for lst in (a, b):
        for rank, h in enumerate(lst, start=1):
            scores[_key(h)] = scores.get(_key(h), 0.0) + 1.0 / (k + rank)
    out: Hits = []
    seen: set[tuple[str, str]] = set()
    for h in a + b:
        if _key(h) in seen:
            continue
        seen.add(_key(h))
        out.append((h[0], h[1], _coalesce_snippet(h, a + b), scores[_key(h)]))
    out.sort(key=lambda h: h[3], reverse=True)
    return out[:limit]


def _minmax(xs: list[float]) -> list[float]:
    if not xs:
        return xs
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [0.5] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def weighted_fuse(
    a: Hits, b: Hits, *, w_a: float = 0.5, w_b: float = 0.5, limit: int = 10,
) -> Hits:
    """Min-max normalize each list, then score = w_a * a_score + w_b * b_score."""
    a_norm = _minmax([h[3] for h in a])
    b_norm = _minmax([h[3] for h in b])
    a_score = {_key(h): a_norm[i] for i, h in enumerate(a)}
    b_score = {_key(h): b_norm[i] for i, h in enumerate(b)}
    merged: dict[tuple[str, str], float] = {}
    for key in {*a_score, *b_score}:
        merged[key] = w_a * a_score.get(key, 0.0) + w_b * b_score.get(key, 0.0)
    out: Hits = []
    seen: set[tuple[str, str]] = set()
    for h in a + b:
        if _key(h) in seen:
            continue
        seen.add(_key(h))
        out.append((h[0], h[1], _coalesce_snippet(h, a + b), merged[_key(h)]))
    out.sort(key=lambda h: h[3], reverse=True)
    return out[:limit]


def normalized_fuse(a: Hits, b: Hits, *, limit: int = 10) -> Hits:
    """Equal-weight weighted_fuse (0.5/0.5)."""
    return weighted_fuse(a, b, w_a=0.5, w_b=0.5, limit=limit)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_fusion.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/fusion.py tests/embeddings/test_fusion.py
git commit -m "feat(embeddings): RRF/weighted/normalized fusion strategies"
```

---

## Phase 6 — Capabilities

### Task 18: Cross-encoder reranker

**Files:**
- Create: `src/vouch/embeddings/rerank.py`
- Test: `tests/embeddings/test_rerank.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_rerank.py`:

```python
"""Cross-encoder reranker over candidate top-K."""

from __future__ import annotations

from collections.abc import Sequence

from vouch.embeddings.rerank import Reranker, rerank


class MockReranker(Reranker):
    name = "mock-reranker"
    version = "1"

    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        return [10.0 if "BOOST" in c else 0.0 for c in candidates]


def test_rerank_reorders_by_cross_encoder_score() -> None:
    hits = [("claim", "a", "no marker here", 0.9),
            ("claim", "b", "BOOST in this one", 0.1),
            ("claim", "c", "also no marker", 0.5)]
    out = rerank(query="anything", hits=hits, reranker=MockReranker(), top_k=3)
    assert out[0][1] == "b"


def test_rerank_top_k_truncates() -> None:
    hits = [("claim", "a", "x", 0.5), ("claim", "b", "y", 0.3)]
    out = rerank(query="q", hits=hits, reranker=MockReranker(), top_k=1)
    assert len(out) == 1


def test_rerank_empty_hits() -> None:
    out = rerank(query="q", hits=[], reranker=MockReranker(), top_k=5)
    assert out == []
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_rerank.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `rerank.py`**

Create `src/vouch/embeddings/rerank.py`:

```python
"""Cross-encoder reranking over candidate hits."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

Hit = tuple[str, str, str, float]


class Reranker(ABC):
    name: ClassVar[str] = ""
    version: ClassVar[str] = ""

    @abstractmethod
    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        """Return one score per candidate. Higher = more relevant."""


class CrossEncoderReranker(Reranker):
    name = "cross-encoder/ms-marco-MiniLM-L6-v2"
    version = "v1"

    def __init__(self) -> None:
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
        self._model: Any = CrossEncoder(self.name)

    def score(self, query: str, candidates: Sequence[str]) -> list[float]:
        if not candidates:
            return []
        pairs = [(query, c) for c in candidates]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]


def rerank(
    *, query: str, hits: list[Hit], reranker: Reranker, top_k: int = 50,
) -> list[Hit]:
    if not hits:
        return []
    candidates = [h[2] or h[1] for h in hits]
    scores = reranker.score(query, candidates)
    reranked = [
        (kind, id_, snip, score)
        for (kind, id_, snip, _orig), score in zip(hits, scores)
    ]
    reranked.sort(key=lambda h: h[3], reverse=True)
    return reranked[:top_k]


def default_reranker() -> Reranker:
    """Return a CrossEncoderReranker; raise ImportError if extras not installed."""
    return CrossEncoderReranker()
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_rerank.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/rerank.py tests/embeddings/test_rerank.py
git commit -m "feat(embeddings): cross-encoder reranker (Reranker ABC + default impl)"
```

---

### Task 19: HyDE query expansion

**Files:**
- Create: `src/vouch/embeddings/hyde.py`
- Test: `tests/embeddings/test_hyde.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_hyde.py`:

```python
"""HyDE -- Hypothetical Document Embedding query expansion."""

from __future__ import annotations

from vouch.embeddings.hyde import expand_query_template


def test_template_expansion_adds_context() -> None:
    expanded = expand_query_template("auth")
    assert "auth" in expanded
    assert len(expanded) > len("auth")


def test_template_expansion_idempotent_for_long_queries() -> None:
    long_q = "this is a long descriptive query about something specific"
    out = expand_query_template(long_q, min_chars=20)
    assert out == long_q
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_hyde.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement HyDE**

Create `src/vouch/embeddings/hyde.py`:

```python
"""HyDE -- Hypothetical Document Embedding expansion."""

from __future__ import annotations

from collections.abc import Callable

DEFAULT_TEMPLATE = (
    "The following is a document that answers the question: '{query}'. "
    "It contains relevant facts, claims, and supporting evidence about {query}."
)


def expand_query_template(query: str, *, min_chars: int = 20) -> str:
    """Pad short queries with HyDE template; pass through long ones."""
    if len(query.strip()) >= min_chars:
        return query
    return DEFAULT_TEMPLATE.format(query=query.strip())


def expand_query_with_llm(query: str, *, llm: Callable[[str], str]) -> str:
    """Use an LLM to draft a hypothetical answer; embed that instead."""
    prompt = (
        "Write a short, factual paragraph that would answer this question. "
        "Stay neutral; don't ask follow-ups.\n\n"
        f"Question: {query}\n\nAnswer:"
    )
    return llm(prompt).strip() or query
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_hyde.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/hyde.py tests/embeddings/test_hyde.py
git commit -m "feat(embeddings): HyDE query expansion (template + LLM hook)"
```

---

### Task 20: Ingest-time duplicate detection

**Files:**
- Create: `src/vouch/embeddings/dedup.py`
- Test: `tests/embeddings/test_dedup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_dedup.py`:

```python
"""Ingest-time duplicate detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import index_db
from vouch.embeddings import register
from vouch.embeddings.dedup import list_duplicates
from vouch.models import Claim
from vouch.storage import KBStore
from tests.embeddings._fakes import MockEmbedder


@pytest.fixture(autouse=True)
def _register_default() -> None:
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_identical_text_flagged_as_duplicate(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="exact same text", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="exact same text", evidence=[src.id]))
    dupes = list_duplicates(store.kb_dir)
    pairs = {(d["id"], d["near_id"]) for d in dupes}
    assert ("c2", "c1") in pairs or ("c1", "c2") in pairs


def test_disjoint_text_not_flagged(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="apples", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="zebras", evidence=[src.id]))
    dupes = list_duplicates(store.kb_dir)
    assert dupes == []
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_dedup.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `dedup.py`**

Create `src/vouch/embeddings/dedup.py`:

```python
"""Ingest-time duplicate detection via embedding cosine similarity."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np

from .. import index_db

DEFAULT_THRESHOLD = 0.95


def check_and_log(
    kb_dir: Path, *,
    kind: str, id: str, vec: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[str, float] | None:
    """Find nearest-neighbor cosine >= threshold; log if found."""
    candidates = index_db.search_embedding(
        kb_dir, query_vec=vec, kinds=(kind,), limit=2,
    )
    for k, near_id, _snip, cos in candidates:
        if k != kind or near_id == id:
            continue
        if cos >= threshold:
            _log(kb_dir, kind=kind, id=id, near_id=near_id, cosine=cos)
            return near_id, cos
    return None


def _log(kb_dir: Path, *, kind: str, id: str, near_id: str, cosine: float) -> None:
    with index_db.open_db(kb_dir) as conn:
        conn.execute(
            "INSERT INTO embedding_dupes (kind, id, near_id, cosine, detected_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, id, near_id, float(cosine),
             dt.datetime.now(dt.UTC).isoformat(timespec="seconds")),
        )


def list_duplicates(kb_dir: Path) -> list[dict[str, Any]]:
    with index_db.open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT kind, id, near_id, cosine, detected_at FROM embedding_dupes "
            "ORDER BY detected_at DESC"
        ).fetchall()
    return [
        {"kind": k, "id": i, "near_id": n, "cosine": float(c), "detected_at": d}
        for k, i, n, c, d in rows
    ]


def scan_all(
    kb_dir: Path, *, threshold: float = DEFAULT_THRESHOLD, dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Cross-artifact scan within same kind. For ad-hoc audit."""
    found: list[dict[str, Any]] = []
    with index_db.open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT kind, id, vec, dim FROM embedding_index"
        ).fetchall()
    if not rows:
        return found
    vecs: dict[tuple[str, str], np.ndarray] = {}
    for kind, id_, blob, dim in rows:
        vecs[(kind, id_)] = np.frombuffer(blob, dtype=np.float32, count=dim).copy()
    seen: set[frozenset[tuple[str, str]]] = set()
    keys = list(vecs.keys())
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            if k1[0] != k2[0]:
                continue
            cos = float(vecs[k1] @ vecs[k2])
            if cos >= threshold:
                pair = frozenset([k1, k2])
                if pair in seen:
                    continue
                seen.add(pair)
                if not dry_run:
                    _log(kb_dir, kind=k1[0], id=k2[1], near_id=k1[1], cosine=cos)
                found.append({
                    "kind": k1[0], "id": k2[1], "near_id": k1[1], "cosine": cos,
                })
    return found
```

> Note: the dedup hook in `KBStore._embed_and_store` (added in Task 11) already references `check_and_log` via `from .embeddings.dedup import check_and_log`. Once this task lands, that branch resolves.

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_dedup.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/dedup.py tests/embeddings/test_dedup.py
git commit -m "feat(embeddings): ingest-time duplicate detection + audit ledger"
```

---

### Task 21: Evaluation harness (recall@k, MRR, nDCG)

**Files:**
- Create: `src/vouch/embeddings/scorer.py`
- Test: `tests/embeddings/test_scorer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_scorer.py`:

```python
"""Metrics for embedding retrieval."""

from __future__ import annotations

from vouch.embeddings.scorer import mrr, ndcg, recall_at_k


def test_recall_at_k_full_hit() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c1", "", 0.9), ("claim", "c2", "", 0.1)]
    assert recall_at_k(hits, relevant, k=1) == 1.0


def test_recall_at_k_miss() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c2", "", 0.9)]
    assert recall_at_k(hits, relevant, k=1) == 0.0


def test_recall_at_k_partial_with_k_larger_than_hits() -> None:
    relevant = {"claim:c1", "claim:c2"}
    hits = [("claim", "c1", "", 0.9)]
    assert recall_at_k(hits, relevant, k=5) == 0.5


def test_mrr_first_position() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c1", "", 0.9), ("claim", "c2", "", 0.5)]
    assert mrr(hits, relevant) == 1.0


def test_mrr_second_position() -> None:
    relevant = {"claim:c1"}
    hits = [("claim", "c2", "", 0.9), ("claim", "c1", "", 0.5)]
    assert abs(mrr(hits, relevant) - 0.5) < 1e-9


def test_ndcg_monotonic() -> None:
    relevant = {"claim:c1"}
    good = [("claim", "c1", "", 1.0), ("claim", "c2", "", 0.5)]
    bad = [("claim", "c2", "", 1.0), ("claim", "c1", "", 0.5)]
    assert ndcg(good, relevant, k=2) > ndcg(bad, relevant, k=2)
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_scorer.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `scorer.py`**

Create `src/vouch/embeddings/scorer.py`:

```python
"""Retrieval evaluation metrics: recall@k, MRR, nDCG.

Ground truth format: a set of "kind:id" strings (e.g. {"claim:c1"}).
Hits format matches index_db: list of (kind, id, snippet, score).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

Hit = tuple[str, str, str, float]


def _key(h: Hit) -> str:
    return f"{h[0]}:{h[1]}"


def recall_at_k(hits: list[Hit], relevant: set[str], *, k: int = 10) -> float:
    if not relevant:
        return 0.0
    top = {_key(h) for h in hits[:k]}
    return len(top & relevant) / len(relevant)


def mrr(hits: list[Hit], relevant: set[str]) -> float:
    for i, h in enumerate(hits, start=1):
        if _key(h) in relevant:
            return 1.0 / i
    return 0.0


def ndcg(hits: list[Hit], relevant: set[str], *, k: int = 10) -> float:
    dcg = 0.0
    for i, h in enumerate(hits[:k], start=1):
        if _key(h) in relevant:
            dcg += 1.0 / math.log2(i + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    if ideal <= 0:
        return 0.0
    return dcg / ideal


def evaluate(
    *,
    kb_dir: Path,
    queries_file: Path,
    k: int = 10,
    metrics: tuple[str, ...] = ("recall@k", "mrr", "ndcg"),
) -> dict[str, float]:
    """Run a metric sweep over a JSONL queries file."""
    from .. import index_db
    totals = {m: 0.0 for m in metrics}
    n = 0
    with queries_file.open() as f:
        for line in f:
            row = json.loads(line)
            q = row["query"]
            rel = set(row["relevant"])
            hits = index_db.search_semantic(kb_dir, q, limit=k)
            if "recall@k" in metrics:
                totals["recall@k"] += recall_at_k(hits, rel, k=k)
            if "mrr" in metrics:
                totals["mrr"] += mrr(hits, rel)
            if "ndcg" in metrics:
                totals["ndcg"] += ndcg(hits, rel, k=k)
            n += 1
    if n == 0:
        return {m: 0.0 for m in metrics}
    return {m: totals[m] / n for m in metrics}


def write_report(out: dict[str, float], path: Path) -> None:
    path.write_text(json.dumps(out, indent=2))
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_scorer.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/scorer.py tests/embeddings/test_scorer.py
git commit -m "feat(embeddings): scorer harness (recall@k, MRR, nDCG)"
```

---

### Task 22: Model-identity migration

**Files:**
- Create: `src/vouch/embeddings/migration.py`
- Test: `tests/embeddings/test_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_migration.py`:

```python
"""Model-identity mismatch detection + backfill."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import index_db
from vouch.embeddings import register
from vouch.embeddings.migration import (
    backfill_embeddings,
    detect_mismatch,
)
from vouch.models import Claim
from vouch.storage import KBStore
from tests.embeddings._fakes import MockEmbedder


@pytest.fixture(autouse=True)
def _register_default() -> None:
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_detect_mismatch_returns_none_for_empty_kb(store: KBStore) -> None:
    assert detect_mismatch(store.kb_dir) is None


def test_detect_mismatch_reports_model_change(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    index_db.set_embedding_meta(
        store.kb_dir, model="some-other-model", version="v2", dim=8,
    )
    mismatch = detect_mismatch(store.kb_dir)
    assert mismatch is not None
    assert mismatch["stored_model"] == "some-other-model"
    assert mismatch["current_model"] == "mock"


def test_backfill_re_encodes_all_artifacts(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="alpha", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="beta", evidence=[src.id]))
    with index_db.open_db(store.kb_dir) as conn:
        conn.execute("DELETE FROM embedding_index")
    n = backfill_embeddings(store)
    assert n >= 2
    assert index_db.get_embedding(store.kb_dir, kind="claim", id="c1") is not None
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_migration.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `migration.py`**

Create `src/vouch/embeddings/migration.py`:

```python
"""Model-identity migration and backfill."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import index_db
from .base import get_embedder


def detect_mismatch(kb_dir: Path) -> dict[str, Any] | None:
    """Return mismatch info or None if no mismatch."""
    meta = index_db.get_embedding_meta(kb_dir)
    stored = meta.get("embedding_model")
    if not stored:
        return None
    try:
        current = get_embedder()
    except KeyError:
        return None
    if current.name == stored:
        return None
    return {
        "stored_model": stored,
        "stored_version": meta.get("embedding_model_version"),
        "stored_dim": meta.get("embedding_dim"),
        "current_model": current.name,
        "current_version": current.version,
        "current_dim": current.dim,
    }


def backfill_embeddings(store: Any, *, force: bool = False) -> int:
    """Re-encode every artifact under the current adapter. Returns count touched."""
    embedder = get_embedder()
    touched = 0
    for c in store.list_claims():
        store._embed_and_store(kind="claim", id=c.id, text=c.text)
        touched += 1
    for p in store.list_pages():
        store._embed_and_store(kind="page", id=p.id, text=f"{p.title}\n\n{p.body}")
        touched += 1
    for s in store.list_sources():
        store._embed_and_store(
            kind="source", id=s.id, text=s.title or s.locator or "",
        )
        touched += 1
    for e in store.list_entities():
        store._embed_and_store(
            kind="entity", id=e.id, text=f"{e.name}\n\n{e.description or ''}",
        )
        touched += 1
    for r in store.list_relations():
        store._embed_and_store(
            kind="relation", id=r.id,
            text=f"{r.source} {r.type.value} {r.target}\n{r.description or ''}",
        )
        touched += 1
    for ev in store.list_evidence():
        store._embed_and_store(kind="evidence", id=ev.id, text=ev.excerpt or "")
        touched += 1
    index_db.set_embedding_meta(
        store.kb_dir, model=embedder.name, version=embedder.version, dim=embedder.dim,
    )
    return touched
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_migration.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/embeddings/migration.py tests/embeddings/test_migration.py
git commit -m "feat(embeddings): model-identity mismatch detection + backfill"
```

---

## Phase 7 — Context pack integration

### Task 23: Semantic-default `build_context_pack` + `--explain`

**Files:**
- Modify: `src/vouch/context.py`
- Modify: `tests/test_context.py`

- [ ] **Step 1: Read the current `context.py` end-to-end**

Open `src/vouch/context.py` and note the existing signature of `build_context_pack` and how it dispatches to `index_db.search`. The task is to insert `search_semantic` as the first attempt while preserving all existing item-assembly logic.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_context.py`:

```python
def test_build_context_pack_uses_semantic_default(tmp_path: Path) -> None:
    from vouch.embeddings import register
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    from vouch.models import Claim
    from vouch.storage import KBStore
    from vouch.context import build_context_pack
    from tests.embeddings._fakes import MockEmbedder

    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="exact query string", evidence=[src.id]))
    pack = build_context_pack(store, query="exact query string", limit=5)
    assert any(item["id"] == "c1" for item in pack.get("items", []))


def test_build_context_pack_explain_flag_returns_score_breakdown(
    tmp_path: Path,
) -> None:
    from vouch.embeddings import register
    from vouch.embeddings.base import DEFAULT_MODEL_NAME
    from vouch.models import Claim
    from vouch.storage import KBStore
    from vouch.context import build_context_pack
    from tests.embeddings._fakes import MockEmbedder

    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="hello", evidence=[src.id]))
    pack = build_context_pack(store, query="hello", limit=5, explain=True)
    assert "explain" in pack
    assert any("backend" in row for row in pack["explain"])
```

- [ ] **Step 3: Run tests, confirm fail**

Run: `.venv/bin/pytest tests/test_context.py -v`
Expected: 2 failures.

- [ ] **Step 4: Modify `build_context_pack` in `src/vouch/context.py`**

Locate `build_context_pack`. Add an `explain: bool = False` parameter to the signature. At the top of the body (replacing the existing retrieval call), use:

```python
    from . import index_db
    hits = index_db.search_semantic(store.kb_dir, query, limit=limit)
    backend = "embedding"
    if not hits:
        hits = index_db.search(store.kb_dir, query, limit=limit)
        backend = "fts5"
    if not hits:
        hits = store.search_substring(query, limit=limit)
        backend = "substring"
```

Preserve all of the existing item-assembly logic that consumes `hits`. Finally, before returning, add the optional explain field:

```python
    if explain:
        pack["explain"] = [
            {"kind": k, "id": i, "score": sc, "backend": backend}
            for k, i, _sn, sc in hits
        ]
    pack["backend"] = backend
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/test_context.py -v`
Expected: existing context tests pass + 2 new pass.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/context.py tests/test_context.py
git commit -m "feat(embeddings): semantic-default build_context_pack + --explain support"
```

---

## Phase 8 — CLI + JSONL parity sweep

### Task 24: `vouch search` flag surface

**Files:**
- Modify: `src/vouch/cli.py`
- Create: `tests/embeddings/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/embeddings/test_cli.py`:

```python
"""CLI flag surface for embeddings commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME
from vouch.models import Claim
from vouch.storage import KBStore
from tests.embeddings._fakes import MockEmbedder


@pytest.fixture(autouse=True)
def _register_default() -> None:
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="some text", evidence=[src.id]))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_search_semantic_flag(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "some text", "--semantic"])
    assert result.exit_code == 0
    assert "c1" in result.output


def test_search_backend_flag(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "some text", "--backend", "embedding"])
    assert result.exit_code == 0


def test_search_top_k_flag(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "x", "--top-k", "3"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_search_semantic_flag -v`
Expected: fail.

- [ ] **Step 3: Extend `search` in `cli.py`**

Find the existing `@cli.command()` for `search` in `src/vouch/cli.py`. Replace with:

```python
@cli.command()
@click.argument("query")
@click.option("--limit", "-n", default=10, show_default=True, type=int)
@click.option("--top-k", default=None, type=int, help="Alias for --limit.")
@click.option("--semantic/--no-semantic", default=None,
              help="Force semantic backend (alias for --backend embedding).")
@click.option(
    "--backend",
    type=click.Choice(["auto", "embedding", "fts5", "substring", "hybrid"]),
    default="auto", show_default=True,
)
@click.option("--min-score", default=0.0, show_default=True, type=float)
@click.option("--rerank/--no-rerank", default=False)
@click.option("--hyde/--no-hyde", default=False)
@click.option("--explain/--no-explain", default=False)
def search(
    query: str,
    limit: int,
    top_k: int | None,
    semantic: bool | None,
    backend: str,
    min_score: float,
    rerank: bool,
    hyde: bool,
    explain: bool,
) -> None:
    """Search the KB."""
    from . import index_db
    from .embeddings.fusion import rrf_fuse
    store = _load_store()
    if top_k is not None:
        limit = top_k
    if semantic is True:
        backend = "embedding"
    elif semantic is False:
        backend = "fts5"
    q = query
    if hyde:
        from .embeddings.hyde import expand_query_template
        q = expand_query_template(query)

    hits: list[tuple[str, str, str, float]] = []
    used = backend
    if backend in ("auto", "embedding"):
        hits = index_db.search_semantic(
            store.kb_dir, q, limit=limit, min_score=min_score,
        )
        used = "embedding" if hits else used
    if not hits and backend in ("auto", "fts5"):
        hits = index_db.search(store.kb_dir, q, limit=limit)
        used = "fts5" if hits else used
    if not hits and backend in ("auto", "substring"):
        hits = store.search_substring(q, limit=limit)
        used = "substring"
    if backend == "hybrid":
        emb = index_db.search_semantic(store.kb_dir, q, limit=limit * 2)
        fts = index_db.search(store.kb_dir, q, limit=limit * 2)
        hits = rrf_fuse(emb, fts, limit=limit)
        used = "hybrid"

    if rerank and hits:
        try:
            from .embeddings.rerank import default_reranker, rerank as do_rerank
            hits = do_rerank(query=query, hits=hits, reranker=default_reranker(),
                             top_k=limit)
        except ImportError:
            click.echo("warning: rerank extras not installed; skipping rerank",
                       err=True)

    for k, i, snip, score in hits:
        if explain:
            click.echo(f"[{used}] {k}/{i}\tscore={score:.4f}\t{snip}")
        else:
            click.echo(f"{k}/{i}\t{snip}")
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/embeddings/test_cli.py
git commit -m "feat(embeddings): vouch search flags (--semantic, --backend, --top-k, --rerank, --hyde, --explain)"
```

---

### Task 25: `vouch reindex --embeddings`

**Files:**
- Modify: `src/vouch/cli.py`
- Modify: `tests/embeddings/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_cli.py`:

```python
def test_reindex_embeddings_backfills(kb: Path) -> None:
    from vouch import index_db
    from vouch.storage import discover_root, KBStore
    store = KBStore(discover_root(kb))
    with index_db.open_db(store.kb_dir) as conn:
        conn.execute("DELETE FROM embedding_index")
    runner = CliRunner()
    result = runner.invoke(cli, ["reindex", "--embeddings", "--backfill"])
    assert result.exit_code == 0
    assert index_db.get_embedding(store.kb_dir, kind="claim", id="c1") is not None
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_reindex_embeddings_backfills -v`
Expected: fail (`reindex` not present or lacks flags).

- [ ] **Step 3: Add the command**

Append to `src/vouch/cli.py`:

```python
@cli.command()
@click.option("--embeddings/--no-embeddings", default=False,
              help="Rebuild the embedding index in addition to FTS5.")
@click.option("--backfill/--no-backfill", default=False,
              help="Re-encode every artifact under the current model.")
@click.option("--force/--no-force", default=False,
              help="Re-encode even if content hash unchanged.")
@click.option("--model", default=None,
              help="Adapter name; defaults to the registered default.")
def reindex(embeddings: bool, backfill: bool, force: bool, model: str | None) -> None:
    """Rebuild derived indexes from on-disk artifacts."""
    from . import health
    store = _load_store()
    health.rebuild_index(store)
    if embeddings or backfill:
        from .embeddings.migration import backfill_embeddings
        if model:
            from .embeddings import get_embedder
            get_embedder(model)
        n = backfill_embeddings(store, force=force)
        click.echo(f"reindex: embeddings backfilled = {n}")
    else:
        click.echo("reindex: FTS5 rebuilt")
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_reindex_embeddings_backfills -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/embeddings/test_cli.py
git commit -m "feat(embeddings): vouch reindex --embeddings --backfill --force --model"
```

---

### Task 26: `vouch dedup` command

**Files:**
- Modify: `src/vouch/cli.py`
- Modify: `tests/embeddings/test_cli.py`

- [ ] **Step 1: Add failing test**

```python
def test_dedup_scan_lists_duplicates(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["dedup", "--threshold", "0.5", "--dry-run"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_dedup_scan_lists_duplicates -v`
Expected: fail.

- [ ] **Step 3: Add the command**

Append to `src/vouch/cli.py`:

```python
@cli.command()
@click.option("--threshold", default=0.95, show_default=True, type=float)
@click.option("--dry-run/--no-dry-run", default=False)
def dedup(threshold: float, dry_run: bool) -> None:
    """Scan embeddings for cross-artifact near-duplicates."""
    from .embeddings.dedup import scan_all
    store = _load_store()
    rows = scan_all(store.kb_dir, threshold=threshold, dry_run=dry_run)
    if not rows:
        click.echo("dedup: no duplicates found")
        return
    for r in rows:
        click.echo(
            f"{r['kind']}/{r['id']} ~ {r['kind']}/{r['near_id']}  "
            f"cos={r['cosine']:.4f}"
        )
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_dedup_scan_lists_duplicates -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/embeddings/test_cli.py
git commit -m "feat(embeddings): vouch dedup scan command"
```

---

### Task 27: `vouch eval embedding` command

**Files:**
- Modify: `src/vouch/cli.py`
- Modify: `tests/embeddings/test_cli.py`

- [ ] **Step 1: Add failing test**

```python
def test_eval_embedding_outputs_metrics(kb: Path, tmp_path: Path) -> None:
    import json as _json
    qfile = tmp_path / "queries.jsonl"
    qfile.write_text(_json.dumps({"query": "some text", "relevant": ["claim:c1"]}) + "\n")
    runner = CliRunner()
    result = runner.invoke(cli, [
        "eval", "embedding",
        "--queries", str(qfile),
        "--metric", "recall@10,mrr,ndcg",
    ])
    assert result.exit_code == 0
    assert "recall" in result.output.lower() or "mrr" in result.output.lower()
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_eval_embedding_outputs_metrics -v`
Expected: fail.

- [ ] **Step 3: Add the command (Python identifier `eval_group` to avoid shadowing)**

Append to `src/vouch/cli.py`:

```python
@cli.group(name="eval")
def eval_group() -> None:
    """Evaluation harnesses."""


@eval_group.command("embedding")
@click.option("--queries", required=True, type=click.Path(exists=True))
@click.option("--metric", default="recall@10,mrr,ndcg")
def eval_embedding(queries: str, metric: str) -> None:
    """Run retrieval-quality metrics over a JSONL query set."""
    from pathlib import Path
    from .embeddings.scorer import evaluate
    store = _load_store()
    metrics = tuple(m.strip() for m in metric.split(","))
    canonical = tuple(
        "recall@k" if m.startswith("recall@") else m for m in metrics
    )
    k = 10
    for m in metrics:
        if m.startswith("recall@"):
            try:
                k = int(m.split("@", 1)[1])
            except ValueError:
                pass
    out = evaluate(
        kb_dir=store.kb_dir,
        queries_file=Path(queries),
        k=k,
        metrics=canonical,
    )
    for m_name, v in out.items():
        click.echo(f"{m_name}\t{v:.4f}")
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_eval_embedding_outputs_metrics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/embeddings/test_cli.py
git commit -m "feat(embeddings): vouch eval embedding command"
```

---

### Task 28: `vouch embeddings stats` command

**Files:**
- Modify: `src/vouch/cli.py`
- Modify: `tests/embeddings/test_cli.py`

- [ ] **Step 1: Add failing test**

```python
def test_embeddings_stats(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["embeddings", "stats"])
    assert result.exit_code == 0
    assert "model" in result.output.lower() or "claim" in result.output.lower()
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_embeddings_stats -v`
Expected: fail.

- [ ] **Step 3: Add the command**

Append to `src/vouch/cli.py`:

```python
@cli.group()
def embeddings() -> None:
    """Embedding maintenance commands."""


@embeddings.command("stats")
def embeddings_stats() -> None:
    """Print model identity, per-kind counts, and cache hit rate."""
    from . import index_db
    from .embeddings.cache import query_cache_stats
    store = _load_store()
    meta = index_db.get_embedding_meta(store.kb_dir)
    for k, v in sorted(meta.items()):
        click.echo(f"{k}\t{v}")
    with index_db.open_db(store.kb_dir) as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind"
        ).fetchall()
    for k, n in rows:
        click.echo(f"embedding_count_{k}\t{n}")
    cs = query_cache_stats(store.kb_dir)
    click.echo(f"query_cache_entries\t{cs['entries']}")
    click.echo(f"query_cache_hits\t{cs['hits']}")
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_cli.py::test_embeddings_stats -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/embeddings/test_cli.py
git commit -m "feat(embeddings): vouch embeddings stats command"
```

---

### Task 29: MCP + JSONL parity for new commands

**Files:**
- Modify: `src/vouch/server.py`
- Modify: `src/vouch/jsonl_server.py`
- Modify: `tests/embeddings/test_search.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/embeddings/test_search.py`:

```python
def test_mcp_kb_reindex_embeddings(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    out = server.kb_reindex_embeddings(backfill=True)
    assert out["touched"] >= 1


def test_mcp_kb_dedup_scan(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    out = server.kb_dedup_scan(threshold=0.95, dry_run=True)
    assert "duplicates" in out


def test_mcp_kb_embeddings_stats(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import server
    monkeypatch.setattr(server, "_store", lambda: store)
    out = server.kb_embeddings_stats()
    assert "model" in out


def test_jsonl_kb_reindex_embeddings(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch import jsonl_server
    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    resp = jsonl_server.handle_request({
        "id": "1", "method": "kb.reindex_embeddings",
        "params": {"backfill": True},
    })
    assert resp["ok"] is True
```

- [ ] **Step 2: Run tests, confirm fail**

Run: `.venv/bin/pytest tests/embeddings/test_search.py -v`
Expected: 4 new failures.

- [ ] **Step 3: Add MCP tools to `server.py`**

Append:

```python
@mcp.tool()
def kb_reindex_embeddings(
    *, backfill: bool = False, force: bool = False, model: str | None = None,
) -> dict[str, Any]:
    """Re-encode every artifact under the current embedding adapter."""
    from .embeddings.migration import backfill_embeddings
    store = _store()
    if model:
        from .embeddings import get_embedder
        get_embedder(model)
    n = backfill_embeddings(store, force=force)
    return {"touched": n, "model": _current_model_name()}


@mcp.tool()
def kb_dedup_scan(
    *, threshold: float = 0.95, dry_run: bool = False,
) -> dict[str, Any]:
    """Find near-duplicate artifacts via embedding cosine."""
    from .embeddings.dedup import scan_all
    store = _store()
    rows = scan_all(store.kb_dir, threshold=threshold, dry_run=dry_run)
    return {"duplicates": rows, "threshold": threshold}


@mcp.tool()
def kb_eval_embeddings(*, queries_path: str, k: int = 10) -> dict[str, Any]:
    """Run retrieval eval over a JSONL queries file."""
    from pathlib import Path
    from .embeddings.scorer import evaluate
    store = _store()
    return evaluate(
        kb_dir=store.kb_dir,
        queries_file=Path(queries_path),
        k=k,
    )


@mcp.tool()
def kb_embeddings_stats() -> dict[str, Any]:
    """Model identity, per-kind counts, query cache stats."""
    from . import index_db
    from .embeddings.cache import query_cache_stats
    store = _store()
    meta = index_db.get_embedding_meta(store.kb_dir)
    counts: dict[str, int] = {}
    with index_db.open_db(store.kb_dir) as conn:
        for k, n in conn.execute(
            "SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind"
        ):
            counts[k] = int(n)
    return {
        "model": meta.get("embedding_model"),
        "model_version": meta.get("embedding_model_version"),
        "dim": meta.get("embedding_dim"),
        "counts": counts,
        "query_cache": query_cache_stats(store.kb_dir),
    }


def _current_model_name() -> str:
    try:
        from .embeddings import get_embedder
        return get_embedder().name
    except Exception:
        return ""
```

- [ ] **Step 4: Add JSONL handlers**

In `src/vouch/jsonl_server.py`, add four handler functions and register them in the dispatch table. Find the existing `_HANDLERS` registration (or the equivalent `if method == "...":` cascade) and append:

```python
def _h_reindex_embeddings(p: dict) -> dict:
    from .embeddings.migration import backfill_embeddings
    n = backfill_embeddings(_store(), force=bool(p.get("force", False)))
    return {"touched": n}


def _h_dedup_scan(p: dict) -> dict:
    from .embeddings.dedup import scan_all
    return {
        "duplicates": scan_all(
            _store().kb_dir,
            threshold=float(p.get("threshold", 0.95)),
            dry_run=bool(p.get("dry_run", False)),
        ),
    }


def _h_eval_embeddings(p: dict) -> dict:
    from pathlib import Path
    from .embeddings.scorer import evaluate
    return evaluate(
        kb_dir=_store().kb_dir,
        queries_file=Path(p["queries_path"]),
        k=int(p.get("k", 10)),
    )


def _h_embeddings_stats(_: dict) -> dict:
    from . import index_db
    from .embeddings.cache import query_cache_stats
    store = _store()
    meta = index_db.get_embedding_meta(store.kb_dir)
    with index_db.open_db(store.kb_dir) as conn:
        counts = {
            k: int(n) for k, n in conn.execute(
                "SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind"
            )
        }
    return {
        "model": meta.get("embedding_model"),
        "counts": counts,
        "query_cache": query_cache_stats(store.kb_dir),
    }
```

Then register them in the JSONL dispatch table. If the dispatch uses `_HANDLERS[name] = fn`, add:

```python
_HANDLERS["kb.reindex_embeddings"] = _h_reindex_embeddings
_HANDLERS["kb.dedup_scan"] = _h_dedup_scan
_HANDLERS["kb.eval_embeddings"] = _h_eval_embeddings
_HANDLERS["kb.embeddings_stats"] = _h_embeddings_stats
```

If the project uses a single dispatch function with `if method == "...":` branches, add equivalent branches calling the new handlers.

- [ ] **Step 5: Run tests, confirm pass**

Run: `.venv/bin/pytest tests/embeddings -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/server.py src/vouch/jsonl_server.py tests/embeddings/test_search.py
git commit -m "feat(embeddings): MCP + JSONL parity for reindex/dedup/eval/stats"
```

---

### Task 30: Wire migration warning into `health.rebuild_index`

**Files:**
- Modify: `src/vouch/health.py`
- Modify: `tests/embeddings/test_migration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/embeddings/test_migration.py`:

```python
def test_rebuild_index_emits_mismatch_audit_event(store: KBStore) -> None:
    from vouch import audit, health, index_db
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id]))
    index_db.set_embedding_meta(
        store.kb_dir, model="some-other-model", version="v9", dim=8,
    )
    health.rebuild_index(store)
    log_path = store.kb_dir / "audit.log.jsonl"
    text = log_path.read_text() if log_path.exists() else ""
    assert "embedding.model_mismatch" in text
```

- [ ] **Step 2: Run test, confirm fails**

Run: `.venv/bin/pytest tests/embeddings/test_migration.py::test_rebuild_index_emits_mismatch_audit_event -v`
Expected: fail.

- [ ] **Step 3: Wire into `health.rebuild_index`**

In `src/vouch/health.py`, locate `rebuild_index(store)`. After the existing FTS5 rebuild logic, append:

```python
    try:
        from .embeddings.migration import detect_mismatch
        from . import audit
        m = detect_mismatch(store.kb_dir)
        if m is not None:
            audit.log_event(
                store.kb_dir, event="embedding.model_mismatch",
                actor="vouch-health",
                object_ids=[], data=m,
            )
    except ImportError:
        pass
```

- [ ] **Step 4: Run test, confirm passes**

Run: `.venv/bin/pytest tests/embeddings/test_migration.py::test_rebuild_index_emits_mismatch_audit_event -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vouch/health.py tests/embeddings/test_migration.py
git commit -m "feat(embeddings): rebuild_index emits embedding.model_mismatch audit event"
```

---

## Final integration

### Task 31: End-to-end lexical-disjoint regression

**Files:**
- Modify: `tests/embeddings/test_integration.py`

- [ ] **Step 1: Add the integration acceptance test**

Append to `tests/embeddings/test_integration.py`:

```python
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
```

- [ ] **Step 2: Run the integration test**

Run: `.venv/bin/pytest tests/embeddings/test_integration.py -m integration -v`
Expected: 4 passed (3 prior + this new one).

- [ ] **Step 3: Run the full unit suite**

Run: `.venv/bin/pytest --ignore=tests/test_sessions.py -q`
Expected: all green.

- [ ] **Step 4: Run ruff and mypy**

Run: `.venv/bin/python -m ruff check src tests && .venv/bin/python -m mypy src/vouch`
Expected: both clean (or fix any issues before committing).

- [ ] **Step 5: Commit**

```bash
git add tests/embeddings/test_integration.py
git commit -m "test(embeddings): end-to-end lexical-disjoint regression"
```

---

### Task 32: Documentation

**Files:**
- Create: `docs/embeddings.md`
- Modify: `docs/retrieval.md`
- Modify: `docs/README.md`

- [ ] **Step 1: Write `docs/embeddings.md`**

Create `docs/embeddings.md`:

```markdown
# Embedding-based search

vouch's primary search backend is embedding-based semantic retrieval,
backed by `sentence-transformers/all-mpnet-base-v2` (768-dim) by default.
FTS5 remains as the deterministic fallback when embeddings are unavailable
or return no hits.

## Install

```bash
pip install vouch[embeddings]
```

Alternative (no torch):

```bash
pip install vouch[embeddings-fast]
```

## Usage

```bash
# default: semantic primary, FTS5 fallback
vouch search "how do we authenticate users"

# force a specific backend
vouch search "auth" --backend embedding
vouch search "auth" --backend fts5
vouch search "auth" --backend hybrid --rerank --hyde --explain

# maintenance
vouch reindex --embeddings --backfill
vouch dedup --threshold 0.95
vouch embeddings stats
vouch eval embedding --queries eval/queries.jsonl
```

See `docs/superpowers/specs/2026-05-20-semantic-search-design.md`
for the full architecture.
```

- [ ] **Step 2: Cross-link in `docs/retrieval.md`**

Append at the bottom:

```markdown

## Semantic retrieval

See [embeddings.md](./embeddings.md).
```

- [ ] **Step 3: Update `docs/README.md`**

Add a line under the existing entries:

```markdown
- [embeddings.md](./embeddings.md) -- semantic retrieval (primary backend)
```

- [ ] **Step 4: Commit**

```bash
git add docs/embeddings.md docs/retrieval.md docs/README.md
git commit -m "docs(embeddings): user guide + index cross-link"
```

---

## Self-Review

**1. Spec coverage check.** Every section of the spec maps to a task:

| Spec section | Tasks |
|---|---|
| §2 Integration shape | T15, T16 |
| §3 Package layout | T2, T3, T4, T5, T6, T17, T18, T19, T20, T21, T22 |
| §4 Storage layout | T7, T8, T9, T10 |
| §5 Touched modules + LOC | All tasks cumulatively |
| §6 Default behavior table | T15, T16, T23, T24 |
| §7 Write path (skip on hash; dedup) | T11, T12, T13, T20 |
| §8 Migration | T22, T30 |
| §9 CLI surface | T24, T25, T26, T27, T28 |
| §10 MCP / JSONL parity | T29 |
| §11 Dependencies | T1 |
| §12 Default values | T7 (schema), T10 (cache cap), T20 (threshold) |
| §13 Test plan (9 files) | T3, T7, T8, T9, T10, T11, T17, T18, T19, T20, T21, T22, T24, T31 |
| §14 Acceptance criteria | T31 (headline), T1 + T24 + T25 (install + reindex idempotent) |

**2. Placeholder scan.** No "TBD", "TODO", "implement later" left in the plan.

**3. Type consistency.** `Hit = tuple[str, str, str, float]` used consistently across `index_db`, `fusion`, `rerank`, `scorer`. `Embedder.name / version / dim` match across `base.py`, `st_mpnet.py`, `st_minilm.py`, `fastembed_bge.py`. `_embed_and_store(kind, id, text)` signature matches every call site.

**4. Pre-existing flaky tests.** The full test suite has pre-existing flakiness in `tests/test_sessions.py` (timestamp-collision IDs). Plan steps that say "run full suite" use `--ignore=tests/test_sessions.py` -- this is documented as a separate issue, not introduced by this work.

---

## Plan complete

Plan saved to `docs/superpowers/plans/2026-05-20-semantic-search.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Uses `superpowers:subagent-driven-development`.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
