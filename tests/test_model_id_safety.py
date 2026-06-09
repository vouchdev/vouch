"""Tests for path-traversal protection on artifact id fields (issue #149)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vouch.models import (
    Claim,
    Entity,
    EntityType,
    Evidence,
    Page,
    Proposal,
    ProposalKind,
    Relation,
    RelationType,
    Session,
    SourceType,
)

_TRAVERSAL_IDS = [
    "../etc/passwd",
    "../../secret",
    "claims/../../../evil",
    "/absolute/path",
    "sub/dir",
    "back\\slash",
    "nul\x00byte",
    "",
    "   ",
]


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_claim_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Claim(id=bad_id, text="some text")


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_entity_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Entity(id=bad_id, name="Some Entity", type=EntityType.CONCEPT)


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_page_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Page(id=bad_id, title="Some Page")


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_relation_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Relation(id=bad_id, source="a", relation=RelationType.USES, target="b")


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_evidence_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Evidence(id=bad_id, source_id="0" * 64, locator="L1")


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_session_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Session(id=bad_id, agent="test-agent")


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_proposal_id_rejects_traversal(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Proposal(id=bad_id, kind=ProposalKind.CLAIM, proposed_by="agent", payload={})


def test_safe_ids_accepted() -> None:
    Claim(id="auth-uses-jwt", text="auth uses JWT")
    Entity(id="proj-foo", name="Foo", type=EntityType.PROJECT)
    Page(id="overview", title="Overview")
    Relation(id="r1", source="a", relation=RelationType.USES, target="b")
    Evidence(id="ev-1", source_id="0" * 64, locator="L10-L20")
    Session(id="sess-abc123", agent="claude-code")
    Proposal(id="prop_001", kind=ProposalKind.CLAIM, proposed_by="agent", payload={})
