"""kb.experts - rank entities by evidence density on a topic (issue #315).

Read-only: aggregates approved, live claims and returns a ranking. These tests
seed entities + claims directly and match on the topic via the entity
name/alias substring pass, so they exercise the ranking without depending on
the FTS index being populated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.experts import rank_experts
from vouch.jsonl_server import handle_request
from vouch.models import Claim, ClaimStatus, Entity, EntityType
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _seed(store: KBStore) -> None:
    src = store.put_source(b"evidence-bytes")
    src2 = store.put_source(b"other-evidence")
    store.put_entity(Entity(id="jwt", name="JWT", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="alice", name="alice", type=EntityType.PERSON))
    store.put_entity(Entity(id="bob", name="bob", type=EntityType.PERSON))
    # alice: 2 JWT claims (one citing two distinct sources); bob: 1 JWT claim.
    store.put_claim(
        Claim(id="c1", text="jwt auth by alice", evidence=[src.id], entities=["jwt", "alice"])
    )
    store.put_claim(
        Claim(
            id="c2",
            text="jwt rotation by alice",
            evidence=[src.id, src2.id],
            entities=["jwt", "alice"],
        )
    )
    store.put_claim(
        Claim(id="c3", text="jwt review by bob", evidence=[src.id], entities=["jwt", "bob"])
    )


def test_ranks_by_claim_count(store: KBStore) -> None:
    _seed(store)
    rows = rank_experts(store, "JWT", weight="count")
    names = [r["name"] for r in rows]
    assert names[0] == "JWT"  # on all 3 claims
    assert names.index("alice") < names.index("bob")  # 2 claims vs 1
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["claim_count"] == 2


def test_min_claims_and_limit(store: KBStore) -> None:
    _seed(store)
    names = {r["name"] for r in rank_experts(store, "JWT", min_claims=2)}
    assert "bob" not in names  # bob has only 1 claim
    assert rank_experts(store, "JWT", limit=1)[0]["name"] == "JWT"


def test_citation_weight_rewards_source_breadth(store: KBStore) -> None:
    _seed(store)
    rows = rank_experts(store, "JWT", weight="citation")
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["citation_count"] == 2  # c2 cites two distinct sources


def test_excludes_superseded_archived_redacted(store: KBStore) -> None:
    src = store.put_source(b"x")
    store.put_entity(Entity(id="e", name="ghost", type=EntityType.CONCEPT))
    store.put_claim(
        Claim(
            id="live",
            text="ghost live",
            evidence=[src.id],
            entities=["e"],
            status=ClaimStatus.STABLE,
        )
    )
    for i, dead in enumerate(
        (ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED)
    ):
        store.put_claim(
            Claim(
                id=f"dead{i}",
                text="ghost dead",
                evidence=[src.id],
                entities=["e"],
                status=dead,
            )
        )
    row = next(r for r in rank_experts(store, "ghost") if r["name"] == "ghost")
    assert row["claim_count"] == 1  # only the live claim scored


def test_unknown_weight_falls_back_to_count(store: KBStore) -> None:
    _seed(store)
    fallback = [r["entity_id"] for r in rank_experts(store, "JWT", weight="nonsense")]
    baseline = [r["entity_id"] for r in rank_experts(store, "JWT", weight="count")]
    assert fallback == baseline


def test_empty_kb_and_no_match(store: KBStore) -> None:
    assert rank_experts(store, "anything") == []
    _seed(store)
    assert rank_experts(store, "no-such-topic-xyz") == []


def test_deterministic_tie_break_on_entity_id(store: KBStore) -> None:
    src = store.put_source(b"y")
    store.put_entity(Entity(id="t", name="topic-x", type=EntityType.CONCEPT))
    store.put_entity(Entity(id="a2", name="a2", type=EntityType.PERSON))
    store.put_entity(Entity(id="a1", name="a1", type=EntityType.PERSON))
    store.put_claim(
        Claim(id="k1", text="topic-x one", evidence=[src.id], entities=["t", "a1"])
    )
    store.put_claim(
        Claim(id="k2", text="topic-x two", evidence=[src.id], entities=["t", "a2"])
    )
    ranked = rank_experts(store, "topic-x")
    tied = [r["entity_id"] for r in ranked if r["entity_id"] in {"a1", "a2"}]
    assert tied == ["a1", "a2"]  # equal score -> ascending entity_id


def test_jsonl_experts_envelope_success(store: KBStore, monkeypatch) -> None:
    # kb.experts over the JSONL contract: a well-formed request returns the
    # {id, ok, result} envelope with the ranking under result["experts"].
    _seed(store)
    monkeypatch.chdir(store.root)
    resp = handle_request(
        {"id": "e1", "method": "kb.experts", "params": {"topic": "JWT"}}
    )
    assert resp["id"] == "e1"
    assert resp["ok"] is True
    names = [r["name"] for r in resp["result"]["experts"]]
    assert "alice" in names


def test_jsonl_experts_envelope_missing_topic_errors(store: KBStore, monkeypatch) -> None:
    # A request missing the required `topic` param yields the failure envelope
    # {id, ok: false, error} rather than raising out of the server.
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "e2", "method": "kb.experts", "params": {}})
    assert resp["id"] == "e2"
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_param"
