"""JSONL tool server — request/response envelope behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import health
from vouch.jsonl_server import handle_request
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_jsonl_search_request(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="findable token", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r1", "method": "kb.search",
                           "params": {"query": "findable"}})
    assert resp["ok"]
    assert resp["id"] == "r1"
    assert any(it["id"] == "c1" for it in resp["result"])


def test_jsonl_unknown_method_returns_error(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r2", "method": "kb.bogus", "params": {}})
    assert not resp["ok"]
    assert resp["error"]["code"] == "method_not_found"


def test_jsonl_dry_run_propose_then_real_propose(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    monkeypatch.chdir(store.root)
    dry = handle_request({"id": "1", "method": "kb.propose_claim",
                          "params": {"text": "x", "evidence": [src.id],
                                     "dry_run": True}})
    assert dry["ok"] and dry["result"]["dry_run"] is True
    real = handle_request({"id": "2", "method": "kb.propose_claim",
                           "params": {"text": "x", "evidence": [src.id]}})
    assert real["ok"]
    pending = handle_request({"id": "3", "method": "kb.list_pending",
                              "params": {}})
    assert len(pending["result"]) == 1


def test_jsonl_full_flow(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"raw evidence")
    monkeypatch.chdir(store.root)
    pr = handle_request({"id": "1", "method": "kb.propose_claim",
                         "params": {"text": "JWT used", "evidence": [src.id]}})
    pid = pr["result"]["proposal_id"]
    handle_request({"id": "2", "method": "kb.approve",
                    "params": {"proposal_id": pid}})
    status = handle_request({"id": "3", "method": "kb.status", "params": {}})
    assert status["result"]["claims"] == 1
    caps = handle_request({"id": "4", "method": "kb.capabilities", "params": {}})
    assert caps["result"]["review_gated"] is True


def test_jsonl_session_lifecycle(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    monkeypatch.chdir(store.root)
    sess = handle_request({"id": "1", "method": "kb.session_start",
                           "params": {"task": "demo"}})
    sid = sess["result"]["id"]
    handle_request({"id": "2", "method": "kb.propose_claim",
                    "params": {"text": "x", "evidence": [src.id],
                               "session_id": sid}})
    handle_request({"id": "3", "method": "kb.session_end",
                    "params": {"session_id": sid}})
    cryst = handle_request({"id": "4", "method": "kb.crystallize",
                            "params": {"session_id": sid}})
    assert len(cryst["result"]["approved"]) == 1
