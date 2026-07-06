"""Config-declared page kinds and per-kind frontmatter validation (issue #234)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from vouch.cli import cli
from vouch.models import Page
from vouch.page_kinds import PageKindError, load_page_kind_registry, validate_page
from vouch.proposals import ProposalError, approve, propose_page
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _declare_kinds(store: KBStore, kinds: dict[str, Any]) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    assert isinstance(cfg, dict)
    cfg["page_kinds"] = kinds
    store.config_path.write_text(yaml.safe_dump(cfg))


# --- acceptance: declaring a kind makes it usable --------------------------


def test_declared_kind_is_accepted(store: KBStore) -> None:
    _declare_kinds(store, {"meeting-notes": {"required_fields": ["attendees"]}})
    pr = propose_page(
        store,
        title="Weekly sync",
        body="notes",
        page_type="meeting-notes",
        metadata={"attendees": ["alice-example", "bob-example"]},
        proposed_by="agent",
    )
    page = approve(store, pr.id, approved_by="reviewer")
    assert isinstance(page, Page)
    assert page.type == "meeting-notes"
    assert page.metadata["attendees"] == ["alice-example", "bob-example"]
    # survives a round-trip to disk
    assert store.get_page(page.id).metadata["attendees"] == ["alice-example", "bob-example"]


def test_undeclared_kind_is_rejected(store: KBStore) -> None:
    with pytest.raises(ProposalError) as exc:
        propose_page(store, title="X", body="b", page_type="proposal", proposed_by="a")
    assert "unknown page kind" in str(exc.value)


def test_string_schema_accepts_yaml_date_scalars(store: KBStore) -> None:
    """A bare `due_at: 2026-07-01` loads as datetime.date from CLI --meta
    parsing and from every frontmatter disk round-trip; a `type: string`
    schema must accept it or pages fail re-validation at approve time."""
    import datetime

    _declare_kinds(
        store,
        {
            "followup": {
                "required_fields": ["due_at"],
                "frontmatter_schema": {
                    "type": "object",
                    "properties": {"due_at": {"type": "string"}},
                },
            }
        },
    )
    pr = propose_page(
        store,
        title="ping alice-example",
        body="",
        page_type="followup",
        metadata={"due_at": datetime.date(2026, 7, 1)},
        proposed_by="agent",
    )
    page = approve(store, pr.id, approved_by="reviewer")
    assert isinstance(page, Page)
    # a genuinely wrong type still fails
    with pytest.raises(ProposalError):
        propose_page(
            store,
            title="bad",
            body="",
            page_type="followup",
            metadata={"due_at": ["not", "a", "date"]},
            proposed_by="agent",
        )


def test_protected_kind_blocks_self_approval_despite_trusted_agent(store: KBStore) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg["review"] = {"approver_role": "trusted-agent"}
    cfg["page_kinds"] = {
        "voice": {"protected": True},
        "meeting-notes": {},
    }
    store.config_path.write_text(yaml.safe_dump(cfg))

    # unprotected kind: trusted-agent opt-out lets the proposer self-approve
    open_pr = propose_page(
        store, title="sync", body="b", page_type="meeting-notes", proposed_by="agent",
    )
    assert isinstance(approve(store, open_pr.id, approved_by="agent"), Page)

    # protected kind: self-approval stays forbidden regardless of the opt-out
    voice_pr = propose_page(
        store, title="email voice", body="b", page_type="voice", proposed_by="agent",
    )
    with pytest.raises(ProposalError, match="protected"):
        approve(store, voice_pr.id, approved_by="agent")

    # a distinct reviewer can still approve it
    assert isinstance(approve(store, voice_pr.id, approved_by="reviewer"), Page)


# --- acceptance: per-field error on a missing required field ---------------


def test_missing_required_field_reports_that_field(store: KBStore) -> None:
    _declare_kinds(store, {"meeting-notes": {"required_fields": ["attendees", "date"]}})
    with pytest.raises(ProposalError) as exc:
        propose_page(
            store,
            title="Sync",
            body="b",
            page_type="meeting-notes",
            metadata={"attendees": ["a"]},
            proposed_by="agent",
        )
    msg = str(exc.value)
    assert "date" in msg
    assert "attendees" not in msg  # provided -> not reported


# --- acceptance: built-in kinds keep working unchanged ---------------------


@pytest.mark.parametrize("kind", ["entity", "decision", "concept", "session"])
def test_builtin_kinds_unchanged(store: KBStore, kind: str) -> None:
    pr = propose_page(store, title=f"a {kind}", body="b", page_type=kind, proposed_by="a")
    page = approve(store, pr.id, approved_by="r")
    assert isinstance(page, Page)
    assert page.type == kind


def test_builtin_default_page_type_still_concept(store: KBStore) -> None:
    pr = propose_page(store, title="thing", body="b", proposed_by="a")
    page = approve(store, pr.id, approved_by="r")
    assert page.type == "concept"


# --- frontmatter schema validation -----------------------------------------


def test_frontmatter_schema_type_mismatch(store: KBStore) -> None:
    _declare_kinds(
        store,
        {
            "meeting-notes": {
                "frontmatter_schema": {
                    "type": "object",
                    "properties": {"attendees": {"type": "array"}},
                }
            }
        },
    )
    with pytest.raises(ProposalError) as exc:
        propose_page(
            store,
            title="Sync",
            body="b",
            page_type="meeting-notes",
            metadata={"attendees": "alice"},  # string, should be array
            proposed_by="a",
        )
    assert "attendees" in str(exc.value)


def test_frontmatter_schema_accepts_string_form(store: KBStore) -> None:
    # the issue writes frontmatter_schema as a quoted inline string
    _declare_kinds(
        store,
        {"meeting-notes": {"frontmatter_schema": "{type: object, required: [date]}"}},
    )
    with pytest.raises(ProposalError):
        propose_page(store, title="S", body="b", page_type="meeting-notes", proposed_by="a")
    pr = propose_page(
        store,
        title="S",
        body="b",
        page_type="meeting-notes",
        metadata={"date": "2026-06-22"},
        proposed_by="a",
    )
    assert pr.id


# --- one-level inheritance (extends) ---------------------------------------


def test_extends_merges_required_fields(store: KBStore) -> None:
    _declare_kinds(
        store,
        {
            "base-decision": {"required_fields": ["owner"]},
            "decision-record": {"extends": "base-decision", "required_fields": ["date"]},
        },
    )
    registry = load_page_kind_registry(store)
    required, _schema, _cit = registry.resolve("decision-record")
    assert set(required) == {"owner", "date"}


def test_multi_level_inheritance_rejected(store: KBStore) -> None:
    _declare_kinds(
        store,
        {
            "a": {"required_fields": ["x"]},
            "b": {"extends": "a"},
            "c": {"extends": "b"},
        },
    )
    with pytest.raises(PageKindError) as exc:
        validate_page(store, "c", {}, has_citations=False)
    assert "multi-level" in str(exc.value)


def test_required_citations(store: KBStore) -> None:
    _declare_kinds(store, {"cited": {"required_citations": True}})
    with pytest.raises(ProposalError):
        propose_page(store, title="t", body="b", page_type="cited", proposed_by="a")


# --- CLI surface ------------------------------------------------------------


def test_cli_propose_page_kind_and_meta(store: KBStore) -> None:
    _declare_kinds(store, {"meeting-notes": {"required_fields": ["attendees"]}})
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "propose-page",
            "--title",
            "Sync",
            "--kind",
            "meeting-notes",
            "--meta",
            "attendees=[alice, bob]",
        ],
    )
    assert res.exit_code == 0, res.output


def test_cli_schema_list_and_sync(store: KBStore) -> None:
    _declare_kinds(store, {"meeting-notes": {"required_fields": ["attendees"]}})
    runner = CliRunner()
    listed = runner.invoke(cli, ["schema", "list"])
    assert listed.exit_code == 0, listed.output
    assert "meeting-notes" in listed.output

    # a clean KB has no conflicts
    sync = runner.invoke(cli, ["schema", "sync"])
    assert sync.exit_code == 0, sync.output
    assert "no conflicts" in sync.output


def test_schema_sync_flags_pages_after_tightening(store: KBStore) -> None:
    # propose+approve a meeting-notes page while no fields are required
    _declare_kinds(store, {"meeting-notes": {}})
    pr = propose_page(
        store, title="Sync", body="b", page_type="meeting-notes", proposed_by="a"
    )
    approve(store, pr.id, approved_by="r")
    # now tighten the kind: the existing page is missing the new field
    _declare_kinds(store, {"meeting-notes": {"required_fields": ["attendees"]}})
    res = CliRunner().invoke(cli, ["schema", "sync"])
    assert res.exit_code == 1
    assert "attendees" in res.output
