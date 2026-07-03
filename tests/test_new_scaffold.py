"""``vouch new <kind>`` — scaffold a typed page/entity proposal (issue #330).

The scaffold reads the page-kind registry (or ``EntityType``) for the kind,
stubs every required field, and files a *pending* proposal through the normal
review gate. It reuses ``propose_page`` / ``propose_entity`` verbatim, so it
never writes an approved artifact and never weakens validation: an unfilled
required field is flagged the same way any other proposal is. ``--dry-run``
shows the stubbed shape (and the missing-field list) without filing anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from vouch.cli import cli
from vouch.models import ProposalKind, ProposalStatus
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _declare_kind(store: KBStore, name: str, **spec: Any) -> None:
    """Write a ``config.yaml`` ``page_kinds.<name>`` entry for the test kb."""
    cfg = store.kb_dir / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    data = data or {}
    data.setdefault("page_kinds", {})[name] = spec
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_page_scaffold_creates_pending_with_required_fields(store: KBStore) -> None:
    _declare_kind(store, "decision-record", required_fields=["status", "owner"])
    r = CliRunner().invoke(
        cli,
        [
            "new", "decision-record", "--title", "pick db",
            "--field", "status=open", "--field", "owner=alice",
        ],
    )
    assert r.exit_code == 0, r.output
    pr = store.get_proposal(r.output.strip())
    assert pr.status == ProposalStatus.PENDING
    assert pr.kind == ProposalKind.PAGE
    md = pr.payload["metadata"]
    assert md["status"] == "open"
    assert md["owner"] == "alice"


def test_dry_run_stubs_required_fields_and_files_nothing(store: KBStore) -> None:
    _declare_kind(store, "decision-record", required_fields=["status", "owner"])
    before = len(store.list_proposals())
    r = CliRunner().invoke(
        cli,
        ["new", "decision-record", "--title", "x", "--field", "status=open", "--dry-run"],
    )
    assert r.exit_code == 0, r.output
    # every required field is stubbed into the draft; the unfilled one is listed.
    assert "status" in r.output and "owner" in r.output
    assert "missing required" in r.output
    assert len(store.list_proposals()) == before  # nothing filed


def test_unfilled_required_field_is_flagged(store: KBStore) -> None:
    # propose_page re-validates, so an empty required field is flagged, not
    # silently written — the scaffold never weakens validation.
    _declare_kind(store, "decision-record", required_fields=["status"])
    r = CliRunner().invoke(cli, ["new", "decision-record", "--title", "x"])
    assert r.exit_code != 0
    assert "Traceback" not in r.output
    assert "status" in r.output


def test_field_is_parsed_as_yaml(store: KBStore) -> None:
    _declare_kind(store, "meeting", required_fields=["attendees"])
    r = CliRunner().invoke(
        cli, ["new", "meeting", "--title", "sync", "--field", "attendees=[a, b]"]
    )
    assert r.exit_code == 0, r.output
    pr = store.get_proposal(r.output.strip())
    assert pr.payload["metadata"]["attendees"] == ["a", "b"]


def test_entity_scaffold_routes_to_propose_entity(store: KBStore) -> None:
    r = CliRunner().invoke(cli, ["new", "person", "--name", "alice-example"])
    assert r.exit_code == 0, r.output
    pr = store.get_proposal(r.output.strip())
    assert pr.kind == ProposalKind.ENTITY
    assert pr.status == ProposalStatus.PENDING
    assert pr.payload["name"] == "alice-example"


def test_collision_defaults_to_page_and_entity_flag_forces_entity(store: KBStore) -> None:
    # ``decision`` is both a built-in page type and an EntityType member.
    r_page = CliRunner().invoke(cli, ["new", "decision", "--title", "d1"])
    assert r_page.exit_code == 0, r_page.output
    assert store.get_proposal(r_page.output.strip()).kind == ProposalKind.PAGE

    r_ent = CliRunner().invoke(cli, ["new", "decision", "--entity", "--name", "d-ent"])
    assert r_ent.exit_code == 0, r_ent.output
    assert store.get_proposal(r_ent.output.strip()).kind == ProposalKind.ENTITY


def test_unknown_kind_errors_with_known_list(store: KBStore) -> None:
    r = CliRunner().invoke(cli, ["new", "no-such-kind", "--title", "x"])
    assert r.exit_code != 0
    assert "unknown kind" in r.output


def test_scaffold_only_files_a_pending_proposal(store: KBStore) -> None:
    r = CliRunner().invoke(cli, ["new", "concept", "--title", "graphs"])
    assert r.exit_code == 0, r.output
    pid = r.output.strip()
    assert store.get_proposal(pid).status == ProposalStatus.PENDING
    pending_ids = [p.id for p in store.list_proposals(ProposalStatus.PENDING)]
    assert pid in pending_ids
