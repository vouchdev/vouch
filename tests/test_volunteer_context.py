"""Confidence-gated push context — kb.volunteer_context (#236)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from vouch import health, volunteer_context
from vouch import sessions as sess_mod
from vouch.hot_memory import get as hot_memory_get
from vouch.jsonl_server import handle_request
from vouch.models import ArtifactScope, Claim, Visibility
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    s.config_path.write_text(
        "volunteer:\n  poll_interval_seconds: 999\n  throttle_seconds: 0\n"
    )
    return s


def test_jwt_claim_volunteered_on_session_start(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"jwt spec")
    store.put_claim(Claim(
        id="auth-uses-jwt",
        text="Authentication uses JWT bearer tokens for API access",
        evidence=[src.id],
    ))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    sess = sess_mod.session_start(store, agent="test-agent", task="implement jwt auth")
    pending = volunteer_context.drain_pending(sess.id)

    assert len(pending) == 1
    offer = pending[0]
    assert offer.claim_id == "auth-uses-jwt"
    assert offer.relevance >= volunteer_context.DEFAULT_THRESHOLD
    assert "jwt" in offer.why.lower()

    assert volunteer_context.drain_pending(sess.id) == []

    sess_mod.session_end(store, sess.id)


def test_pending_proposal_not_volunteered(store: KBStore, monkeypatch) -> None:
    from vouch.proposals import propose_claim

    src = store.put_source(b"e")
    sess = sess_mod.session_start(store, agent="a", task="jwt")
    propose_claim(
        store,
        text="JWT draft — not yet approved",
        evidence=[src.id],
        proposed_by="a",
        session_id=sess.id,
    )
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    offer = volunteer_context.evaluate_now(store, sess.id)
    assert offer is None
    sess_mod.session_end(store, sess.id)


def test_private_claim_respects_scope(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="secret-jwt",
        text="JWT signing key rotation policy",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PRIVATE, agent="alice"),
    ))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)
    monkeypatch.setenv("VOUCH_AGENT", "bob")

    sess = sess_mod.session_start(store, agent="bob", task="jwt rotation")
    offer = volunteer_context.evaluate_now(store, sess.id)
    assert offer is None
    sess_mod.session_end(store, sess.id)


def test_throttle_blocks_second_push(store: KBStore, monkeypatch) -> None:
    from vouch import hot_memory
    from vouch.models import Session

    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="auth-uses-jwt",
        text="JWT tokens",
        evidence=[src.id],
    ))
    store.put_claim(Claim(
        id="jwt-refresh",
        text="JWT refresh rotation",
        evidence=[src.id],
    ))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    cfg = volunteer_context.VolunteerConfig(throttle_seconds=60.0)
    sess = Session(id="sess-throttle-test", agent="a", task="jwt")
    store.put_session(sess)
    hot_memory.register(session_id=sess.id, query="jwt", agent="a")

    first = volunteer_context.evaluate_session(store, sess, config=cfg)
    assert first is not None
    volunteer_context.enqueue_offer(first)

    second = volunteer_context.evaluate_session(store, sess, config=cfg)
    assert second is None


def test_jsonl_volunteer_context_poll(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="auth-uses-jwt",
        text="JWT bearer auth",
        evidence=[src.id],
    ))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    start = handle_request({
        "id": "1",
        "method": "kb.session_start",
        "params": {"task": "jwt"},
    })
    assert start["ok"]
    session_id = start["result"]["id"]
    volunteer_context.evaluate_now(store, session_id)

    poll = handle_request({
        "id": "2",
        "method": "kb.volunteer_context",
        "params": {"session_id": session_id},
    })
    assert poll["ok"]
    assert len(poll["result"]["volunteers"]) == 1
    assert poll["result"]["volunteers"][0]["claim_id"] == "auth-uses-jwt"

    empty = handle_request({
        "id": "3",
        "method": "kb.volunteer_context",
        "params": {"session_id": session_id},
    })
    assert empty["ok"]
    assert empty["result"]["volunteers"] == []

    handle_request({"id": "4", "method": "kb.session_end", "params": {"session_id": session_id}})


def test_session_without_task_skips_watch(store: KBStore) -> None:
    sess = sess_mod.session_start(store, agent="a")
    assert hot_memory_get(sess.id) is None
    sess_mod.session_end(store, sess.id)


def test_watch_delivers_within_five_seconds(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="auth-uses-jwt",
        text="JWT authentication",
        evidence=[src.id],
    ))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    store.config_path.write_text(
        "volunteer:\n  poll_interval_seconds: 1\n  throttle_seconds: 0\n"
    )

    sess = sess_mod.session_start(store, agent="a", task="jwt")
    deadline = time.monotonic() + 5.0
    got: list[volunteer_context.VolunteerOffer] = []
    while time.monotonic() < deadline:
        got = volunteer_context.drain_pending(sess.id, clear=False)
        if got:
            break
        time.sleep(0.2)

    assert got, "expected volunteered claim within 5 seconds"
    assert got[0].claim_id == "auth-uses-jwt"
    sess_mod.session_end(store, sess.id)
