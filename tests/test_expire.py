"""Proposal expiry — stale pending GC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import audit
from vouch.cli import cli
from vouch.jsonl_server import handle_request
from vouch.models import ProposalStatus
from vouch.proposals import (
    EXPIRE_ACTOR,
    EXPIRE_REASON,
    expire_one,
    expire_pending,
    expire_pending_after_days,
    list_stale_pending,
    propose_claim,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _set_expire_days(store: KBStore, days: int) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    assert isinstance(cfg, dict)
    review = cfg.setdefault("review", {})
    assert isinstance(review, dict)
    review["expire_pending_after_days"] = days
    store.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def _age_proposal(store: KBStore, proposal_id: str, *, days: int) -> None:
    path = store.kb_dir / "proposed" / f"{proposal_id}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["proposed_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_expire_pending_after_days_default(store: KBStore) -> None:
    assert expire_pending_after_days(store) == 90


def test_expire_pending_after_days_from_config(store: KBStore) -> None:
    _set_expire_days(store, 30)
    assert expire_pending_after_days(store) == 30
    assert expire_pending_after_days(store, override=7) == 7


def test_list_stale_pending_respects_threshold(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    fresh = propose_claim(store, text="fresh", evidence=[src.id], proposed_by="a")
    stale_pr = propose_claim(store, text="stale", evidence=[src.id], proposed_by="b")
    _age_proposal(store, stale_pr.id, days=100)

    stale = list_stale_pending(store, days=90)
    assert [p.id for p in stale] == [stale_pr.id]
    assert fresh.id not in {p.id for p in stale}


def test_expire_pending_dry_run(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=120)

    result = expire_pending(store, apply=False, days=90)
    assert len(result.would_expire) == 1
    assert result.expired == []
    assert store.get_proposal(pr.id).status == ProposalStatus.PENDING


def test_expire_pending_apply_moves_to_decided(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=120)

    result = expire_pending(store, apply=True, days=90)
    assert len(result.expired) == 1
    decided = store.get_proposal(pr.id)
    assert decided.status == ProposalStatus.REJECTED
    assert decided.decision_reason == EXPIRE_REASON
    assert decided.decided_by == EXPIRE_ACTOR
    assert not (store.kb_dir / "proposed" / f"{pr.id}.yaml").exists()
    assert (store.kb_dir / "decided" / f"{pr.id}.yaml").exists()


def test_expire_pending_disabled_when_days_zero(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=500)

    result = expire_pending(store, apply=True, days=0)
    assert result.would_expire == []
    assert store.get_proposal(pr.id).status == ProposalStatus.PENDING


def test_expire_one_idempotent_on_already_expired(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    expire_one(store, pr.id)
    again = expire_one(store, pr.id)
    assert again.decision_reason == EXPIRE_REASON


def test_expire_writes_audit_event(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=200)
    expire_pending(store, apply=True, days=90)

    events = [e for e in audit.read_events(store.kb_dir) if e.event == "proposal.expire"]
    assert len(events) == 1
    assert events[0].object_ids == [pr.id]
    assert events[0].actor == EXPIRE_ACTOR


def test_cli_expire_dry_run_json(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=100)

    result = CliRunner().invoke(cli, ["expire", "--json", "--days", "90"])
    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["would_expire"][0]["id"] == pr.id
    assert payload["expired"] == []


def test_cli_expire_apply(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=100)

    result = CliRunner().invoke(cli, ["expire", "--apply", "--days", "90"])
    assert result.exit_code == 0, result.output
    assert "expired 1" in result.output
    assert store.get_proposal(pr.id).status == ProposalStatus.REJECTED


def test_jsonl_kb_expire(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    _age_proposal(store, pr.id, days=100)

    dry = handle_request({
        "id": "1",
        "method": "kb.expire",
        "params": {"apply": False, "days": 90},
    })
    assert dry["ok"] is True
    assert pr.id in dry["result"]["would_expire"]

    applied = handle_request({
        "id": "2",
        "method": "kb.expire",
        "params": {"apply": True, "days": 90},
    })
    assert applied["ok"] is True
    assert pr.id in applied["result"]["expired"]
