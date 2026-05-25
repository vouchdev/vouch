"""Session lifecycle — start, end, crystallize."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
    pr1 = propose_claim(store, text="first finding", evidence=[src.id],
                        proposed_by="claude-code", session_id=sess.id)
    pr2 = propose_claim(store, text="second finding", evidence=[src.id],
                        proposed_by="claude-code", session_id=sess.id)
    sess = sess_mod.session_end(store, sess.id)
    assert sorted(sess.proposal_ids) == sorted([pr1.id, pr2.id])

    result = sess_mod.crystallize(store, sess.id, approver="u")
    assert len(result["approved"]) == 2
    assert result["summary_page_id"] is not None
    assert {c.text for c in store.list_claims()} == {
        "first finding", "second finding",
    }


def test_crystallize_skips_already_approved(store: KBStore) -> None:
    src = store.put_source(b"e")
    sess = sess_mod.session_start(store, agent="a")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a",
                       session_id=sess.id)
    approve(store, pr.id, approved_by="u")
    sess_mod.session_end(store, sess.id)
    result = sess_mod.crystallize(store, sess.id, approver="u")
    assert result["approved"] == []  # already handled


def test_crystallize_collects_approval_failures(store: KBStore) -> None:
    src = store.put_source(b"e")
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
