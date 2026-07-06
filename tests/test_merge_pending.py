"""Merging pending proposals (pages or claims) into one combined proposal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch.models import ProposalKind, ProposalStatus
from vouch.proposals import (
    ProposalError,
    merge_pending,
    merge_pending_pages,
    propose_claim,
    propose_page,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _session_page(store: KBStore, n: int) -> str:
    p = propose_page(
        store,
        title=f"session: do thing {n}",
        body=f"# session: do thing {n}\n\n## prompt\n\n> do thing {n}\n",
        page_type="session",
        proposed_by="vouch-capture",
        session_id=f"sid-{n}",
        tags=[f"tag{n}", "shared"],
    )
    return p.id


def test_merge_files_one_pending_and_rejects_sources(store: KBStore) -> None:
    a, b = _session_page(store, 1), _session_page(store, 2)
    merged = merge_pending_pages(store, [a, b], merged_by="reviewer")

    assert merged.status == ProposalStatus.PENDING
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert [p.id for p in pending] == [merged.id]

    for src in (a, b):
        decided = store.get_proposal(src)
        assert decided.status == ProposalStatus.REJECTED
        assert decided.decision_reason == f"merged into proposal {merged.id}"

    payload = merged.payload
    assert payload["type"] == "session"
    assert str(payload["title"]).startswith("merged (2): session: do thing 1")
    body = str(payload["body"])
    # both source bodies present, chronological, headings demoted one level
    assert body.index("do thing 1") < body.rindex("do thing 2")
    assert "## session: do thing 1" in body
    assert "### prompt" in body
    # tag union, no duplicates
    assert payload["tags"] == ["tag1", "shared", "tag2"]


def test_merge_validation_errors(store: KBStore) -> None:
    a = _session_page(store, 1)
    with pytest.raises(ProposalError, match="at least two"):
        merge_pending_pages(store, [a, a], merged_by="reviewer")

    src = store.put_source(b"some evidence", title="ev")
    claim = propose_claim(
        store, text="a claim", proposed_by="agent", evidence=[src.id],
    )
    with pytest.raises(ProposalError, match="single kind"):
        merge_pending_pages(store, [a, claim.id], merged_by="reviewer")

    concept = propose_page(
        store, title="a concept", body="body", page_type="concept",
        proposed_by="agent",
    )
    with pytest.raises(ProposalError, match="different types"):
        merge_pending_pages(store, [a, concept.id], merged_by="reviewer")

    b = _session_page(store, 2)
    merged = merge_pending_pages(store, [a, b], merged_by="reviewer")
    with pytest.raises(ProposalError, match="not pending"):
        merge_pending_pages(store, [a, merged.id], merged_by="reviewer")


def test_merge_writes_audit_event(store: KBStore) -> None:
    a, b = _session_page(store, 1), _session_page(store, 2)
    merged = merge_pending_pages(store, [a, b], merged_by="reviewer")
    events = [
        json.loads(ln)
        for ln in (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    ev = next(e for e in events if e["event"] == "proposal.page.merge")
    assert ev["actor"] == "reviewer"
    assert ev["data"]["merged_into"] == merged.id
    assert ev["data"]["sources"] == [a, b]


def test_merged_proposal_is_approvable(store: KBStore) -> None:
    from vouch.proposals import approve

    a, b = _session_page(store, 1), _session_page(store, 2)
    merged = merge_pending_pages(store, [a, b], merged_by="reviewer")
    page = approve(store, merged.id, approved_by="someone-else")
    assert "do thing 1" in page.body  # type: ignore[union-attr]
    assert "do thing 2" in page.body  # type: ignore[union-attr]


# --- claim merging (captured sessions file as claims) ------------------------


def _session_claim(store: KBStore, n: int) -> str:
    src = store.put_source(f"record {n}".encode(), title=f"session record {n}")
    p = propose_claim(
        store,
        text=f"# session: do thing {n}\n\n## prompt\n\n> do thing {n}\n",
        evidence=[src.id],
        proposed_by="vouch-capture",
        claim_type="session",
        session_id=f"sid-{n}",
        tags=[f"tag{n}", "session"],
    )
    return p.proposal.id


def test_merge_claims_unions_evidence_and_rejects_sources(store: KBStore) -> None:
    a, b = _session_claim(store, 1), _session_claim(store, 2)
    merged = merge_pending(store, [a, b], merged_by="reviewer")

    assert merged.kind == ProposalKind.CLAIM
    assert merged.status == ProposalStatus.PENDING
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert [p.id for p in pending] == [merged.id]
    for src_id in (a, b):
        assert store.get_proposal(src_id).status == ProposalStatus.REJECTED

    payload = merged.payload
    assert payload["type"] == "session"
    assert len(payload["evidence"]) == 2  # union of both cited sources
    text = str(payload["text"])
    assert text.index("do thing 1") < text.rindex("do thing 2")
    assert "## session: do thing 1" in text  # headings demoted
    assert payload["tags"] == ["tag1", "session", "tag2"]


def test_merged_claim_is_approvable(store: KBStore) -> None:
    from vouch.proposals import approve

    a, b = _session_claim(store, 1), _session_claim(store, 2)
    merged = merge_pending(store, [a, b], merged_by="reviewer")
    claim = approve(store, merged.id, approved_by="someone-else")
    assert "do thing 1" in claim.text  # type: ignore[union-attr]
    assert "do thing 2" in claim.text  # type: ignore[union-attr]


def test_merge_claims_writes_claim_audit_event(store: KBStore) -> None:
    a, b = _session_claim(store, 1), _session_claim(store, 2)
    merged = merge_pending(store, [a, b], merged_by="reviewer")
    events = [
        json.loads(ln)
        for ln in (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    ev = next(e for e in events if e["event"] == "proposal.claim.merge")
    assert ev["data"]["merged_into"] == merged.id


def test_cli_merge_pending(store: KBStore) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    a, b = _session_page(store, 1), _session_page(store, 2)
    runner = CliRunner()
    res = runner.invoke(
        cli, ["merge-pending", a, b, "--reason", "same work, two captures"],
        env={"VOUCH_KB_PATH": str(store.kb_dir)},
    )
    assert res.exit_code == 0, res.output
    assert "Merged 2 proposals into" in res.output
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].rationale == "same work, two captures"
