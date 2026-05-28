"""init --template registry and the gittensor starter pack."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.onboarding import (
    SeedResult,
    available_templates,
    seed_gittensor_kb,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


# --- registry -------------------------------------------------------------


def test_available_templates_lists_starter_and_gittensor() -> None:
    assert available_templates() == ["gittensor", "starter"]


# --- gittensor seed -------------------------------------------------------


def test_seed_gittensor_creates_cited_linked_claims(store: KBStore) -> None:
    result = seed_gittensor_kb(store, approved_by="tester")

    assert isinstance(result, SeedResult)
    assert result.template == "gittensor"
    assert result.created_anything

    claims = store.list_claims()
    assert len(claims) == 4
    sid = store.list_sources()[0].id
    assert all(sid in c.evidence for c in claims)          # every claim cited
    assert all("gittensor-sn74" in c.entities for c in claims)  # linked to entity
    assert any(e.id == "gittensor-sn74" for e in store.list_entities())


def test_seed_gittensor_is_idempotent(store: KBStore) -> None:
    first = seed_gittensor_kb(store)
    second = seed_gittensor_kb(store)

    assert first.created_anything is True
    assert second.created_anything is False
    assert len(store.list_claims()) == 4
    assert len(store.list_sources()) == 1
    assert len(store.list_entities()) == 1


# --- CLI ------------------------------------------------------------------


def test_init_template_gittensor_seeds_pack(tmp_path: Path) -> None:
    res = CliRunner().invoke(cli, ["init", "--path", str(tmp_path), "--template", "gittensor"])
    assert res.exit_code == 0, res.output
    assert "gittensor template" in res.output.lower()
    store = KBStore(tmp_path)
    assert len(store.list_claims()) == 4  # only the gittensor pack, not the starter claim


def test_init_unknown_template_clean_error(tmp_path: Path) -> None:
    res = CliRunner().invoke(cli, ["init", "--path", str(tmp_path), "--template", "bogus"])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "unknown template" in res.output.lower()
