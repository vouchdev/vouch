"""Passive answer-memory: transcript extraction + capture_answer.

`capture_answer` turns a session's latest Q&A into receipt-backed claims and
self-approves only what the review gate already allows (trusted-agent or
auto_approve_on_receipt); with neither opt-in the claims stay pending. It is
idempotent (same answer bytes) and quiet (skips short acknowledgements), so a
Stop hook firing every turn does not duplicate or flood the KB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import capture as cap
from vouch.models import ProposalStatus
from vouch.storage import KBStore

# an answer with three clean, quotable sentences (>160 chars) so segment_source
# yields receipt-verifiable claims.
ANSWER = (
    "Vouch is pivoting from a memory store into a verified knowledge compiler. "
    "The review gate is becoming arithmetic instead of a person. "
    "Passive session capture saves a session answer and recalls it in a fresh session."
)
QUESTION = "what's vouch roadmap?"


def _transcript(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _user(text: str, *, meta: bool = False) -> dict:
    row: dict = {"type": "user", "message": _msg("user", text)}
    if meta:
        row["isMeta"] = True
    return row


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": _msg("assistant", text)}


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path / "kb")


def _enable_receipt_gate(store: KBStore) -> None:
    store.config_path.write_text("review:\n  auto_approve_on_receipt: true\n", encoding="utf-8")


def _enable_trusted_agent(store: KBStore) -> None:
    store.config_path.write_text("review:\n  approver_role: trusted-agent\n", encoding="utf-8")


# --- last_exchange -------------------------------------------------------


def test_last_exchange_extracts_question_and_answer(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant(ANSWER)])
    got = cap.last_exchange(tp)
    assert got is not None
    q, a = got
    assert q == QUESTION
    assert a == ANSWER


def test_last_exchange_skips_meta_and_wrapper_turns(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, [
        _user("<command-name>/compact</command-name>"),
        _user("caveat: local command output"),
        _user(QUESTION, meta=True),   # meta -> ignored
        _user(QUESTION),              # the real question
        _assistant(ANSWER),
    ])
    got = cap.last_exchange(tp)
    assert got is not None
    assert got[0] == QUESTION


def test_last_exchange_pairs_the_latest_answer(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, [
        _user("first question"),
        _assistant("first answer, discarded"),
        _user(QUESTION),
        _assistant(ANSWER),
    ])
    got = cap.last_exchange(tp)
    assert got == (QUESTION, ANSWER)


def test_last_exchange_none_without_assistant(tmp_path: Path) -> None:
    tp = _transcript(tmp_path, [_user(QUESTION)])
    assert cap.last_exchange(tp) is None


def test_last_exchange_missing_file(tmp_path: Path) -> None:
    assert cap.last_exchange(tmp_path / "nope.jsonl") is None


# --- capture_answer ------------------------------------------------------


def test_capture_answer_approves_under_receipt_gate(store: KBStore, tmp_path: Path) -> None:
    _enable_receipt_gate(store)
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant(ANSWER)])
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is True
    assert res["filed"] >= 3
    assert res["approved"] == res["filed"]
    # no human, and the claims are durable + queryable.
    assert cap.pending_count(store) == 0
    approved = [p for p in store.list_proposals(ProposalStatus.APPROVED)]
    assert len(approved) >= 3
    # the answer's knowledge is now durable and findable by content.
    texts = " ".join(c.text.lower() for c in store.list_claims())
    assert "knowledge compiler" in texts


def test_capture_answer_approves_under_trusted_agent(store: KBStore, tmp_path: Path) -> None:
    _enable_trusted_agent(store)
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant(ANSWER)])
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is True
    assert res["approved"] == res["filed"] >= 3


def test_capture_answer_leaves_pending_when_gate_off(store: KBStore, tmp_path: Path) -> None:
    # default starter config: neither opt-in set.
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant(ANSWER)])
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is True
    assert res["filed"] >= 3
    assert res["approved"] == 0
    # the review gate is honoured — claims wait for a human.
    pending = [p for p in store.list_proposals(ProposalStatus.PENDING)]
    assert len(pending) >= 3


def test_capture_answer_skips_short_answer(store: KBStore, tmp_path: Path) -> None:
    _enable_receipt_gate(store)
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant("done.")])
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is False
    assert res["skipped"] == "answer-too-short"


def test_capture_answer_is_idempotent(store: KBStore, tmp_path: Path) -> None:
    _enable_receipt_gate(store)
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant(ANSWER)])
    first = cap.capture_answer(store, "sess-1", tp)
    assert first["captured"] is True
    # same answer bytes on a second Stop-hook fire -> skipped, no duplicates.
    second = cap.capture_answer(store, "sess-1", tp)
    assert second["captured"] is False
    assert second["skipped"] == "already-captured"
    assert cap.pending_count(store) == 0


def test_capture_answer_no_answer(store: KBStore, tmp_path: Path) -> None:
    _enable_receipt_gate(store)
    tp = _transcript(tmp_path, [_user(QUESTION)])
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is False
    assert res["skipped"] == "no-answer"


def test_capture_answer_disabled_by_env(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_receipt_gate(store)
    monkeypatch.setenv("VOUCH_CAPTURE_DISABLE", "1")
    tp = _transcript(tmp_path, [_user(QUESTION), _assistant(ANSWER)])
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is False
    assert res["skipped"] == "disabled-env"
