"""First-run onboarding behavior."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from vouch import index_db
from vouch.cli import cli
from vouch.onboarding import (
    BRAIN_PAGE_ID,
    BRAIN_PAGE_KINDS,
    STARTER_CLAIM_ID,
    TEMPLATES,
    available_templates,
    seed_company_brain_kb,
    seed_starter_kb,
)
from vouch.page_kinds import load_page_kind_registry
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


def test_company_brain_template_declares_page_kinds(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    seed = seed_company_brain_kb(store, approved_by="tester")

    assert seed.template == "company-brain"
    assert seed.created_anything is True

    registry = load_page_kind_registry(store)
    for kind in BRAIN_PAGE_KINDS:
        assert registry.is_known(kind), kind

    required, schema, _ = registry.resolve("followup")
    assert "due_at" in required
    assert "followup_status" in required
    assert schema["properties"]["due_at"] == {"type": "string"}

    # decision records and voice pages must cite their evidence
    assert registry.resolve("decision-record")[2] is True
    assert registry.resolve("voice")[2] is True

    # the seeded guide page is approved, cited, and searchable
    page = store.get_page(BRAIN_PAGE_ID)
    assert page.sources
    assert page.status.value == "active"


def test_company_brain_template_preserves_existing_config(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    store.config_path.write_text(
        "review:\n"
        "  approver_role: human\n"
        "page_kinds:\n"
        "  followup:\n"
        "    required_fields: [custom]\n",
        encoding="utf-8",
    )

    seed_company_brain_kb(store)

    loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    assert loaded["review"]["approver_role"] == "human"
    # a kind the operator already declared wins over the template
    assert loaded["page_kinds"]["followup"]["required_fields"] == ["custom"]
    # missing kinds are filled in
    assert "contact" in loaded["page_kinds"]
    assert "decision-record" in loaded["page_kinds"]


def test_company_brain_template_appends_when_no_page_kinds(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    store.config_path.write_text(
        "# operator notes survive the template\nreview:\n  approver_role: human\n",
        encoding="utf-8",
    )

    seed_company_brain_kb(store)

    text = store.config_path.read_text(encoding="utf-8")
    # no page_kinds key -> the block is appended; comments survive verbatim
    assert text.startswith("# operator notes survive the template")
    loaded = yaml.safe_load(text)
    assert set(BRAIN_PAGE_KINDS) <= set(loaded["page_kinds"])
    assert loaded["review"]["approver_role"] == "human"


def test_company_brain_template_is_idempotent(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)

    first = seed_company_brain_kb(store)
    second = seed_company_brain_kb(store)

    assert first.created_anything is True
    assert second.created_anything is False


def test_company_brain_registered_in_templates() -> None:
    assert "company-brain" in TEMPLATES
    assert "company-brain" in available_templates()


def test_init_command_with_company_brain_template(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["init", "--path", str(tmp_path), "--template", "company-brain"]
    )

    assert result.exit_code == 0, result.output
    assert "company-brain" in result.output

    store = KBStore(tmp_path)
    registry = load_page_kind_registry(store)
    assert registry.is_known("followup")
    assert store.get_page(BRAIN_PAGE_ID).status.value == "active"
    # the starter claim still seeds — templates add to the default, not replace
    assert store.get_claim(STARTER_CLAIM_ID)


def test_init_command_rejects_unknown_template(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["init", "--path", str(tmp_path), "--template", "bogus"]
    )

    assert result.exit_code != 0
    assert "bogus" in result.output


def test_init_command_can_run_twice(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(cli, ["init", "--path", str(tmp_path)])
    second = runner.invoke(cli, ["init", "--path", str(tmp_path)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "Starter claim already present" in second.output
    assert [c.id for c in KBStore(tmp_path).list_claims()] == [STARTER_CLAIM_ID]
