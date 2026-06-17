"""Entity-salience retrieval reflex — issue #223."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import health, salience
from vouch.models import Claim, Entity, EntityType
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _clean_buffers() -> None:
    salience._BUFFERS.clear()
    yield
    salience._BUFFERS.clear()


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    kb = KBStore.init(tmp_path)
    kb.put_entity(Entity(id="jwt", name="JWT", type=EntityType.CONCEPT))
    src = kb.put_source(b"auth notes")
    kb.put_claim(Claim(id="c1", text="auth uses JWT", evidence=[src.id], entities=["jwt"]))
    health.rebuild_index(kb)
    return kb


def _write_reflex_cfg(store: KBStore, **reflex: object) -> None:
    import yaml
    cfg = {"retrieval": {"reflex": reflex}}
    (store.kb_dir / "config.yaml").write_text(yaml.safe_dump(cfg))


def test_record_then_compute_highlights_entity(store: KBStore) -> None:
    for _ in range(3):
        salience.record_query("sess-1", "jwt")
    out = salience.compute_salience(store, "sess-1")
    assert out
    rec = out[0]
    assert rec["entity_id"] == "jwt"
    assert rec["claim_count"] == 1
    assert rec["top_claim_id"] == "c1"


def test_attach_adds_meta_when_enabled(store: KBStore) -> None:
    for _ in range(3):
        salience.record_query("sess-1", "jwt")
    cfg = {"retrieval": {"reflex": {"enabled": True}}}
    result = salience.attach_salience({}, store, "sess-1", cfg)
    assert result["_meta"]["vouch_salience"][0]["entity_id"] == "jwt"


def test_handler_attaches_salience_on_next_context_call(store: KBStore, monkeypatch) -> None:
    from vouch import jsonl_server

    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    for _ in range(3):
        jsonl_server._h_context({"task": "jwt", "session_id": "sess-1"})
    result = jsonl_server._h_context({"task": "jwt", "session_id": "sess-1"})
    salient = result["_meta"]["vouch_salience"]
    assert any(rec["entity_id"] == "jwt" for rec in salient)


def test_disabled_in_config_omits_field(store: KBStore, monkeypatch) -> None:
    from vouch import jsonl_server

    _write_reflex_cfg(store, enabled=False)
    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    for _ in range(3):
        jsonl_server._h_context({"task": "jwt", "session_id": "sess-1"})
    result = jsonl_server._h_context({"task": "jwt", "session_id": "sess-1"})
    assert "vouch_salience" not in result.get("_meta", {})


def test_stateless_call_has_no_salience(store: KBStore, monkeypatch) -> None:
    from vouch import jsonl_server

    monkeypatch.setattr(jsonl_server, "_store", lambda: store)
    result = jsonl_server._h_context({"task": "jwt"})
    assert "vouch_salience" not in result.get("_meta", {})


def test_reset_session_clears_buffer(store: KBStore) -> None:
    salience.record_query("sess-1", "jwt")
    assert salience.compute_salience(store, "sess-1")
    salience.reset_session("sess-1")
    assert salience.compute_salience(store, "sess-1") == []


def test_session_end_resets_buffer(tmp_path: Path) -> None:
    from vouch import sessions

    kb = KBStore.init(tmp_path)
    sess = sessions.session_start(kb, agent="a", task="t")
    salience.record_query(sess.id, "jwt")
    assert sess.id in salience._BUFFERS
    sessions.session_end(kb, sess.id)
    assert sess.id not in salience._BUFFERS


def test_window_bounds_buffer(store: KBStore) -> None:
    for i in range(20):
        salience.record_query("sess-1", f"q{i}", window=8)
    assert len(salience._BUFFERS["sess-1"].queries) == 8
