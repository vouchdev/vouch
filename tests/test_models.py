"""Tests for model-layer validation constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vouch.models import Claim, Entity, EntityType, Page


# --- Claim.text -----------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n", "\r\n"])
def test_claim_text_rejects_empty(bad: str) -> None:
    with pytest.raises(ValidationError, match="text must not be empty"):
        Claim(id="c1", text=bad)


def test_claim_text_accepts_nonempty() -> None:
    c = Claim(id="c1", text="auth uses JWT")
    assert c.text == "auth uses JWT"


# --- Entity.name ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_entity_name_rejects_empty(bad: str) -> None:
    with pytest.raises(ValidationError, match="name must not be empty"):
        Entity(id="e1", name=bad, type=EntityType.CONCEPT)


def test_entity_name_accepts_nonempty() -> None:
    e = Entity(id="e1", name="My Project", type=EntityType.PROJECT)
    assert e.name == "My Project"


# --- Page.title -----------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_page_title_rejects_empty(bad: str) -> None:
    with pytest.raises(ValidationError, match="title must not be empty"):
        Page(id="p1", title=bad)


def test_page_title_accepts_nonempty() -> None:
    p = Page(id="p1", title="Auth overview")
    assert p.title == "Auth overview"
