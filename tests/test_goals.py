from __future__ import annotations

from pathlib import Path

import pytest

from vouch import lifecycle
from vouch.models import Goal, GoalStatus, ProposalKind, ProposalStatus
from vouch.proposals import approve, propose_goal
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_goal_model_rejects_whitespace_title() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Goal(id="g1", title="   ")


def test_propose_goal_then_approve(store: KBStore) -> None:
    pr = propose_goal(
        store,
        title="finish typed config migration",
        detail="replace ad-hoc dict reads with typed settings access",
        proposed_by="agent",
        tags=["migration"],
    )
    assert pr.kind == ProposalKind.GOAL
    approved = approve(store, pr.id, approved_by="reviewer")
    assert isinstance(approved, Goal)
    assert approved.status == GoalStatus.OPEN
    assert store.get_goal(approved.id).title == "finish typed config migration"
    decided = store.get_proposal(pr.id)
    assert decided.status == ProposalStatus.APPROVED


def test_goal_status_transitions_write_decided_and_audit(store: KBStore) -> None:
    pr = propose_goal(store, title="ship v1.2", proposed_by="agent")
    goal = approve(store, pr.id, approved_by="reviewer")
    lifecycle.goal_set_status(
        store,
        goal_id=goal.id,
        status=GoalStatus.BLOCKED,
        actor="reviewer",
        reason="waiting on release checks",
    )
    updated = store.get_goal(goal.id)
    assert updated.status == GoalStatus.BLOCKED
    decided = store.list_proposals(ProposalStatus.APPROVED)
    assert any(
        p.kind == ProposalKind.GOAL
        and p.payload.get("id") == goal.id
        and p.payload.get("action") == "goal.blocked"
        for p in decided
    )


def test_jsonl_goal_flow(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch.jsonl_server import handle_request

    monkeypatch.chdir(store.root)
    proposed = handle_request({
        "id": "1",
        "method": "kb.propose_goal",
        "params": {"title": "reduce flaky tests"},
    })
    assert proposed["ok"]
    pid = proposed["result"]["proposal_id"]
    monkeypatch.setenv("VOUCH_AGENT", "reviewer")
    approved = handle_request({
        "id": "2",
        "method": "kb.approve",
        "params": {"proposal_id": pid},
    })
    assert approved["ok"]
    listed = handle_request({
        "id": "3",
        "method": "kb.list_goals",
        "params": {},
    })
    assert listed["ok"]
    assert len(listed["result"]) == 1
    assert listed["result"][0]["status"] == "open"
    moved = handle_request({
        "id": "4",
        "method": "kb.goal_set_status",
        "params": {"goal_id": listed["result"][0]["id"], "status": "done"},
    })
    assert moved["ok"]
    assert moved["result"]["status"] == "done"

