"""First-run onboarding behavior."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from vouch import index_db
from vouch.cli import cli
from vouch.onboarding import STARTER_CLAIM_ID, seed_starter_kb
from vouch.storage import KBStore


def test_seed_starter_kb_creates_cited_claim(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    seed = seed_starter_kb(store, approved_by="tester")

    claim = store.get_claim(STARTER_CLAIM_ID)
    source = store.get_source(seed.source_id)

    assert seed.created_source is True
    assert seed.created_claim is True
    assert claim.evidence == [source.id]
    assert claim.approved_by == "tester"
    assert "future agent sessions" in claim.text


def test_seed_starter_kb_is_idempotent(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)

    first = seed_starter_kb(store)
    second = seed_starter_kb(store)

    assert first.created_anything is True
    assert second.created_anything is False
    assert [c.id for c in store.list_claims()] == [STARTER_CLAIM_ID]
    assert len(store.list_sources()) == 1


def test_init_command_seeds_searchable_starter_kb(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Seeded starter claim" in result.output
    assert "vouch search agent" in result.output

    store = KBStore(tmp_path)
    hits = index_db.search(store.kb_dir, "agent", limit=5)

    assert store.get_claim(STARTER_CLAIM_ID).evidence
    assert any(kind == "claim" and hid == STARTER_CLAIM_ID for kind, hid, _, _ in hits)


def test_init_command_can_run_twice(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(cli, ["init", "--path", str(tmp_path)])
    second = runner.invoke(cli, ["init", "--path", str(tmp_path)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "Starter claim already present" in second.output
    assert [c.id for c in KBStore(tmp_path).list_claims()] == [STARTER_CLAIM_ID]
