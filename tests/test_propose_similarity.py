"""Propose-time similarity warnings."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME
from vouch.models import Claim
from vouch.proposals import propose_claim
from vouch.storage import KBStore

_SIMILAR_TEXT = "Auth uses JWTs in the Authorization header."


@pytest.fixture(autouse=True)
def _mock_embedder() -> None:
    # MockEmbedder requires numpy. Skip cleanly when CI installs only [dev].
    pytest.importorskip("numpy")
    from tests.embeddings._fakes import MockEmbedder

    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_propose_warns_similar_approved_claim(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(
        Claim(id="auth-jwt", text=_SIMILAR_TEXT, evidence=[src.id]),
    )
    result = propose_claim(
        store,
        text=_SIMILAR_TEXT,
        evidence=[src.id],
        proposed_by="agent",
    )
    codes = {w["code"] for w in result.warnings}
    assert "similar_approved" in codes
    ids = {w["artifact_id"] for w in result.warnings if w["code"] == "similar_approved"}
    assert "auth-jwt" in ids


def test_propose_warns_similar_pending_proposal(store: KBStore) -> None:
    src = store.put_source(b"e")
    first = propose_claim(
        store,
        text=_SIMILAR_TEXT,
        evidence=[src.id],
        proposed_by="agent-a",
    )
    assert not first.warnings

    second = propose_claim(
        store,
        text=_SIMILAR_TEXT,
        evidence=[src.id],
        proposed_by="agent-b",
    )
    pending = [w for w in second.warnings if w["code"] == "similar_pending"]
    assert pending
    assert pending[0]["artifact_id"] == first.id


def test_propose_no_warning_for_unrelated_text(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="apples and oranges", evidence=[src.id]))
    result = propose_claim(
        store, text="zebras run fast in the savanna", evidence=[src.id], proposed_by="agent",
    )
    assert result.warnings == []


def test_propose_similarity_on_dry_run(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text=_SIMILAR_TEXT, evidence=[src.id]))
    result = propose_claim(
        store, text=_SIMILAR_TEXT, evidence=[src.id],
        proposed_by="agent", dry_run=True,
    )
    assert not (store.kb_dir / "proposed" / f"{result.id}.yaml").exists()
    assert any(w["code"] == "similar_approved" for w in result.warnings)


def test_jsonl_propose_claim_includes_warnings(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch.jsonl_server import handle_request

    src = store.put_source(b"e")
    store.put_claim(
        Claim(id="c1", text=_SIMILAR_TEXT, evidence=[src.id]),
    )
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "1",
        "method": "kb.propose_claim",
        "params": {
            "text": _SIMILAR_TEXT,
            "evidence": [src.id],
        },
    })
    assert resp["ok"]
    assert "warnings" in resp["result"]
    assert any(w["code"] == "similar_approved" for w in resp["result"]["warnings"])


def test_propose_similarity_without_embedder(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text=_SIMILAR_TEXT, evidence=[src.id]))

    def _no_embedder(name: str | None = None) -> None:
        raise KeyError("no embedder")

    monkeypatch.setattr("vouch.embeddings.get_embedder", _no_embedder)

    result = propose_claim(
        store, text=_SIMILAR_TEXT, evidence=[src.id], proposed_by="agent",
    )
    assert result.warnings == []
