"""`vouch digest` / kb.digest — the read-only reviewer briefing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import digest as digest_mod
from vouch.cli import cli
from vouch.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    Page,
    PageStatus,
    Proposal,
    ProposalKind,
)
from vouch.proposals import approve, propose_claim, reject
from vouch.storage import KBStore

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"evidence body", title="src", locator="test:src", source_type="message")

    # two pending proposals with distinct ages (older one first in the digest);
    # written directly because put_proposal is create-only and the ages must
    # be deterministic relative to NOW
    s.put_proposal(
        Proposal(
            id="pending-old", kind=ProposalKind.CLAIM, proposed_by="agent-a",
            proposed_at=NOW - timedelta(days=5),
            payload={"text": "older pending fact", "evidence": [src.id]},
        )
    )
    s.put_proposal(
        Proposal(
            id="pending-fresh", kind=ProposalKind.CLAIM, proposed_by="agent-b",
            proposed_at=NOW - timedelta(days=1),
            payload={"text": "newer pending fact", "evidence": [src.id]},
        )
    )

    # one approval and one rejection inside the window
    approved = propose_claim(
        s, text="approved fact", evidence=[src.id], proposed_by="agent-a",
    )
    approve(s, approved.id, approved_by="reviewer")
    rejected = propose_claim(
        s, text="rejected fact", evidence=[src.id], proposed_by="agent-a",
    )
    reject(s, rejected.id, rejected_by="reviewer", reason="test rejection")

    # a stale approved claim (anchor far past the threshold) and a fresh one
    s.put_claim(
        Claim(
            id="stale-claim", text="an old fact nobody confirmed",
            type=ClaimType.FACT, status=ClaimStatus.STABLE,
            evidence=[src.id], approved_by="reviewer",
            created_at=NOW - timedelta(days=400),
            updated_at=NOW - timedelta(days=400),
        )
    )
    s.put_claim(
        Claim(
            id="fresh-claim", text="a recent fact",
            type=ClaimType.FACT, status=ClaimStatus.STABLE,
            evidence=[src.id], approved_by="reviewer",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
        )
    )

    # followups: one open+due, one open+future, one closed+due
    def followup(pid: str, due: str, status: str) -> Page:
        return Page(
            id=pid, title=pid, type="followup", status=PageStatus.ACTIVE,
            metadata={"due_at": due, "followup_status": status, "owner": "alice-example"},
        )

    s.put_page(followup("due-open", "2026-07-01", "open"))
    s.put_page(followup("future-open", "2026-08-01", "open"))
    s.put_page(followup("due-done", "2026-07-01", "done"))
    return s


def test_build_sections(store: KBStore) -> None:
    d = digest_mod.build(store, since=NOW - timedelta(days=7), now=NOW)

    # pending: oldest first, both listed
    assert d.pending_total == 2
    assert [r.proposed_by for r in d.pending] == ["agent-a", "agent-b"]
    assert d.pending[0].age_days == 5

    # decisions: both inside the window, newest first, titled from payload
    decisions = {r.decision for r in d.decisions}
    assert decisions == {"approved", "rejected"}
    assert any("approved fact" in r.title for r in d.decisions)

    # stale: only the old claim
    assert [r.id for r in d.stale_claims] == ["stale-claim"]
    assert d.stale_total == 1

    # followups: open and due only
    assert [r.id for r in d.followups_due] == ["due-open"]
    assert d.followups_due[0].owner == "alice-example"

    assert d.citation_coverage is not None


def test_build_window_excludes_old_decisions(store: KBStore) -> None:
    d = digest_mod.build(store, since=NOW + timedelta(days=1), now=NOW)
    assert d.decisions == []
    # pending and followups are point-in-time state, not window-scoped
    assert d.pending_total == 2
    assert [r.id for r in d.followups_due] == ["due-open"]


def test_build_limit_caps_sections(store: KBStore) -> None:
    d = digest_mod.build(store, limit=1, now=NOW)
    assert len(d.pending) == 1
    assert d.pending_total == 2
    assert len(d.decisions) <= 1


def test_digest_is_read_only(store: KBStore) -> None:
    audit_before = (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8")
    files_before = sorted(p.name for p in (store.kb_dir / "proposed").glob("*"))

    d = digest_mod.build(store, now=NOW)
    digest_mod.render_text(d)
    digest_mod.render_markdown(d)
    json.dumps(d.to_dict())

    assert (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8") == audit_before
    assert sorted(p.name for p in (store.kb_dir / "proposed").glob("*")) == files_before


def test_empty_kb_digest(tmp_path: Path) -> None:
    s = KBStore.init(tmp_path)
    d = digest_mod.build(s, now=NOW)
    assert d.pending_total == 0
    assert d.followups_due == []
    assert "pending awaiting review: 0" in digest_mod.render_text(d)


def test_cli_digest_formats(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    runner = CliRunner()

    as_json = runner.invoke(cli, ["digest", "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    body = json.loads(as_json.output)
    assert body["pending_total"] == 2
    assert "_meta" in body  # trust stamp rides on dict-shaped CLI json

    md = runner.invoke(cli, ["digest", "--format", "markdown", "--since", "all"])
    assert md.exit_code == 0, md.output
    assert "## pending awaiting review" in md.output

    bad = runner.invoke(cli, ["digest", "--since", "notaspec"])
    assert bad.exit_code != 0


def test_jsonl_digest_handler(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    from vouch.jsonl_server import HANDLERS

    body = HANDLERS["kb.digest"]({"since": "all", "limit": 5})
    assert body["pending_total"] == 2
    assert [r["id"] for r in body["followups_due"]] == ["due-open"]
