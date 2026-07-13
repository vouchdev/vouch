"""Answer-mode synthesis — citation traceability and the gaps path."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vouch import capabilities, health, synthesize
from vouch.jsonl_server import HANDLERS, handle_request
from vouch.models import Claim, ClaimStatus, Page, PageStatus
from vouch.storage import KBStore

_CITE = re.compile(r"\[([^\[\]]+)\]")


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _auth_kb(store: KBStore) -> list[str]:
    src = store.put_source(b"auth design evidence")
    claims = [
        Claim(id="c-auth-1", text="auth uses short-lived JWT access tokens",
              evidence=[src.id], status=ClaimStatus.STABLE),
        Claim(id="c-auth-2", text="auth refresh tokens rotate on every use",
              evidence=[src.id], status=ClaimStatus.STABLE),
        Claim(id="c-auth-3", text="auth sessions expire after thirty minutes idle",
              evidence=[src.id], status=ClaimStatus.STABLE),
    ]
    for c in claims:
        store.put_claim(c)
    health.rebuild_index(store)
    return [c.id for c in claims]


def test_synthesize_cites_every_approved_claim(store: KBStore) -> None:
    ids = _auth_kb(store)
    result = synthesize.synthesize(store, query="auth", depth=5)
    assert result["answer"] != ""
    for cid in ids:
        assert f"[{cid}]" in result["answer"]
        assert cid in result["claims"]
    assert result["_meta"]["synthesis_confidence"] == "high"


def test_synthesize_uncovered_query_returns_empty_answer_and_gaps(
    store: KBStore,
) -> None:
    _auth_kb(store)
    result = synthesize.synthesize(store, query="kubernetes networking topology")
    assert result["answer"] == ""
    assert result["claims"] == []
    assert result["gaps"]
    assert "kubernetes" in result["gaps"]


def test_every_sentence_carries_a_resolvable_citation(store: KBStore) -> None:
    _auth_kb(store)
    result = synthesize.synthesize(store, query="auth tokens sessions", depth=5)
    assert result["answer"]
    claim_set = set(result["claims"])
    for sentence in result["answer"].split("]. "):
        ids = _CITE.findall(sentence + "]")
        assert ids, f"sentence without citation: {sentence!r}"
        assert all(i in claim_set for i in ids)
        for i in ids:
            assert store.get_claim(i).id == i


def test_max_chars_drops_trailing_claims_without_cutting_citations(
    store: KBStore,
) -> None:
    _auth_kb(store)
    result = synthesize.synthesize(store, query="auth", depth=5, max_chars=60)
    assert len(result["answer"]) <= 60
    for cid in result["claims"]:
        assert f"[{cid}]" in result["answer"]
    assert result["answer"].count("[") == len(result["claims"])


def test_confidence_reflects_claim_status(store: KBStore) -> None:
    src = store.put_source(b"mixed evidence")
    store.put_claim(Claim(id="m1", text="payments use stripe",
                          evidence=[src.id], status=ClaimStatus.WORKING))
    health.rebuild_index(store)
    result = synthesize.synthesize(store, query="payments stripe")
    assert "m1" in result["claims"]
    assert result["_meta"]["synthesis_confidence"] == "medium"

    src2 = store.put_source(b"contested evidence")
    store.put_claim(Claim(id="m2", text="billing rounds half-up",
                          evidence=[src2.id], status=ClaimStatus.CONTESTED))
    health.rebuild_index(store)
    contested = synthesize.synthesize(store, query="billing rounds")
    assert "m2" in contested["claims"]
    assert contested["_meta"]["synthesis_confidence"] == "low"


def test_llm_without_config_raises(store: KBStore) -> None:
    with pytest.raises(ValueError, match="llm synthesis is not configured"):
        synthesize.synthesize(store, query="auth", llm=True)


# --- the llm backend --------------------------------------------------------


def _wire_llm(store: KBStore, tmp_path: Path, payload: object) -> None:
    """Point compile.llm_cmd at a stub that emits `payload` as JSON."""
    out = tmp_path / "answer.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + f'\ncompile:\n  llm_cmd: "cat {out}"\n',
        encoding="utf-8",
    )


def _auth_page(store: KBStore) -> str:
    page = Page(
        id="auth-overview", title="Auth Overview",
        body="Access tokens are short-lived JWTs; refresh tokens rotate.",
        status=PageStatus.ACTIVE,
    )
    store.put_page(page)
    health.rebuild_index(store)
    return page.id


def test_llm_synthesize_cites_pages_and_strips_invented_ids(
    store: KBStore, tmp_path: Path,
) -> None:
    _auth_kb(store)
    pid = _auth_page(store)
    _wire_llm(store, tmp_path, {
        "answer": f"Auth uses short-lived JWTs [{pid}]. "
        "Refresh tokens rotate on every use [c-auth-2]. "
        "Billing rounds half-up [invented-id].",
        "gaps": ["billing"],
    })
    result = synthesize.synthesize(store, query="auth tokens", llm=True)
    assert result["pages"] == [pid]
    assert result["claims"] == ["c-auth-2"]
    assert "[invented-id]" not in result["answer"]
    assert "billing" in result["gaps"]
    assert result["_meta"]["synthesis_backend"] == "llm"
    assert result["_meta"]["dropped_citations"] == ["invented-id"]
    assert result["_meta"]["synthesis_confidence"] == "high"


def test_llm_uncited_draft_stays_silent(store: KBStore, tmp_path: Path) -> None:
    _auth_page(store)
    _wire_llm(store, tmp_path, {"answer": "Auth is fine, trust me.", "gaps": []})
    result = synthesize.synthesize(store, query="auth", llm=True)
    assert result["answer"] == ""
    assert result["claims"] == []
    assert result["pages"] == []
    assert result["gaps"]


def test_llm_unusable_output_raises(store: KBStore, tmp_path: Path) -> None:
    _auth_page(store)
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + '\ncompile:\n  llm_cmd: "echo not json"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        synthesize.synthesize(store, query="auth", llm=True)


def test_jsonl_synthesize_llm_passthrough(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = _auth_page(store)
    _wire_llm(store, tmp_path, {
        "answer": f"Access tokens are short-lived JWTs [{pid}].", "gaps": [],
    })
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "s2", "method": "kb.synthesize",
        "params": {"query": "auth tokens", "llm": True},
    })
    assert resp["ok"]
    assert resp["result"]["pages"] == [pid]
    assert resp["result"]["_meta"]["synthesis_backend"] == "llm"


def test_capabilities_lists_synthesize() -> None:
    assert "kb.synthesize" in capabilities.capabilities().methods
    assert "kb.synthesize" in HANDLERS


def test_jsonl_synthesize_handler(store: KBStore, monkeypatch) -> None:
    ids = _auth_kb(store)
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "s1", "method": "kb.synthesize",
        "params": {"query": "auth", "depth": 5},
    })
    assert resp["ok"]
    assert resp["result"]["claims"]
    assert set(resp["result"]["claims"]) <= set(ids)
    for cid in resp["result"]["claims"]:
        assert f"[{cid}]" in resp["result"]["answer"]
