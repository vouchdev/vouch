"""Cascade delete: referrer edits ride along in the delete proposal (#600).

The "block if referenced" gate stays exactly where it was. `cascade=True`
changes what the reviewer is asked to approve — the target *and* the edits
that free it — rather than lowering the bar for approving a delete.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import audit
from vouch.cli import cli
from vouch.jsonl_server import handle_request
from vouch.models import (
    Claim,
    Entity,
    EntityType,
    Page,
    ProposalStatus,
    Relation,
    RelationType,
)
from vouch.proposals import (
    ProposalError,
    _apply_cascade,
    approve,
    cascade_plan,
    check_approvable,
    propose_delete,
    referenced_by,
)
from vouch.server import kb_propose_delete
from vouch.storage import ArtifactNotFoundError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _claim(store: KBStore, cid: str = "c1", text: str = "a claim", **kw) -> Claim:
    src = store.put_source(b"src-bytes-" + cid.encode())
    return store.put_claim(Claim(id=cid, text=text, evidence=[src.id], **kw))


def _decide(store: KBStore, proposal_id: str) -> None:
    approve(store, proposal_id, approved_by="reviewer")


def _events(store: KBStore) -> list[str]:
    return [e.event for e in audit.read_events(store.kb_dir)]


# --- today's behaviour is untouched ---------------------------------------


def test_referenced_delete_is_still_refused_without_cascade(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    with pytest.raises(ProposalError) as e:
        propose_delete(
            store, target_kind="claim", target_id="c1", proposed_by="agent"
        )
    assert "referenced by" in str(e.value)


def test_refusal_names_the_cascade_flag(store: KBStore) -> None:
    """The dead end has to be discoverable, or the flag may as well not exist."""
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    with pytest.raises(ProposalError) as e:
        propose_delete(
            store, target_kind="claim", target_id="c1", proposed_by="agent"
        )
    assert "cascade" in str(e.value)
    assert "--cascade" in str(e.value)


def test_unreferenced_delete_files_no_cascade_key(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent"
    )
    assert "cascade" not in pr.payload


def test_cascade_on_an_unreferenced_target_is_an_empty_plan(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    assert pr.payload["cascade"] == []
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")


# --- the plan --------------------------------------------------------------


def test_plan_mirrors_referenced_by_for_a_page_cited_claim(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    assert referenced_by(store, "claim", "c1") == ["page 'p1'"]
    assert cascade_plan(store, "claim", "c1") == [
        {"kind": "page", "id": "p1", "unlink_claims": ["c1"]}
    ]


def test_plan_and_apply_unlink_goal_cited_claims(store: KBStore) -> None:
    """Goals are first-class claim referrers after #427 — cascade must free them."""
    from vouch.models import Goal

    _claim(store, "c1")
    store.put_goal(Goal(id="keep-c1", title="keep c1", claims=["c1"]))
    assert referenced_by(store, "claim", "c1") == ["goal 'keep-c1'"]
    assert cascade_plan(store, "claim", "c1") == [
        {"kind": "goal", "id": "keep-c1", "unlink_claims": ["c1"]}
    ]
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent",
        cascade=True,
    )
    approve(store, pr.id, approved_by="reviewer")
    assert store.get_goal("keep-c1").claims == []
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")
    assert "goal.cascade_unlink" in _events(store)


def test_plan_unlinks_goal_cited_entities(store: KBStore) -> None:
    from vouch.models import Goal

    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    store.put_goal(Goal(id="track-e1", title="track e1", entities=["e1"]))
    assert cascade_plan(store, "entity", "e1") == [
        {"kind": "goal", "id": "track-e1", "unlink_entities": ["e1"]}
    ]


def test_plan_deletes_relations_and_unlinks_pages(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    plan = cascade_plan(store, "claim", "c1")
    assert {"kind": "page", "id": "p1", "unlink_claims": ["c1"]} in plan
    assert {
        "kind": "relation", "id": "c1--supports--c2", "action": "delete"
    } in plan


def test_plan_skips_claims_that_do_not_reference_the_target(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    assert cascade_plan(store, "claim", "c1") == []


def test_plan_rejects_an_unknown_kind(store: KBStore) -> None:
    with pytest.raises(ProposalError):
        cascade_plan(store, "sandwich", "c1")


# --- approving a cascade ---------------------------------------------------


def test_page_cited_claim_is_deletable_with_cascade(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="body [claim: c1] tail", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    assert pr.status is ProposalStatus.PENDING
    # nothing has moved yet — filing a proposal is not a write
    assert store.get_page("p1").claims == ["c1"]
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")
    assert store.get_page("p1").claims == []


def test_cascade_strips_the_inline_body_marker_too(store: KBStore) -> None:
    """Frontmatter alone would leave the body rendering a dead citation."""
    _claim(store, "c1")
    store.put_page(Page(
        id="p1", title="P", body="before [claim: c1] after", claims=["c1"],
    ))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    assert "[claim: c1]" not in store.get_page("p1").body
    assert "before" in store.get_page("p1").body


def test_supersede_pair_is_no_longer_mutually_locked(store: KBStore) -> None:
    """Neither end of a supersede chain could be removed before this."""
    _claim(store, "old")
    _claim(store, "new", supersedes=["old"])
    store.update_claim(
        store.get_claim("old").model_copy(update={"superseded_by": "new"})
    )
    assert referenced_by(store, "claim", "old")
    assert referenced_by(store, "claim", "new")
    pr = propose_delete(
        store, target_kind="claim", target_id="old", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("old")
    assert store.get_claim("new").supersedes == []


def test_cascade_clears_superseded_by_on_the_surviving_claim(store: KBStore) -> None:
    _claim(store, "old")
    _claim(store, "new", supersedes=["old"])
    store.update_claim(
        store.get_claim("old").model_copy(update={"superseded_by": "new"})
    )
    pr = propose_delete(
        store, target_kind="claim", target_id="new", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    assert store.get_claim("old").superseded_by is None


def test_cascade_unlinks_contradicts(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2", contradicts=["c1"])
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    assert store.get_claim("c2").contradicts == []


def test_cascade_deletes_the_relations_that_pointed_at_the_target(
    store: KBStore,
) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_relation("c1--supports--c2")
    # the far endpoint is untouched — only edges are collateral, not nodes
    assert store.get_claim("c2").id == "c2"


def test_entity_cascade_unlinks_claims_and_pages(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    _claim(store, "c1", entities=["e1"])
    store.put_page(Page(id="p1", title="P", body="x", entities=["e1"]))
    pr = propose_delete(
        store, target_kind="entity", target_id="e1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_entity("e1")
    assert store.get_claim("c1").entities == []
    assert store.get_page("p1").entities == []


def test_page_cascade_deletes_relations_pointing_at_it(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="x"))
    _claim(store, "c1")
    store.put_relation(Relation(
        id="c1--supports--p1", source="c1",
        relation=RelationType.SUPPORTS, target="p1",
    ))
    pr = propose_delete(
        store, target_kind="page", target_id="p1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_page("p1")
    with pytest.raises(ArtifactNotFoundError):
        store.get_relation("c1--supports--p1")


# --- re-derivation at approve time -----------------------------------------


def test_a_referrer_added_after_propose_is_still_unlinked(store: KBStore) -> None:
    """The plan is re-derived at approve, exactly as refs are re-checked."""
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    store.put_page(Page(id="p2", title="P2", body="y", claims=["c1"]))
    _decide(store, pr.id)
    assert store.get_page("p2").claims == []
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")


def test_a_referrer_removed_before_approve_is_not_fatal(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    store.delete_page("p1")
    _decide(store, pr.id)
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")


def test_payload_keeps_the_plan_the_reviewer_saw(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    store.put_page(Page(id="p2", title="P2", body="y", claims=["c1"]))
    stored = store.get_proposal(pr.id)
    assert stored.payload["cascade"] == [
        {"kind": "page", "id": "p1", "unlink_claims": ["c1"]}
    ]


# --- the applier is idempotent under a stale plan ---------------------------
#
# approve() re-derives the plan, so these branches are not reachable through
# the public path — they exist for the narrow race where a concurrent writer
# changes a referrer between derivation and application, and for a crash-retry
# of approve(). Exercised directly, because that is the only honest way to
# reach them.


def test_applier_skips_a_page_that_vanished(store: KBStore) -> None:
    assert _apply_cascade(
        store, [{"kind": "page", "id": "gone", "unlink_claims": ["c1"]}],
        actor="reviewer",
    ) == []


def test_applier_skips_a_page_already_unlinked(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="x"))
    assert _apply_cascade(
        store, [{"kind": "page", "id": "p1", "unlink_claims": ["c1"]}],
        actor="reviewer",
    ) == []
    assert "page.cascade_unlink" not in _events(store)


def test_applier_skips_a_claim_that_vanished(store: KBStore) -> None:
    assert _apply_cascade(
        store, [{"kind": "claim", "id": "gone", "unlink_supersedes": ["c1"]}],
        actor="reviewer",
    ) == []


def test_applier_skips_a_claim_already_unlinked(store: KBStore) -> None:
    _claim(store, "c2")
    assert _apply_cascade(
        store, [{"kind": "claim", "id": "c2", "unlink_supersedes": ["c1"]}],
        actor="reviewer",
    ) == []
    assert "claim.cascade_unlink" not in _events(store)


def test_applier_skips_a_goal_that_vanished(store: KBStore) -> None:
    assert _apply_cascade(
        store, [{"kind": "goal", "id": "gone", "unlink_claims": ["c1"]}],
        actor="reviewer",
    ) == []


def test_applier_skips_a_goal_already_unlinked(store: KBStore) -> None:
    from vouch.models import Goal

    store.put_goal(Goal(id="g1", title="empty refs"))
    assert _apply_cascade(
        store, [{"kind": "goal", "id": "g1", "unlink_claims": ["c1"]}],
        actor="reviewer",
    ) == []
    assert "goal.cascade_unlink" not in _events(store)


def test_applier_skips_a_relation_that_vanished(store: KBStore) -> None:
    assert _apply_cascade(
        store, [{"kind": "relation", "id": "gone", "action": "delete"}],
        actor="reviewer",
    ) == []


def test_applier_ignores_a_malformed_step(store: KBStore) -> None:
    assert _apply_cascade(store, ["not-a-dict"], actor="reviewer") == []  # type: ignore[list-item]


def test_applier_ignores_a_step_without_an_id(store: KBStore) -> None:
    assert _apply_cascade(store, [{"kind": "page"}], actor="reviewer") == []


def test_applier_ignores_an_unknown_step_kind(store: KBStore) -> None:
    assert _apply_cascade(
        store, [{"kind": "source", "id": "s1"}], actor="reviewer"
    ) == []


# --- the gate --------------------------------------------------------------


def test_check_approvable_allows_a_cascade_proposal(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    assert check_approvable(store, pr.id, approved_by="reviewer") is None


def test_check_approvable_still_blocks_a_plain_referenced_delete(
    store: KBStore,
) -> None:
    """A pre-cascade proposal whose target gained a referrer stays blocked."""
    _claim(store, "c1")
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent"
    )
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    reason = check_approvable(store, pr.id, approved_by="reviewer")
    assert reason is not None and "referenced by" in reason


def test_cascade_does_not_bypass_self_approval(store: KBStore) -> None:
    """On a human-reviewed KB the second pair of eyes is still required."""
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    cfg["review"]["approver_role"] = "human"
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    with pytest.raises(ProposalError, match="forbidden_self_approval"):
        approve(store, pr.id, approved_by="agent")
    # refused → nothing was unlinked
    assert store.get_page("p1").claims == ["c1"]
    assert store.get_claim("c1").id == "c1"


def test_proposing_a_cascade_writes_nothing(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="b [claim: c1]", claims=["c1"]))
    before = _events(store)
    propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    assert store.get_page("p1").claims == ["c1"]
    assert "[claim: c1]" in store.get_page("p1").body
    assert store.get_claim("c1").id == "c1"
    assert [e for e in _events(store) if e.endswith("cascade_unlink")] == []
    assert len(_events(store)) == len(before) + 1  # the proposal event only


# --- audit -----------------------------------------------------------------


def test_every_cascade_edit_lands_an_audit_event(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    events = _events(store)
    assert "page.cascade_unlink" in events
    assert "relation.delete" in events
    assert "claim.delete" in events


def test_the_delete_event_names_what_the_cascade_touched(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    deletes = [
        e for e in audit.read_events(store.kb_dir) if e.event == "claim.delete"
    ]
    assert deletes[-1].data["cascaded"] == ["p1"]


def test_cascade_edits_are_irreversible_in_the_log(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c1", proposed_by="agent", cascade=True
    )
    _decide(store, pr.id)
    unlinks = [
        e for e in audit.read_events(store.kb_dir)
        if e.event == "page.cascade_unlink"
    ]
    assert unlinks and unlinks[-1].reversible is False


# --- surfaces --------------------------------------------------------------


def test_mcp_surface_accepts_cascade(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    out = kb_propose_delete(
        target_kind="claim", target_id="c1", cascade=True
    )
    assert out["status"] == ProposalStatus.PENDING.value
    assert store.get_proposal(out["proposal_id"]).payload["cascade"]


def test_mcp_surface_without_cascade_still_refuses(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    with pytest.raises(ValueError, match="referenced by"):
        kb_propose_delete(target_kind="claim", target_id="c1")


def test_jsonl_surface_accepts_cascade(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    resp = handle_request({
        "id": "1", "method": "kb.propose_delete",
        "params": {"target_kind": "claim", "target_id": "c1", "cascade": True},
    })
    assert resp["ok"] is True
    assert store.get_proposal(resp["result"]["proposal_id"]).payload["cascade"]


def test_jsonl_surface_without_cascade_returns_the_error_envelope(
    store: KBStore, monkeypatch
) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    resp = handle_request({
        "id": "1", "method": "kb.propose_delete",
        "params": {"target_kind": "claim", "target_id": "c1"},
    })
    assert resp["ok"] is False
    assert "referenced by" in resp["error"]["message"]


def test_cli_cascade_flag(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    runner = CliRunner()
    res = runner.invoke(cli, ["propose-delete", "claim", "c1", "--cascade"])
    assert res.exit_code == 0, res.output
    assert store.get_proposal(res.output.strip()).payload["cascade"]


def test_cli_without_cascade_is_a_clean_error(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="x", claims=["c1"]))
    runner = CliRunner()
    res = runner.invoke(cli, ["propose-delete", "claim", "c1"])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "--cascade" in res.output
