"""Dead claim references: approve-time strip + KB-wide wipe.

A claim can disappear between propose and approve (archived file removed,
redaction, bulk clear) while pages still point at it. Approval refuses the
dead reference by default, offers drop_missing_claims to strip it, and
lifecycle.wipe_dead_claim_refs clears every dead pointer KB-wide.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import lifecycle as life
from vouch.jsonl_server import handle_request
from vouch.proposals import (
    DeadClaimRefsError,
    approve,
    missing_claim_refs,
    propose_claim,
    propose_page,
    strip_claim_markers,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _approved_claim(store: KBStore, text: str) -> str:
    src = store.put_source(text.encode())
    pr = propose_claim(store, text=text, evidence=[src.id], proposed_by="agent")
    return approve(store, pr.id, approved_by="human").id


def _dead_ref_page_proposal(store: KBStore, *, title: str = "topic page"):
    """A pending PAGE proposal whose only cited claim no longer resolves."""
    cid = _approved_claim(store, f"claim behind {title}")
    pr = propose_page(
        store,
        title=title,
        body=f"the fact. [claim: {cid}] more prose.",
        claim_ids=[cid],
        proposed_by="agent",
    )
    store._claim_path(cid).unlink()
    return pr, cid


def _audit_events(store: KBStore) -> list[dict]:
    text = (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_approve_refuses_dead_claim_refs(store: KBStore) -> None:
    pr, cid = _dead_ref_page_proposal(store)
    with pytest.raises(DeadClaimRefsError) as exc:
        approve(store, pr.id, approved_by="human")
    assert exc.value.missing == [cid]
    assert exc.value.proposal_id == pr.id
    assert store.get_proposal(pr.id).status.value == "pending"


def test_approve_drop_missing_claims_strips_and_audits(store: KBStore) -> None:
    pr, cid = _dead_ref_page_proposal(store)
    page = approve(store, pr.id, approved_by="human", drop_missing_claims=True)
    assert cid not in page.claims
    assert f"[claim: {cid}]" not in page.body
    assert "the fact." in page.body  # prose survives, only the marker goes
    ev = [e for e in _audit_events(store) if e["event"] == "proposal.page.approve"][-1]
    assert ev["data"]["dropped_claims"] == [cid]


def test_missing_claim_refs_ignores_live_and_archived(store: KBStore) -> None:
    live = _approved_claim(store, "live claim")
    archived = _approved_claim(store, "archived claim")
    pr = propose_page(
        store, title="healthy", body="b", claim_ids=[live, archived],
        proposed_by="agent",
    )
    life.archive(store, claim_id=archived, actor="human")
    # An archived claim's file still exists — only unresolvable ids are dead.
    assert missing_claim_refs(store, store.get_proposal(pr.id)) == []
    page = approve(store, pr.id, approved_by="human")
    assert page.claims == [live, archived]


def test_wipe_dead_claim_refs_pages_and_pending(store: KBStore) -> None:
    # A durable page whose cited claim dies after approval …
    cid = _approved_claim(store, "will die")
    ppr = propose_page(
        store, title="durable", body=f"x [claim: {cid}]", claim_ids=[cid],
        proposed_by="agent",
    )
    approve(store, ppr.id, approved_by="human")
    # … and a pending page proposal in the same dead-ref state.
    pending, cid2 = _dead_ref_page_proposal(store, title="pending page")
    store._claim_path(cid).unlink()

    preview = life.wipe_dead_claim_refs(store, actor="human", dry_run=True)
    assert preview.dry_run
    assert preview.dropped == 2
    assert cid in store.get_page("durable").claims  # dry-run wrote nothing

    result = life.wipe_dead_claim_refs(store, actor="human")
    assert result.pages == {"durable": [cid]}
    assert result.proposals == {pending.id: [cid2]}
    page = store.get_page("durable")
    assert page.claims == []
    assert "[claim:" not in page.body
    assert store.get_proposal(pending.id).payload["claims"] == []
    assert any(e["event"] == "page.dead_refs_wipe" for e in _audit_events(store))

    again = life.wipe_dead_claim_refs(store, actor="human")
    assert again.dropped == 0


def test_wipe_stripped_pending_proposal_is_approvable(store: KBStore) -> None:
    pending, _ = _dead_ref_page_proposal(store)
    life.wipe_dead_claim_refs(store, actor="human")
    page = approve(store, pending.id, approved_by="human")
    assert page.claims == []


def test_strip_claim_markers_targets_only_named_ids() -> None:
    body = "a [claim: one] b [claim: two] c"
    assert strip_claim_markers(body, ["one"]) == "a b [claim: two] c"


def test_jsonl_approve_dead_refs_error_code(store: KBStore, monkeypatch) -> None:
    pr, _ = _dead_ref_page_proposal(store)
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r1", "method": "kb.approve",
                           "params": {"proposal_id": pr.id}})
    assert not resp["ok"]
    assert resp["error"]["code"] == "dead_claim_refs"

    resp = handle_request({"id": "r2", "method": "kb.approve",
                           "params": {"proposal_id": pr.id,
                                      "drop_missing_claims": True}})
    assert resp["ok"]
    assert resp["result"]["kind"] == "page"


def test_jsonl_wipe_dead_refs(store: KBStore, monkeypatch) -> None:
    pr, cid = _dead_ref_page_proposal(store)
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r1", "method": "kb.wipe_dead_refs",
                           "params": {"dry_run": True}})
    assert resp["ok"]
    assert resp["result"]["dry_run"] is True
    assert resp["result"]["proposals"] == {pr.id: [cid]}

    resp = handle_request({"id": "r2", "method": "kb.wipe_dead_refs", "params": {}})
    assert resp["ok"]
    assert resp["result"]["dropped"] == 1
