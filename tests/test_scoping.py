"""VEP-0005 richer scopes — visibility and retrieval filtering."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import context, health
from vouch.models import ArtifactScope, Claim, Visibility
from vouch.scoping import ViewerContext, filter_hits, is_visible, viewer_from
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_legacy_scope_string_parses_on_claim() -> None:
    claim = Claim.model_validate({
        "id": "c1",
        "text": "x",
        "evidence": ["s1"],
        "scope": "project",
    })
    assert claim.scope.visibility == Visibility.PROJECT
    assert claim.scope.project is None
    assert claim.scope.agent is None


def test_structured_scope_parses_on_claim() -> None:
    claim = Claim.model_validate({
        "id": "c1",
        "text": "x",
        "evidence": ["s1"],
        "scope": {
            "visibility": "private",
            "project": "billing",
            "agent": "claude-cli",
        },
    })
    assert claim.scope.visibility == Visibility.PRIVATE
    assert claim.scope.project == "billing"
    assert claim.scope.agent == "claude-cli"


def test_is_visible_matrix() -> None:
    public = ArtifactScope(visibility=Visibility.PUBLIC)
    team = ArtifactScope(visibility=Visibility.TEAM)
    unbound = ArtifactScope(visibility=Visibility.PROJECT)
    billing = ArtifactScope(visibility=Visibility.PRIVATE, agent="alice")
    billing_proj = ArtifactScope(visibility=Visibility.PROJECT, project="billing")
    other_proj = ArtifactScope(visibility=Visibility.PROJECT, project="other")

    default = ViewerContext()
    billing_viewer = ViewerContext(project="billing")
    alice = ViewerContext(agent="alice")

    assert is_visible(public, default)
    assert is_visible(team, default)
    assert is_visible(unbound, default)
    assert not is_visible(billing, default)
    assert not is_visible(billing_proj, default)
    assert not is_visible(other_proj, default)

    assert is_visible(billing_proj, billing_viewer)
    assert not is_visible(other_proj, billing_viewer)
    assert is_visible(unbound, billing_viewer)

    assert is_visible(billing, alice)
    assert not is_visible(billing, ViewerContext(agent="bob"))


def test_private_fail_closed_without_agent() -> None:
    scope = ArtifactScope(visibility=Visibility.PRIVATE, agent="alice")
    assert not is_visible(scope, ViewerContext())
    assert not is_visible(scope, ViewerContext(agent=None))


def test_viewer_from_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = KBStore.init(tmp_path)
    store.config_path.write_text(
        "retrieval:\n  scope:\n    project: from-config\n    agent: cfg-agent\n"
    )
    monkeypatch.setenv("VOUCH_PROJECT", "from-env")
    monkeypatch.setenv("VOUCH_AGENT", "env-agent")

    explicit = viewer_from(
        config_path=store.config_path,
        project="from-param",
        agent="param-agent",
    )
    assert explicit == ViewerContext(project="from-param", agent="param-agent")

    from_env = viewer_from(config_path=store.config_path)
    assert from_env == ViewerContext(project="from-env", agent="env-agent")

    monkeypatch.delenv("VOUCH_PROJECT", raising=False)
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    from_config = viewer_from(config_path=store.config_path)
    assert from_config == ViewerContext(project="from-config", agent="cfg-agent")


def test_search_filters_project_bound_claims(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="shared",
        text="shared auth design",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT),
    ))
    store.put_claim(Claim(
        id="billing-only",
        text="billing auth design",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="billing"),
    ))
    health.rebuild_index(store)

    default_hits = filter_hits(
        store,
        [("claim", "shared", "shared auth design", 1.0),
         ("claim", "billing-only", "billing auth design", 1.0)],
        ViewerContext(),
    )
    assert [h[1] for h in default_hits] == ["shared"]

    billing_hits = filter_hits(
        store,
        [("claim", "shared", "shared auth design", 1.0),
         ("claim", "billing-only", "billing auth design", 1.0)],
        ViewerContext(project="billing"),
    )
    assert {h[1] for h in billing_hits} == {"shared", "billing-only"}


def test_context_pack_respects_private_scope(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="public-note",
        text="team jwt policy",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT),
    ))
    store.put_claim(Claim(
        id="alice-scratch",
        text="team jwt scratch",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PRIVATE, agent="alice"),
    ))
    health.rebuild_index(store)

    pack = context.build_context_pack(
        store, query="team jwt", limit=5, agent="alice",
    )
    ids = {it["id"] for it in pack["items"]}
    assert "public-note" in ids
    assert "alice-scratch" in ids

    pack_other = context.build_context_pack(store, query="team jwt", limit=5)
    ids_other = {it["id"] for it in pack_other["items"]}
    assert "public-note" in ids_other
    assert "alice-scratch" not in ids_other


def test_artifact_scope_for_object_id_resolves_claim_and_proposal(store: KBStore) -> None:
    from vouch.proposals import propose_claim
    from vouch.scoping import artifact_scope_for_object_id

    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="scoped-claim",
        text="x",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="billing"),
    ))
    assert artifact_scope_for_object_id(store, "scoped-claim") == ArtifactScope(
        visibility=Visibility.PROJECT, project="billing",
    )

    pr = propose_claim(
        store, text="pending", evidence=[src.id], proposed_by="a",
        slug_hint="pending-claim", dry_run=True,
    )
    pr.proposal.payload["scope"] = {"visibility": "project", "project": "platform"}
    store.put_proposal(pr.proposal)
    assert artifact_scope_for_object_id(store, pr.proposal.id) == ArtifactScope(
        visibility=Visibility.PROJECT, project="platform",
    )

    assert artifact_scope_for_object_id(store, "unknown-page-id") is None


def test_default_kb_behavior_unchanged(store: KBStore) -> None:
    """Claims with legacy default scope stay visible to the default viewer."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="jwt tokens", evidence=[src.id]))
    health.rebuild_index(store)
    pack = context.build_context_pack(store, query="jwt", limit=5)
    assert any(it["id"] == "c1" for it in pack["items"])
