"""Session lifecycle — start, end, crystallize."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import sessions as sess_mod
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_session_lifecycle_and_crystallize(store: KBStore) -> None:
    src = store.put_source(b"e")
    sess = sess_mod.session_start(store, agent="claude-code", task="design")
    pr1 = propose_claim(
        store,
        text="first finding",
        evidence=[src.id],
        proposed_by="claude-code",
        session_id=sess.id,
    )
    pr2 = propose_claim(
        store,
        text="second finding",
        evidence=[src.id],
        proposed_by="claude-code",
        session_id=sess.id,
    )
    sess = sess_mod.session_end(store, sess.id)
    assert sorted(sess.proposal_ids) == sorted([pr1.id, pr2.id])

    result = sess_mod.crystallize(store, sess.id, approver="u")
    assert len(result["approved"]) == 2
    assert result["summary_page_id"] is not None
    assert {c.text for c in store.list_claims()} == {
        "first finding",
        "second finding",
    }


def test_crystallize_skips_already_approved(store: KBStore) -> None:
    src = store.put_source(b"e")
    sess = sess_mod.session_start(store, agent="a")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a", session_id=sess.id)
    approve(store, pr.id, approved_by="u")
    sess_mod.session_end(store, sess.id)
    result = sess_mod.crystallize(store, sess.id, approver="u")
    assert result["approved"] == []  # already handled


def test_crystallize_single_agent_succeeds(tmp_path, monkeypatch) -> None:
    """Single-agent crystallize must succeed when trusted-agent is configured."""
    import yaml

    from vouch import sessions as sess_mod
    from vouch.storage import KBStore

    store = KBStore.init(tmp_path)
    monkeypatch.chdir(store.root)

    # Configure trusted-agent opt-out
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("review", {})["approver_role"] = "trusted-agent"
    cfg_path.write_text(yaml.safe_dump(cfg))

    src = store.put_source(b"evidence")
    sess = sess_mod.session_start(store, agent="alice")

    from vouch.proposals import propose_claim

    propose_claim(
        store,
        text="a claim",
        evidence=[src.id],
        proposed_by="alice",
        session_id=sess.id,
    )

    result = sess_mod.crystallize(store, sess.id, approver="alice")
    assert result["approved"], f"expected approved artifacts, got: {result}"
    assert result["failures"] == [], f"unexpected failures: {result['failures']}"


def test_crystallize_collects_approval_failures(store):
    from unittest.mock import patch

    from vouch.proposals import propose_claim

    src = store.put_source(b"e")
    import vouch.sessions as sess_mod

    sess = sess_mod.session_start(store, agent="a", task="t")
    propose_claim(store, text="t", evidence=[src.id], proposed_by="a", session_id=sess.id)
    propose_claim(store, text="u", evidence=[src.id], proposed_by="a", session_id=sess.id)
    sess_mod.session_end(store, sess.id)

    with patch("vouch.sessions.approve", side_effect=ValueError("storage full")):
        result = sess_mod.crystallize(store, sess.id, approver="u")

    assert result["approved"] == []
    assert len(result["failures"]) == 2
    for f in result["failures"]:
        assert f["error"] == "storage full"
        assert f["error_type"] == "ValueError"
