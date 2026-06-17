"""Proposal write latency benchmarks."""
from __future__ import annotations

import pytest

from vouch.proposals import propose_claim
from vouch.storage import KBStore


@pytest.fixture
def fresh_store(tmp_path):
    return KBStore.init(tmp_path)


@pytest.fixture
def source_id(fresh_store):
    src = fresh_store.put_source(b"benchmark evidence")
    return src.id


def test_propose_claim_latency(benchmark, fresh_store, source_id):
    """propose_claim() write latency — sits in the agent hot loop."""
    counter = [0]

    def _propose():
        counter[0] += 1
        return propose_claim(
            fresh_store,
            text=f"benchmark claim {counter[0]}",
            evidence=[source_id],
            proposed_by="bench-agent",
        )

    result = benchmark(_propose)
    assert result is not None
