"""Lessons: a typed artifact with follow-through tracking (issue #428).

Covers the achievable slice of #428: ClaimType.LESSON resurfacing through the
normal retrieval path, kb.mark_lesson_followed as an append-only observation
(never edits the claim), and the repeat guard at propose time -- which is
free, since propose_claim already runs find_similar_on_propose (#147) for
every claim regardless of type.

Not covered here: the "effectiveness computation" the issue names as a
downstream consumer of these events does not exist anywhere in this codebase
(its own cross-reference points at an unrelated PR) -- these events are
shaped for such a consumer, but building that unspecified system is out of
scope for this issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import audit, capabilities, context
from vouch.context import build_context_pack
from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME
from vouch.jsonl_server import handle_request
from vouch.lifecycle import mark_lesson_followed
from vouch.models import Claim, ClaimType, ProposalStatus
from vouch.proposals import propose_claim
from vouch.server import kb_mark_lesson_followed
from vouch.storage import ArtifactNotFoundError, KBStore

_LESSON_TEXT = "Always run mypy before pushing."


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _lesson(store: KBStore, cid: str = "lesson-mypy", text: str = _LESSON_TEXT) -> Claim:
    src = store.put_source(b"e")
    return store.put_claim(Claim(id=cid, text=text, type=ClaimType.LESSON, evidence=[src.id]))


# ---------------------------------------------------------------------------
# Claim type
# ---------------------------------------------------------------------------


def test_lesson_is_a_claim_type() -> None:
    assert ClaimType.LESSON.value == "lesson"


def test_lesson_claim_round_trips_through_store(store: KBStore) -> None:
    _lesson(store)
    loaded = store.get_claim("lesson-mypy")
    assert loaded.type == ClaimType.LESSON


# ---------------------------------------------------------------------------
# Resurfacing via retrieval
# ---------------------------------------------------------------------------


def test_lesson_resurfaces_via_context_pack(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lesson claim must resurface through the normal retrieval path --
    no special-casing anywhere excludes ClaimType.LESSON."""
    _lesson(store)
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [("claim", "lesson-mypy", _LESSON_TEXT, 0.9)],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    pack = build_context_pack(store, query="mypy before pushing")
    ids = {item["id"] for item in pack["items"]}
    assert "lesson-mypy" in ids


# ---------------------------------------------------------------------------
# mark_lesson_followed: observe, don't edit
# ---------------------------------------------------------------------------


def test_mark_followed_appends_audit_event(store: KBStore) -> None:
    _lesson(store)
    result = mark_lesson_followed(store, claim_id="lesson-mypy", followed=True, actor="agent")
    assert result == {"id": "lesson-mypy", "followed": True}
    events = [e for e in audit.read_events(store.kb_dir) if e.event == "lesson.followed"]
    assert len(events) == 1
    assert events[0].object_ids == ["lesson-mypy"]
    assert events[0].data["followed"] is True


def test_mark_not_followed_records_false_with_context(store: KBStore) -> None:
    _lesson(store)
    mark_lesson_followed(
        store,
        claim_id="lesson-mypy",
        followed=False,
        actor="agent",
        context="skipped due to deadline",
    )
    events = [e for e in audit.read_events(store.kb_dir) if e.event == "lesson.followed"]
    assert events[0].data["followed"] is False
    assert events[0].data["context"] == "skipped due to deadline"


def test_mark_followed_does_not_edit_the_claim(store: KBStore) -> None:
    """Core invariant: observing usage must never mutate the lesson itself."""
    before = _lesson(store)
    mark_lesson_followed(store, claim_id="lesson-mypy", followed=True, actor="agent")
    after = store.get_claim("lesson-mypy")
    assert after.text == before.text
    assert after.status == before.status
    assert after.updated_at == before.updated_at
    assert after.confidence == before.confidence


def test_mark_followed_multiple_times_appends_each_observation(store: KBStore) -> None:
    """Repeated observations each land as their own event -- a log of usage
    over time, not a single mutable field."""
    _lesson(store)
    mark_lesson_followed(store, claim_id="lesson-mypy", followed=True, actor="agent-a")
    mark_lesson_followed(store, claim_id="lesson-mypy", followed=False, actor="agent-b")
    events = [e for e in audit.read_events(store.kb_dir) if e.event == "lesson.followed"]
    assert len(events) == 2
    assert [e.data["followed"] for e in events] == [True, False]


def test_mark_followed_missing_claim_raises(store: KBStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        mark_lesson_followed(store, claim_id="ghost", followed=True, actor="agent")


def test_mark_followed_not_restricted_to_lesson_type(store: KBStore) -> None:
    """The issue's own proposed surface allows 'lesson' as a flag on an
    existing WORKFLOW/WARNING claim, not only a dedicated type -- any claim
    id may carry a follow-through observation."""
    src = store.put_source(b"e")
    store.put_claim(
        Claim(id="wf1", text="a workflow rule", type=ClaimType.WORKFLOW, evidence=[src.id])
    )
    mark_lesson_followed(store, claim_id="wf1", followed=True, actor="agent")
    events = [e for e in audit.read_events(store.kb_dir) if e.event == "lesson.followed"]
    assert events[0].data["claim_type"] == "workflow"


# ---------------------------------------------------------------------------
# Registration across surfaces (capabilities / JSONL / MCP)
# ---------------------------------------------------------------------------


def test_method_registered_in_capabilities() -> None:
    assert "kb.mark_lesson_followed" in capabilities.METHODS


def test_jsonl_mark_lesson_followed_end_to_end(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _lesson(store)
    resp = handle_request(
        {
            "id": "r1",
            "method": "kb.mark_lesson_followed",
            "params": {"claim_id": "lesson-mypy", "followed": True},
        }
    )
    assert resp["ok"] is True, resp
    assert resp["result"]["id"] == "lesson-mypy"
    assert resp["result"]["followed"] is True


def test_mcp_mark_lesson_followed(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _lesson(store)
    result = kb_mark_lesson_followed(
        claim_id="lesson-mypy",
        followed=False,
        context="ran out of time",
    )
    assert result == {"id": "lesson-mypy", "followed": False}
    events = [e for e in audit.read_events(store.kb_dir) if e.event == "lesson.followed"]
    assert events[0].data["context"] == "ran out of time"


# ---------------------------------------------------------------------------
# Repeat guard: reuses existing propose-time similarity warnings (#147)
# ---------------------------------------------------------------------------


def test_proposing_overlapping_lesson_warns_without_blocking(store: KBStore) -> None:
    """propose_claim already runs find_similar_on_propose for every claim
    type -- adding ClaimType.LESSON gets the repeat guard for free.

    _REGISTRY is module-global (vouch.embeddings.base); registering a
    MockEmbedder here without restoring it afterward leaks into unrelated
    later tests elsewhere in the suite (e.g. test_cli's fts5-backend-label
    test), flipping their expected backend from fts5 to embedding. Mirrors
    tests/embeddings/conftest.py's _isolate_embedder_registry, which only
    covers that directory, not this one.
    """
    pytest.importorskip("numpy")
    from tests.embeddings._fakes import MockEmbedder
    from vouch.embeddings import base as embeddings_base

    saved_registry = dict(embeddings_base._REGISTRY)
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))
    try:
        _assert_overlapping_lesson_warns(store)
    finally:
        embeddings_base._REGISTRY.clear()
        embeddings_base._REGISTRY.update(saved_registry)


def _assert_overlapping_lesson_warns(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(
        Claim(id="lesson-mypy", text=_LESSON_TEXT, type=ClaimType.LESSON, evidence=[src.id])
    )
    result = propose_claim(
        store,
        text=_LESSON_TEXT,
        evidence=[src.id],
        proposed_by="agent",
        claim_type="lesson",
    )
    codes = {w["code"] for w in result.warnings}
    assert "similar_approved" in codes
    # non-blocking: the proposal was still filed, not rejected
    assert result.proposal.status is ProposalStatus.PENDING
