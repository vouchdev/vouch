"""Search latency benchmarks at varying KB sizes."""
from __future__ import annotations

from pathlib import Path

import pytest

from vouch import index_db
from vouch.health import rebuild_index
from vouch.storage import KBStore


def _store(kb_path: Path) -> KBStore:
    store = KBStore(kb_path)
    rebuild_index(store)
    return store


@pytest.fixture(scope="module")
def store_1k(kb_1k):
    return _store(kb_1k)


@pytest.fixture(scope="module")
def store_10k(kb_10k):
    return _store(kb_10k)


def test_search_fts5_1k(benchmark, store_1k):
    """FTS5 search latency on a 1k-claim KB."""
    benchmark(index_db.search, store_1k.kb_dir, "auth uses JWT", limit=10)


def test_search_fts5_10k(benchmark, store_10k):
    """FTS5 search latency on a 10k-claim KB."""
    benchmark(index_db.search, store_10k.kb_dir, "auth uses JWT", limit=10)


def test_search_substring_1k(benchmark, store_1k):
    """Substring fallback search latency on a 1k-claim KB."""
    benchmark(store_1k.search_substring, "auth", limit=10)


def test_search_substring_10k(benchmark, store_10k):
    """Substring fallback search latency on a 10k-claim KB."""
    benchmark(store_10k.search_substring, "auth", limit=10)
