"""CLI surface — every command must turn domain errors into a clean
`Error: ...` line via click.ClickException, never a raw Python traceback.

These regressions cover the bug class where CLI handlers caught
``(ArtifactNotFoundError, ValueError)`` but ``proposals.approve()`` /
``proposals.reject()`` (and the ``propose_*`` helpers) raise
``ProposalError`` (a ``RuntimeError`` subclass) for validation failures —
which slipped past the except and surfaced as a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from vouch import sessions as sess_mod
from vouch.cli import cli
from vouch.models import ProposalStatus
from vouch.proposals import propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _assert_clean_error(result, needle: str) -> None:
    assert result.exit_code != 0, result.output
    # click.ClickException renders as ``Error: <msg>``; a raw traceback would
    # include the Python frame marker ``Traceback (most recent call last)``.
    assert "Traceback" not in result.output, result.output
    assert "Error:" in result.output, result.output
    assert needle in result.output, result.output


def test_approve_already_decided_proposal_shows_clean_error(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="x", evidence=[src.id], proposed_by="agent")
    runner = CliRunner()
    first = runner.invoke(cli, ["approve", pr.id])
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli, ["approve", pr.id])
    _assert_clean_error(second, "not pending")


def test_reject_empty_reason_shows_clean_error(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="x", evidence=[src.id], proposed_by="agent")
    result = CliRunner().invoke(cli, ["reject", pr.id, "--reason", "   "])
    _assert_clean_error(result, "reason")


def test_propose_claim_empty_text_shows_clean_error(store: KBStore) -> None:
    src = store.put_source(b"e")
    result = CliRunner().invoke(
        cli, ["propose-claim", "--text", "   ", "--source", src.id]
    )
    _assert_clean_error(result, "claim text")


def test_propose_claim_unknown_source_shows_clean_error(store: KBStore) -> None:
    result = CliRunner().invoke(
        cli, ["propose-claim", "--text", "ok", "--source", "deadbeef"]
    )
    _assert_clean_error(result, "unknown source")


def test_propose_entity_empty_name_shows_clean_error(store: KBStore) -> None:
    result = CliRunner().invoke(
        cli, ["propose-entity", "--name", "   ", "--type", "project"]
    )
    _assert_clean_error(result, "entity name")


def test_show_missing_proposal_shows_clean_error(store: KBStore) -> None:
    result = CliRunner().invoke(cli, ["show", "no-such-proposal"])
    _assert_clean_error(result, "proposal no-such-proposal")


def test_fsck_clean_kb_prints_clean_and_exits_zero(store: KBStore) -> None:
    """`vouch fsck` on a fresh KB exits 0 and only emits info-level findings."""
    from vouch.models import Claim
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    result = CliRunner().invoke(cli, ["fsck"])
    # No state.db yet → info finding, but report.ok stays True.
    assert result.exit_code == 0, result.output
    assert "[index_missing]" in result.output


def test_fsck_reports_dangling_chain_and_exits_nonzero(store: KBStore) -> None:
    """`vouch fsck` exits 1 on error findings and prints affected object ids."""
    from vouch.models import Claim
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="c1", text="t", evidence=[src.id], supersedes=["ghost"],
    ))
    result = CliRunner().invoke(cli, ["fsck"])
    assert result.exit_code == 1, result.output
    assert "dangling_supersedes" in result.output
    # The affected object ids are surfaced inline so users can grep / pipe.
    assert "(objects: c1, ghost)" in result.output


def test_pending_json_empty_queue(store: KBStore) -> None:
    result = CliRunner().invoke(cli, ["pending", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_pending_json_lists_pending_proposals(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="pending json claim", evidence=[src.id], proposed_by="agent")

    result = CliRunner().invoke(cli, ["pending", "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["id"] == pr.id
    assert rows[0]["kind"] == "claim"
    assert rows[0]["proposed_by"] == "agent"
    assert rows[0]["status"] == "pending"
    assert rows[0]["payload"]["text"] == "pending json claim"


def test_pending_human_output_remains_text(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(store, text="pending text claim", evidence=[src.id], proposed_by="agent")

    result = CliRunner().invoke(cli, ["pending"])

    assert result.exit_code == 0, result.output
    assert pr.id in result.output
    assert "[claim]  by agent" in result.output
    assert "pending text claim" in result.output


def test_review_approves_pending_proposal(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.setenv("VOUCH_USER", "reviewer")
    src = store.put_source(b"e")
    pr = propose_claim(
        store,
        text="review can approve",
        evidence=[src.id],
        proposed_by="agent",
    )

    result = CliRunner().invoke(cli, ["review"], input="a\n\n")

    assert result.exit_code == 0, result.output
    assert "Approved -> claim/review-can-approve" in result.output
    assert store.get_claim("review-can-approve").text == "review can approve"
    assert store.get_proposal(pr.id).status == ProposalStatus.APPROVED


def test_review_rejects_with_reason(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.setenv("VOUCH_USER", "reviewer")
    src = store.put_source(b"e")
    pr = propose_claim(
        store,
        text="review can reject",
        evidence=[src.id],
        proposed_by="agent",
    )

    result = CliRunner().invoke(cli, ["review"], input="r\nnot true\n")

    assert result.exit_code == 0, result.output
    assert f"Rejected {pr.id}" in result.output
    decided = store.get_proposal(pr.id)
    assert decided.status == ProposalStatus.REJECTED
    assert decided.decision_reason == "not true"


def test_review_skip_and_quit_leave_proposals_pending(store: KBStore) -> None:
    src = store.put_source(b"e")
    first = propose_claim(
        store,
        text="review can skip",
        evidence=[src.id],
        proposed_by="agent",
    )
    second = propose_claim(
        store,
        text="review can quit",
        evidence=[src.id],
        proposed_by="agent",
    )

    result = CliRunner().invoke(cli, ["review"], input="s\nq\n")

    assert result.exit_code == 0, result.output
    assert "Skipped " in result.output
    assert "Stopped review" in result.output
    assert store.get_proposal(first.id).status == ProposalStatus.PENDING
    assert store.get_proposal(second.id).status == ProposalStatus.PENDING


def test_review_dry_run_does_not_mutate(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.setenv("VOUCH_USER", "reviewer")
    src = store.put_source(b"e")
    pr = propose_claim(
        store,
        text="review dry run",
        evidence=[src.id],
        proposed_by="agent",
    )

    result = CliRunner().invoke(cli, ["review", "--dry-run"], input="a\n\n")

    assert result.exit_code == 0, result.output
    assert f"Would approve {pr.id}" in result.output
    assert store.get_proposal(pr.id).status == ProposalStatus.PENDING
    with pytest.raises(KeyError):
        store.get_claim("review-dry-run")


def test_review_dry_run_reject_does_not_mutate(store: KBStore) -> None:
    src = store.put_source(b"e")
    pr = propose_claim(
        store,
        text="review dry run reject",
        evidence=[src.id],
        proposed_by="agent",
    )

    result = CliRunner().invoke(cli, ["review", "--dry-run"], input="r\nnot true\n")

    assert result.exit_code == 0, result.output
    assert f"Would reject {pr.id}" in result.output
    pending = store.get_proposal(pr.id)
    assert pending.status == ProposalStatus.PENDING
    assert pending.decision_reason is None


def test_search_fts5_backend_label(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vouch search prints (fts5) when FTS5 returns hits."""
    from vouch import index_db
    from vouch.proposals import approve as do_approve
    from vouch.proposals import propose_claim
    src = store.put_source(b"e")
    pr = propose_claim(store, text="findable token", evidence=[src.id], proposed_by="agent")
    do_approve(store, pr.id, approved_by="reviewer")
    # Index only the FTS5 tables directly — no embedding stack needed
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id="c-findable", text="findable token",
            type="observation", status="actionable", tags=[],
        )
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "findable"])
    assert result.exit_code == 0, result.output
    assert "(fts5)" in result.output


def test_search_substring_backend_label(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vouch search prints (substring) when FTS5 raises and fallback runs."""
    from vouch.proposals import approve as do_approve
    from vouch.proposals import propose_claim
    src = store.put_source(b"e")
    pr = propose_claim(store, text="findable token", evidence=[src.id], proposed_by="agent")
    do_approve(store, pr.id, approved_by="reviewer")
    # Remove state.db so FTS5 raises and substring fallback runs
    state_db = store.kb_dir / "state.db"
    if state_db.exists():
        state_db.unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "findable"])
    assert result.exit_code == 0, result.output
    assert "(substring)" in result.output


def test_crystallize_cli_all_failures_exits_1(store: KBStore) -> None:
    with patch.object(KBStore, "_embed_and_store"):
        src = store.put_source(b"e")
    sess = sess_mod.session_start(store, agent="a", task="t")
    propose_claim(store, text="t1", evidence=[src.id], proposed_by="a", session_id=sess.id)
    propose_claim(store, text="t2", evidence=[src.id], proposed_by="a", session_id=sess.id)
    sess_mod.session_end(store, sess.id)

    with patch("vouch.sessions.approve", side_effect=ValueError("storage full")):
        result = CliRunner().invoke(cli, ["crystallize", sess.id])

    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "all 2 proposal(s) failed" in result.stderr


def test_crystallize_cli_partial_failures_shows_warning(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch.proposals import approve as real_approve

    # Approve as a distinct reviewer so the second (real) approval isn't blocked
    # by the self-approval guard. Without this the approver defaults to the OS
    # user via _whoami(), which on a machine whose login happens to be "a"
    # collides with the session agent and makes the test environment-dependent.
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    with patch.object(KBStore, "_embed_and_store"):
        src = store.put_source(b"e")
    sess = sess_mod.session_start(store, agent="a", task="t")
    propose_claim(store, text="t1", evidence=[src.id], proposed_by="a", session_id=sess.id)
    propose_claim(store, text="t2", evidence=[src.id], proposed_by="a", session_id=sess.id)
    sess_mod.session_end(store, sess.id)

    call_count = 0

    def _side_effect(store, proposal_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("storage full")
        return real_approve(store, proposal_id, **kwargs)

    with patch("vouch.sessions.approve", side_effect=_side_effect), \
         patch.object(KBStore, "_embed_and_store"):
        result = CliRunner().invoke(cli, ["crystallize", sess.id])

    assert result.exit_code == 0
    assert "warning:" in result.stderr
    assert "1/2 proposal(s) failed" in result.stderr


# --- batch approval (#93) -------------------------------------------------


def _propose_n(store: KBStore, n: int) -> list[str]:
    src = store.put_source(b"e")
    ids = []
    for i in range(n):
        pr = propose_claim(
            store, text=f"batch claim {i}", evidence=[src.id], proposed_by="agent"
        )
        ids.append(pr.id)
    return ids


def test_approve_batch_approves_all(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    ids = _propose_n(store, 3)
    result = CliRunner().invoke(cli, ["approve", *ids])
    assert result.exit_code == 0, result.output
    pending = {p.id for p in store.list_proposals(ProposalStatus.PENDING)}
    for pid in ids:
        assert pid not in pending
    assert result.output.count("Approved") == 3


def test_approve_batch_one_audit_event_per_artifact(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch import audit
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    ids = _propose_n(store, 2)
    CliRunner().invoke(cli, ["approve", *ids])
    approve_events = [
        e for e in audit.read_events(store.kb_dir)
        if e.event.endswith(".approve")
    ]
    assert len(approve_events) == 2


def test_approve_batch_atomic_aborts_on_bad_id(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default is all-or-nothing: one bad id approves nothing."""
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    good = _propose_n(store, 2)
    result = CliRunner().invoke(cli, ["approve", good[0], "no-such-id", good[1]])
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output
    # Nothing approved — both good proposals are still pending.
    for cid in good:
        assert cid in {p.id for p in store.list_proposals(ProposalStatus.PENDING)}


def test_approve_batch_keep_going_best_effort(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--keep-going approves what it can and exits non-zero on partial failure."""
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    good = _propose_n(store, 2)
    result = CliRunner().invoke(
        cli, ["approve", "--keep-going", good[0], "no-such-id", good[1]]
    )
    assert result.exit_code != 0, result.output
    # Both valid proposals were approved despite the bad id in the middle.
    pending = {p.id for p in store.list_proposals(ProposalStatus.PENDING)}
    assert good[0] not in pending
    assert good[1] not in pending
