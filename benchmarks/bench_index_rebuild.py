"""Index rebuild latency benchmarks at varying KB sizes."""
from __future__ import annotations

import pytest

from vouch.health import rebuild_index
from vouch.storage import KBStore


def test_index_rebuild_1k(benchmark, kb_1k):
    """Index rebuild time at 1k claims."""
    store = KBStore(kb_1k)
    benchmark(rebuild_index, store)


def test_index_rebuild_10k(benchmark, kb_10k):
    """Index rebuild time at 10k claims."""
    store = KBStore(kb_10k)
    benchmark(rebuild_index, store)
