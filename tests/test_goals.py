"""Goals — review-gated in-flight objectives (#427).

The load-bearing invariants here are the same two the north star names: a
goal cannot exist without passing `proposals.approve`, and its status cannot
move except through `lifecycle.set_goal_status`, which is what puts the
transition in the audit log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from vouch import audit, digest, recall
from vouch import goals as goals_mod
from vouch import lifecycle as life
from vouch.capabilities import capabilities
from vouch.jsonl_server import HANDLERS
from vouch.lifecycle import LifecycleError
from vouch.models import Goal, GoalStatus, ProposalKind, ProposalStatus
from vouch.proposals import ProposalError, approve, propose_claim, propose_goal
from vouch.storage import ArtifactNotFoundError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    s.config_path.write_text(
        "review:\n  approver_role: trusted-agent\n", encoding="utf-8",
    )
    return s


def _approved_goal(store: KBStore, title: str = "migrate config to typed loader") -> Goal:
    pr = propose_goal(store, title=title, proposed_by="agent")
    approve(store, pr.id, approved_by="reviewer")
    return store.get_goal(pr.payload["id"])


# --- the gate --------------------------------------------------------------


def test_propose_goal_creates_a_pending_proposal_not_a_goal(store: KBStore) -> None:
    pr = propose_goal(
        store, title="migrate config to typed loader", proposed_by="agent"
    )
    assert pr.kind is ProposalKind.GOAL
    assert pr.status is ProposalStatus.PENDING
    with pytest.raises(ArtifactNotFoundError):
        store.get_goal(pr.payload["id"])
    assert store.list_goals() == []


def test_approve_writes_the_goal_open(store: KBStore) -> None:
    goal = _approved_goal(store)
    assert goal.status is GoalStatus.OPEN
    assert goal.approved_by == "reviewer"
    assert goal.title == "migrate config to typed loader"


def test_dry_run_proposal_touches_nothing(store: KBStore) -> None:
    pr = propose_goal(
        store, title="ship the audit-race fix", proposed_by="agent", dry_run=True
    )
    assert store.list_proposals(ProposalStatus.PENDING) == []
    with pytest.raises(ArtifactNotFoundError):
        store.get_proposal(pr.id)


def test_empty_title_is_rejected_at_the_model_layer(store: KBStore) -> None:
    """#155 posture: the model, not just the propose helper, is the gate."""
    with pytest.raises(ProposalError):
        propose_goal(store, title="   ", proposed_by="agent")
    with pytest.raises(ValueError):
        Goal(id="g", title="  ")


def test_proposal_cannot_land_a_goal_that_is_already_done(store: KBStore) -> None:
    """A non-open payload would put a transition on disk that skipped the
    lifecycle write path, so the audit log would never carry it."""
    pr = propose_goal(store, title="already finished", proposed_by="agent")
    proposal = store.get_proposal(pr.id)
    proposal.payload["status"] = "done"
    store._proposal_path(proposal.id).write_text(
        yaml.safe_dump(proposal.model_dump(mode="json")), encoding="utf-8",
    )
    with pytest.raises(ProposalError, match="only 'open' may be approved"):
        approve(store, pr.id, approved_by="reviewer")


def test_goal_refs_must_resolve(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown claim id"):
        propose_goal(
            store, title="finish the thing", proposed_by="agent", claims=["nope"]
        )
    with pytest.raises(ProposalError, match="unknown entity id"):
        propose_goal(
            store, title="finish the thing", proposed_by="agent", entities=["nope"]
        )


def test_goal_can_cite_an_approved_claim(store: KBStore) -> None:
    src = store.put_source(b"the release is blocked on the audit-race fix")
    claim_pr = propose_claim(
        store, text="the release is blocked", evidence=[src.id], proposed_by="agent"
    )
    claim = approve(store, claim_pr.id, approved_by="reviewer")
    pr = propose_goal(
        store, title="unblock the release", proposed_by="agent", claims=[claim.id]
    )
    approve(store, pr.id, approved_by="reviewer")
    assert store.get_goal(pr.payload["id"]).claims == [claim.id]


# --- transitions -----------------------------------------------------------


def test_status_transition_appends_to_the_audit_log(store: KBStore) -> None:
    goal = _approved_goal(store)
    moved = life.set_goal_status(
        store, goal_id=goal.id, status="blocked", actor="human", reason="waiting on ci"
    )
    assert moved.status is GoalStatus.BLOCKED
    assert store.get_goal(goal.id).status is GoalStatus.BLOCKED

    events = [e for e in audit.read_events(store.kb_dir) if e.event == "goal.status"]
    assert len(events) == 1
    assert events[0].data == {
        "from": "open", "to": "blocked", "reason": "waiting on ci",
    }
    assert events[0].actor == "human"
    assert goal.id in events[0].object_ids


def test_transition_records_history_on_the_goal(store: KBStore) -> None:
    goal = _approved_goal(store)
    life.set_goal_status(store, goal_id=goal.id, status="blocked", actor="human")
    life.set_goal_status(store, goal_id=goal.id, status="done", actor="human")
    reread = store.get_goal(goal.id)
    assert [(h["from"], h["to"]) for h in reread.history] == [
        ("open", "blocked"), ("blocked", "done"),
    ]
    assert reread.closed_at is not None


def test_reopening_clears_closed_at(store: KBStore) -> None:
    goal = _approved_goal(store)
    life.set_goal_status(store, goal_id=goal.id, status="done", actor="human")
    reopened = life.set_goal_status(
        store, goal_id=goal.id, status="open", actor="human", reason="not actually done"
    )
    assert reopened.closed_at is None
    assert reopened.status is GoalStatus.OPEN


def test_unknown_status_and_no_op_transition_are_refused(store: KBStore) -> None:
    goal = _approved_goal(store)
    with pytest.raises(LifecycleError, match="unknown goal status"):
        life.set_goal_status(store, goal_id=goal.id, status="shipped", actor="human")
    with pytest.raises(LifecycleError, match="already open"):
        life.set_goal_status(store, goal_id=goal.id, status="open", actor="human")
    # a refused transition writes nothing
    assert not [
        e for e in audit.read_events(store.kb_dir) if e.event == "goal.status"
    ]


def test_only_lifecycle_mutates_a_stored_goal() -> None:
    """`store.update_goal` is the single mutation path, and only
    `lifecycle` may call it (`set_goal_status` and cascade unlink).

    A second module calling storage directly would be a write that skipped
    the audit-log append — exactly the parallel write path the north star
    forbids. Storage and the test suite are excluded: one defines the
    method, the other exercises it.
    """
    src_dir = Path(life.__file__).parent
    callers = sorted(
        path.name
        for path in src_dir.rglob("*.py")
        if path.name != "storage.py"
        and "update_goal(" in path.read_text(encoding="utf-8")
    )
    assert callers == ["lifecycle.py"], callers


# --- reads -----------------------------------------------------------------


def test_list_goals_defaults_to_open_oldest_first(store: KBStore) -> None:
    first = _approved_goal(store, "older objective")
    second = _approved_goal(store, "newer objective")
    older = store.get_goal(first.id)
    older.created_at = datetime.now(UTC) - timedelta(days=30)
    store.update_goal(older)

    assert [g.id for g in goals_mod.list_goals(store)] == [first.id, second.id]

    life.set_goal_status(store, goal_id=second.id, status="done", actor="human")
    assert [g.id for g in goals_mod.list_goals(store)] == [first.id]
    assert [g.id for g in goals_mod.list_goals(store, status=None)] == [
        first.id, second.id,
    ]
    assert [g.id for g in goals_mod.list_goals(store, status="done")] == [second.id]


def test_open_goals_reach_the_digest_and_session_start_recall(store: KBStore) -> None:
    goal = _approved_goal(store, "migrate config to typed loader")

    d = digest.build(store)
    assert d.open_goals_total == 1
    assert [row.id for row in d.open_goals] == [goal.id]
    assert "open goals" in digest.render_text(d)
    assert goal.title in digest.render_markdown(d)
    assert d.to_dict()["open_goals"][0]["title"] == goal.title

    body = recall.build_digest(store)
    assert "## open goals" in body
    assert goal.title in body

    # a closed goal drops out of both surfaces
    life.set_goal_status(store, goal_id=goal.id, status="done", actor="human")
    assert digest.build(store).open_goals_total == 0
    assert "## open goals" not in recall.build_digest(store)


# --- registration ----------------------------------------------------------


@pytest.mark.parametrize(
    "method", ["kb.propose_goal", "kb.list_goals", "kb.set_goal_status"]
)
def test_goal_methods_registered_on_every_surface(method: str) -> None:
    assert method in set(capabilities().methods)
    assert method in HANDLERS
    from vouch.server import mcp

    assert mcp._tool_manager.get_tool(method.replace(".", "_")) is not None


def test_cli_exposes_the_goal_commands() -> None:
    from vouch.cli import cli

    names = set(cli.commands)
    assert {"propose-goal", "goals", "goal-status"} <= names


# --- the surfaces, exercised rather than merely registered -----------------


def _cli(store: KBStore, args: list[str]):
    from click.testing import CliRunner

    from vouch.cli import cli

    return CliRunner().invoke(cli, args, env={"VOUCH_KB_PATH": str(store.kb_dir)})


def test_cli_round_trip_propose_list_and_move(store: KBStore) -> None:
    proposed = _cli(store, ["propose-goal", "--title", "ship the typed loader",
                            "--detail", "config.yaml first",
                            "--tag", "infra", "--rationale", "q3 objective"])
    assert proposed.exit_code == 0, proposed.output
    proposal_id = proposed.output.strip()

    # still nothing durable — the gate is the point
    assert "no open goals" in _cli(store, ["goals"]).output

    approve(store, proposal_id, approved_by="reviewer")
    listed = _cli(store, ["goals"])
    assert listed.exit_code == 0, listed.output
    assert "ship the typed loader" in listed.output
    assert "[open]" in listed.output

    goal_id = store.list_goals()[0].id
    moved = _cli(store, ["goal-status", goal_id, "done", "--reason", "shipped"])
    assert moved.exit_code == 0, moved.output
    assert f"{goal_id} -> done" in moved.output

    assert "no open goals" in _cli(store, ["goals"]).output
    assert "[done]" in _cli(store, ["goals", "--status", "all"]).output


def test_cli_reports_an_empty_all_listing(store: KBStore) -> None:
    result = _cli(store, ["goals", "--status", "all"])
    assert result.exit_code == 0
    assert result.output.strip() == "no goals"


def test_mcp_goal_tools_round_trip(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch import server

    monkeypatch.chdir(store.root)
    proposed = server.kb_propose_goal(title="ship the typed loader")
    assert proposed["status"] == "pending"
    approve(store, proposed["proposal_id"], approved_by="reviewer")

    listed = server.kb_list_goals()
    assert [item["title"] for item in listed["items"]] == ["ship the typed loader"]

    goal_id = listed["items"][0]["id"]
    moved = server.kb_set_goal_status(goal_id, "blocked", reason="waiting on #1")
    assert moved["status"] == "blocked"
    assert moved["history"]


def test_mcp_goal_tools_surface_errors_as_value_errors(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The MCP contract: a host sees ValueError, never an internal exception.
    from vouch import server

    monkeypatch.chdir(store.root)
    with pytest.raises(ValueError):
        server.kb_propose_goal(title="   ")
    with pytest.raises(ValueError, match="unknown goal status"):
        server.kb_list_goals(status="nonsense")
    with pytest.raises(ValueError):
        server.kb_set_goal_status("no-such-goal", "done")


def test_jsonl_goal_handlers_round_trip(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch.jsonl_server import handle_request

    monkeypatch.chdir(store.root)
    proposed = handle_request({
        "id": "g1", "method": "kb.propose_goal",
        "params": {"title": "ship the typed loader"},
    })
    assert proposed["ok"] is True
    assert proposed["result"]["kind"] == "goal"
    approve(store, proposed["result"]["proposal_id"], approved_by="reviewer")

    goal_id = store.list_goals()[0].id
    moved = handle_request({
        "id": "g2", "method": "kb.set_goal_status",
        "params": {"goal_id": goal_id, "status": "done"},
    })
    assert moved["ok"] is True
    assert moved["result"]["status"] == "done"
    assert moved["result"]["history"]


# --- the write gates -------------------------------------------------------


def test_an_unapprovable_goal_never_enters_the_queue(store: KBStore) -> None:
    """Validated at propose time for the same reason entities are: a payload
    that can never pass approve() must not sit in the queue waiting for
    someone to notice."""
    with pytest.raises(ProposalError, match="goal title is empty"):
        propose_goal(store, title="   ", proposed_by="agent")
    # and a payload the model rejects for any other reason, e.g. a non-string
    # tag arriving from a transport that did not type-check it
    with pytest.raises(ProposalError, match="invalid goal payload"):
        propose_goal(
            store, title="a real goal", tags=[123],  # type: ignore[list-item]
            proposed_by="agent",
        )
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_approve_refuses_a_goal_payload_corrupted_after_filing(
    store: KBStore
) -> None:
    pr = propose_goal(store, title="a real goal", proposed_by="agent")
    broken = pr.model_copy(deep=True)
    broken.payload["status"] = "not-a-status"
    store.update_proposal(broken)
    with pytest.raises(ProposalError, match="invalid goal payload"):
        approve(store, pr.id, approved_by="reviewer")


def test_approve_refuses_a_goal_citing_an_artifact_that_vanished(
    store: KBStore
) -> None:
    src = store.put_source(b"evidence")
    claim_pr = propose_claim(
        store, text="the loader is typed", evidence=[src.id], proposed_by="agent"
    )
    claim = approve(store, claim_pr.id, approved_by="reviewer")
    pr = propose_goal(
        store, title="finish the loader", claims=[claim.id], proposed_by="agent"
    )
    store._claim_path(claim.id).unlink()  # the artifact goes away before review
    with pytest.raises(ProposalError, match="unknown claim"):
        approve(store, pr.id, approved_by="reviewer")


def test_put_goal_rejects_dangling_references(store: KBStore) -> None:
    with pytest.raises(ValueError, match="unknown claim"):
        store.put_goal(Goal(id="g-a", title="t", claims=["no-such-claim"]))
    with pytest.raises(ValueError, match="unknown entity"):
        store.put_goal(Goal(id="g-b", title="t", entities=["no-such-entity"]))


def test_put_goal_refuses_to_overwrite_an_existing_slug(store: KBStore) -> None:
    store.put_goal(Goal(id="g-dup", title="first"))
    with pytest.raises(ValueError, match="already exists"):
        store.put_goal(Goal(id="g-dup", title="second"))


def test_update_goal_requires_the_goal_to_exist(store: KBStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.update_goal(Goal(id="g-missing", title="t"))


def test_list_goals_on_a_kb_with_no_goals_dir(store: KBStore) -> None:
    # A KB bootstrapped before goals existed: reading must degrade, not raise.
    goals_dir = store.kb_dir / "goals"
    if goals_dir.exists():
        for child in goals_dir.iterdir():
            child.unlink()
        goals_dir.rmdir()
    assert store.list_goals() == []


def test_digest_says_how_many_open_goals_it_elided(store: KBStore) -> None:
    for i in range(6):
        _approved_goal(store, title=f"objective {i}")
    d = digest.build(store, limit=2)
    rendered = digest.render_text(d)
    assert d.open_goals_total == 6
    assert f"... and {d.open_goals_total - len(d.open_goals)} more" in rendered
