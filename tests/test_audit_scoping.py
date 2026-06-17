"""Visibility-aware audit reads — issue #232."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from vouch import audit
from vouch.models import ArtifactScope, Claim, Visibility
from vouch.proposals import approve, propose_claim
from vouch.scoping import (
    ViewerContext,
    artifact_scope_for_object_id,
    event_visible_to_viewer,
    filter_audit_events,
    viewer_from_params,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _claim_event(store: KBStore, claim_id: str, *, actor: str = "agent") -> None:
    audit.log_event(
        store.kb_dir,
        event="claim.create",
        actor=actor,
        object_ids=[claim_id],
    )


def test_audit_events_without_object_ids_always_visible(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="kb.init", actor="setup")
    audit.log_event(store.kb_dir, event="index.rebuild", actor="setup", object_ids=[])

    events = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="billing"),
    ))
    assert {e.event for e in events} == {"kb.init", "index.rebuild"}


def test_two_project_kb_reviewer_sees_only_own_events(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    store.put_claim(Claim(
        id="shared-design",
        text="shared auth",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT),
    ))
    store.put_claim(Claim(
        id="billing-secret",
        text="billing auth",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="billing"),
    ))
    store.put_claim(Claim(
        id="platform-secret",
        text="platform auth",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="platform"),
    ))

    _claim_event(store, "shared-design")
    _claim_event(store, "billing-secret")
    _claim_event(store, "platform-secret")

    billing_events = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="billing"),
    ))
    billing_ids = {oid for e in billing_events for oid in e.object_ids}
    assert "billing-secret" in billing_ids
    assert "shared-design" in billing_ids
    assert "platform-secret" not in billing_ids

    platform_events = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="platform"),
    ))
    platform_ids = {oid for e in platform_events for oid in e.object_ids}
    assert "platform-secret" in platform_ids
    assert "billing-secret" not in platform_ids


def test_proposal_events_respect_payload_scope(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    pr = propose_claim(
        store,
        text="billing-only proposal",
        evidence=[src.id],
        proposed_by="agent",
        slug_hint="billing-proposal-claim",
        dry_run=True,
    )
    pr.proposal.payload["scope"] = {
        "visibility": "project",
        "project": "billing",
    }
    store.put_proposal(pr.proposal)
    audit.log_event(
        store.kb_dir,
        event="proposal.claim.create",
        actor="agent",
        object_ids=[pr.proposal.id],
    )

    billing = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="billing"),
    ))
    assert any(pr.proposal.id in e.object_ids for e in billing)

    platform = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="platform"),
    ))
    assert not any(pr.proposal.id in e.object_ids for e in platform)


def test_private_claim_hidden_without_agent(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    store.put_claim(Claim(
        id="alice-note",
        text="private scratch",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PRIVATE, agent="alice"),
    ))
    _claim_event(store, "alice-note")

    default_events = list(audit.read_events(store.kb_dir, store=store, viewer=ViewerContext()))
    assert not any("alice-note" in e.object_ids for e in default_events)

    alice_events = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(agent="alice"),
    ))
    assert any("alice-note" in e.object_ids for e in alice_events)


def test_approve_event_hidden_when_result_claim_is_foreign(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    pr = propose_claim(
        store,
        text="billing scoped claim",
        evidence=[src.id],
        proposed_by="agent",
        slug_hint="billing-approved",
        dry_run=True,
    )
    pr.proposal.payload["scope"] = {"visibility": "project", "project": "billing"}
    store.put_proposal(pr.proposal)
    claim = approve(store, pr.proposal.id, approved_by="reviewer")
    assert claim.id == "billing-approved"

    platform = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="platform"),
    ))
    platform_object_ids = {oid for e in platform for oid in e.object_ids}
    assert claim.id not in platform_object_ids
    assert pr.proposal.id not in platform_object_ids


def test_unscoped_page_events_remain_visible(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="page.create", actor="r", object_ids=["auth-design"])
    events = list(audit.read_events(
        store.kb_dir,
        store=store,
        viewer=ViewerContext(project="billing"),
    ))
    assert any("auth-design" in e.object_ids for e in events)


def test_read_events_requires_store_when_viewer_set(store: KBStore) -> None:
    with pytest.raises(ValueError, match="requires store"):
        list(audit.read_events(store.kb_dir, viewer=ViewerContext(project="a")))


def test_viewer_from_params_accepts_nested_viewer_scope(store: KBStore) -> None:
    viewer = viewer_from_params(store, {"viewer_scope": {"project": "billing", "agent": "bot"}})
    assert viewer == ViewerContext(project="billing", agent="bot")


def test_fuzz_no_project_leak_across_viewers(store: KBStore) -> None:
    """Mirrors gbrain zero-leaks: no viewer sees foreign project-bound events."""
    rng = random.Random(232)
    projects = ["alpha", "beta", "gamma", "delta"]
    src = store.put_source(b"shared evidence for fuzz audit scoping")
    claim_scopes: dict[str, ArtifactScope] = {}

    for i in range(40):
        vis_roll = rng.random()
        if vis_roll < 0.25:
            scope = ArtifactScope(visibility=Visibility.PROJECT)
        elif vis_roll < 0.75:
            scope = ArtifactScope(
                visibility=Visibility.PROJECT,
                project=rng.choice(projects),
            )
        else:
            scope = ArtifactScope(
                visibility=Visibility.PRIVATE,
                agent=rng.choice(["alice", "bob", "carol"]),
            )
        cid = f"claim-{i}"
        store.put_claim(Claim(id=cid, text=f"claim {i}", evidence=[src.id], scope=scope))
        claim_scopes[cid] = scope
        _claim_event(store, cid)

    audit.log_event(store.kb_dir, event="kb.init", actor="fuzz")

    viewers = [ViewerContext()] + [
        ViewerContext(project=p) for p in projects
    ] + [
        ViewerContext(agent=a) for a in ("alice", "bob", "carol")
    ]

    for viewer in viewers:
        visible = list(audit.read_events(store.kb_dir, store=store, viewer=viewer))
        for event in visible:
            for oid in event.object_ids:
                scope = artifact_scope_for_object_id(store, oid)
                if scope is None:
                    continue
                assert event_visible_to_viewer(store, event, viewer)
                assert filter_audit_events(store, [event], viewer) == [event]
                if (
                    scope.visibility == Visibility.PROJECT
                    and scope.project is not None
                    and viewer.project is not None
                ):
                    assert scope.project == viewer.project
                if (
                    scope.visibility == Visibility.PRIVATE
                    and scope.agent is not None
                    and viewer.agent is not None
                ):
                    assert scope.agent == viewer.agent


def test_jsonl_audit_returns_scoped_envelope(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch.jsonl_server import handle_request

    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="billing-only",
        text="billing",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="billing"),
    ))
    _claim_event(store, "billing-only")
    monkeypatch.chdir(store.root)

    resp = handle_request({
        "id": "audit-1",
        "method": "kb.audit",
        "params": {"tail": 50, "viewer_scope": {"project": "platform"}},
    })
    assert resp["ok"]
    result = resp["result"]
    assert result["viewer"]["project"] == "platform"
    object_ids = {oid for e in result["events"] for oid in e["object_ids"]}
    assert "billing-only" not in object_ids
