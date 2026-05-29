"""Tests for visibility/project/agent scope fields on Claim and Source."""
import pytest

from vouch.models import Visibility
from vouch.proposals import propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path):
    return KBStore.init(tmp_path)


@pytest.fixture
def source_id(store):
    return store.put_source(b"evidence").id


def test_propose_claim_with_scope_fields(store, source_id):
    pr = propose_claim(
        store, text="scoped claim", evidence=[source_id],
        proposed_by="agent-a",
        visibility="private", project="proj-x", agent="agent-a",
    )
    assert pr.payload["visibility"] == "private"
    assert pr.payload["project"] == "proj-x"
    assert pr.payload["agent"] == "agent-a"


def test_put_source_with_scope_fields(store):
    src = store.put_source(
        b"data", title="t",
        visibility="team", project="proj-y", agent="agent-b",
    )
    assert src.visibility == Visibility.TEAM
    assert src.project == "proj-y"
    assert src.agent == "agent-b"


def test_list_claims_filter_by_project(store, source_id):
    from vouch.proposals import approve, propose_claim
    pr1 = propose_claim(store, text="claim A", evidence=[source_id],
                        proposed_by="u", project="alpha")
    pr2 = propose_claim(store, text="claim B", evidence=[source_id],
                        proposed_by="u", project="beta")
    approve(store, pr1.id, approved_by="reviewer")
    approve(store, pr2.id, approved_by="reviewer")
    alpha = store.list_claims(project="alpha")
    assert len(alpha) == 1
    assert alpha[0].text == "claim A"


def test_list_sources_filter_by_visibility(store):
    store.put_source(b"pub", title="pub", visibility="public")
    store.put_source(b"priv", title="priv", visibility="private")
    public = store.list_sources(visibility="public")
    assert len(public) == 1
    assert public[0].title == "pub"


def test_scope_fields_default_none(store, source_id):
    pr = propose_claim(store, text="no scope", evidence=[source_id],
                       proposed_by="u")
    assert pr.payload.get("visibility") is None
    assert pr.payload.get("project") is None
    assert pr.payload.get("agent") is None
