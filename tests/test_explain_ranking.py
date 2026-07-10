"""kb.explain_ranking — read-time introspection over the retrieval pipeline.

Covers the acceptance criteria for #432: a fused-only query, a gate-dropped
candidate, viewer-scoping parity with kb.context, and the four-site
registration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import context, health
from vouch.models import ArtifactScope, Claim, Visibility
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _put(store: KBStore, cid: str, text: str, *, evidence: list[str] | None = None,
         scope: ArtifactScope | None = None) -> None:
    store.put_claim(Claim(
        id=cid, text=text,
        evidence=evidence if evidence is not None else [],
        scope=scope or ArtifactScope(),
    ))


# --- fused-only query -----------------------------------------------------


def test_fused_only_query_reports_fusion_signals(store: KBStore) -> None:
    """With no embeddings indexed the RRF path still runs on the FTS list:
    fusion is reported active, lexical rank + rrf contribution are populated,
    the semantic rank is null, and the candidate is kept."""
    src = store.put_source(b"e")
    _put(store, "c1", "jwt tokens expire after one hour", evidence=[src.id])
    health.rebuild_index(store)

    result = context.explain_ranking(store, query="jwt", limit=5)

    assert result["query"] == "jwt"
    assert result["backend"] == "hybrid"
    assert result["stages"] == {"fusion": True, "rerank": False, "recency_frequency": False}
    assert result["viewer"] == {"project": None, "agent": None}

    c1 = next(c for c in result["candidates"] if c["id"] == "c1")
    assert c1["gate"] == "kept"
    assert c1["lexical_rank"] == 1
    assert c1["semantic_rank"] is None
    assert c1["rrf_score"] is not None
    assert c1["final_score"] == c1["rrf_score"]
    # #5 / #317 not wired into the context pipeline -> reported null.
    assert c1["rerank_delta"] is None
    assert c1["recency_factor"] is None
    assert c1["frequency_factor"] is None
    assert c1["salience_factor"] is None


def test_candidates_ordered_by_fused_rank(store: KBStore) -> None:
    src = store.put_source(b"e")
    for i in range(4):
        _put(store, f"c{i}", f"redis caching layer note {i}", evidence=[src.id])
    health.rebuild_index(store)

    result = context.explain_ranking(store, query="redis caching", limit=10)
    ranks = [c["fused_rank"] for c in result["candidates"]]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1


# --- gate outcomes --------------------------------------------------------


def test_status_filtered_gate_for_retracted_claim(store: KBStore) -> None:
    """An archived claim still matches the FTS index (its row survives with an
    updated status) so it surfaces as a candidate — gated status-filtered,
    exactly as build_context_pack would drop it."""
    from vouch import lifecycle

    src = store.put_source(b"e")
    _put(store, "keep", "mongodb sharding strategy", evidence=[src.id])
    _put(store, "gone", "mongodb replica set failover", evidence=[src.id])
    health.rebuild_index(store)
    lifecycle.archive(store, claim_id="gone", actor="reviewer")

    result = context.explain_ranking(store, query="mongodb", limit=10)
    gates = {c["id"]: c["gate"] for c in result["candidates"]}
    assert gates["gone"] == "status-filtered"
    assert gates["keep"] == "kept"


def test_budget_dropped_gate(store: KBStore) -> None:
    src = store.put_source(b"e")
    for i in range(10):
        _put(store, f"c{i}",
             f"budget claim {i} with plenty of padding text to exceed the cap",
             evidence=[src.id])
    health.rebuild_index(store)

    result = context.explain_ranking(store, query="budget", limit=10, max_chars=100)
    gates = [c["gate"] for c in result["candidates"]]
    assert "budget-dropped" in gates
    assert "kept" in gates
    # The tail is dropped: every budget-dropped candidate ranks below the last
    # kept one.
    kept_ranks = [c["fused_rank"] for c in result["candidates"] if c["gate"] == "kept"]
    dropped_ranks = [c["fused_rank"] for c in result["candidates"]
                     if c["gate"] == "budget-dropped"]
    assert max(kept_ranks) < min(dropped_ranks)


def test_require_citations_keeps_cited_claims(store: KBStore) -> None:
    """A properly-cited claim is not gated when require_citations is set."""
    src = store.put_source(b"e")
    _put(store, "c1", "cited claim about caches", evidence=[src.id])
    health.rebuild_index(store)

    result = context.explain_ranking(store, query="caches", require_citations=True)
    c1 = next(c for c in result["candidates"] if c["id"] == "c1")
    assert c1["gate"] == "kept"


def test_uncited_gate_classification() -> None:
    """The uncited gate flags a citation-less claim when require_citations is
    set. The model blocks empty-evidence claims on every write path, so this
    otherwise-defensive branch (mirrored from build_context_pack) is exercised
    against a stand-in claim rather than a persisted one."""
    from typing import ClassVar

    from vouch.models import ClaimStatus

    class _FakeClaim:
        status = ClaimStatus.WORKING
        evidence: ClassVar[list[str]] = []

    class _FakeStore:
        def get_claim(self, cid: str) -> _FakeClaim:
            return _FakeClaim()

    def _candidate() -> list[dict]:
        return [{"kind": "claim", "id": "u1", "summary": "x", "gate": "kept"}]

    cands = _candidate()
    context._classify_gates(_FakeStore(), cands, max_chars=None, require_citations=True)
    assert cands[0]["gate"] == "uncited"

    cands = _candidate()
    context._classify_gates(_FakeStore(), cands, max_chars=None, require_citations=False)
    assert cands[0]["gate"] == "kept"


# --- viewer scoping parity ------------------------------------------------


def test_viewer_scoping_matches_context(store: KBStore) -> None:
    """A private claim invisible to the default viewer must not appear in the
    explain breakdown — same guarantee kb.context gives."""
    src = store.put_source(b"e")
    _put(store, "public1", "public postgres tuning note", evidence=[src.id])
    _put(store, "secret1", "private postgres credentials note", evidence=[src.id],
         scope=ArtifactScope(visibility=Visibility.PRIVATE, agent="alice"))
    health.rebuild_index(store)

    # Default viewer (no agent) cannot see the private claim.
    result = context.explain_ranking(store, query="postgres", limit=10)
    ids = {c["id"] for c in result["candidates"]}
    assert "public1" in ids
    assert "secret1" not in ids

    pack = context.build_context_pack(store, query="postgres", limit=10)
    assert {it["id"] for it in pack["items"]} == ids & {it["id"] for it in pack["items"]}
    assert "secret1" not in {it["id"] for it in pack["items"]}

    # The owning agent sees it.
    scoped = context.explain_ranking(store, query="postgres", limit=10, agent="alice")
    assert "secret1" in {c["id"] for c in scoped["candidates"]}
    assert scoped["viewer"]["agent"] == "alice"


def test_explain_ranking_is_read_only(store: KBStore) -> None:
    """Introspection must not file proposals or mutate the audit log."""
    from vouch import audit

    src = store.put_source(b"e")
    _put(store, "c1", "read only invariant claim", evidence=[src.id])
    health.rebuild_index(store)

    before_pending = len(store.list_proposals())
    before_audit = len(list(audit.read_events(store.kb_dir)))
    context.explain_ranking(store, query="invariant", limit=5)
    assert len(store.list_proposals()) == before_pending
    assert len(list(audit.read_events(store.kb_dir))) == before_audit


def test_empty_query_returns_no_candidates(store: KBStore) -> None:
    result = context.explain_ranking(store, query="nothingmatchesthis", limit=5)
    assert result["candidates"] == []
    assert "backend" in result


# --- four-site registration -----------------------------------------------


def test_registered_in_capabilities_and_handlers() -> None:
    from vouch import capabilities
    from vouch.jsonl_server import HANDLERS

    assert "kb.explain_ranking" in capabilities.METHODS
    assert "kb.explain_ranking" in HANDLERS


def test_registered_as_mcp_tool() -> None:
    from vouch.server import mcp

    assert "kb_explain_ranking" in mcp._tool_manager._tools


def test_jsonl_handler_round_trip(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch.jsonl_server import handle_request

    src = store.put_source(b"e")
    _put(store, "c1", "jsonl surface claim", evidence=[src.id])
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    resp = handle_request({
        "id": "r1", "method": "kb.explain_ranking",
        "params": {"query": "jsonl", "limit": 5},
    })
    assert resp["ok"]
    assert resp["result"]["backend"] == "hybrid"
    assert any(c["id"] == "c1" for c in resp["result"]["candidates"])
