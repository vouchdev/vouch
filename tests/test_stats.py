"""KB observability — vouch stats / kb.stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import stats
from vouch.cli import cli
from vouch.jsonl_server import handle_request
from vouch.models import Claim
from vouch.proposals import approve, expire_one, propose_claim, reject
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def test_citation_summary_clean_kb(store: KBStore) -> None:
    src = store.put_source(b"evidence body")
    store.put_claim(Claim(id="c1", text="fact", evidence=[src.id]))
    summary = stats.citation_summary(store)
    assert summary["claims_total"] == 1
    assert summary["claims_with_valid_citation"] == 1
    assert summary["coverage_rate"] == 1.0
    assert summary["broken_citation"] == 0


def test_citation_summary_flags_broken_and_invalid(store: KBStore) -> None:
    src_ok = store.put_source(b"ok")
    src_gone = store.put_source(b"gone")
    store.put_claim(Claim(id="good", text="t", evidence=[src_ok.id]))
    store.put_claim(Claim(id="broken", text="t", evidence=[src_gone.id]))
    import shutil

    shutil.rmtree(store.kb_dir / "sources" / src_gone.id)
    (store.kb_dir / "claims" / "legacy.yaml").write_text(
        "id: legacy\n"
        'text: "uncited"\n'
        "type: fact\n"
        "status: stable\n"
        "confidence: 1.0\n"
        "evidence: []\n"
    )
    summary = stats.citation_summary(store)
    assert summary["claims_total"] == 3
    assert summary["invalid_claim"] == 1
    assert summary["broken_citation"] == 1
    assert summary["claims_with_valid_citation"] == 1


def test_pending_summary_by_agent(store: KBStore) -> None:
    src = store.put_source(b"x")
    propose_claim(store, text="a", evidence=[src.id], proposed_by="agent-a")
    propose_claim(store, text="b", evidence=[src.id], proposed_by="agent-a")
    propose_claim(store, text="c", evidence=[src.id], proposed_by="agent-b")
    pending = stats.pending_summary(store)
    assert pending["total"] == 3
    assert pending["by_agent"] == {"agent-a": 2, "agent-b": 1}
    assert pending["age_days"]["median"] is not None


def test_review_summary_counts_decisions(store: KBStore) -> None:
    src = store.put_source(b"x")
    p_ok = propose_claim(store, text="ok", evidence=[src.id], proposed_by="a1")
    p_no = propose_claim(store, text="no", evidence=[src.id], proposed_by="a2")
    approve(store, p_ok.id, approved_by="human")
    reject(store, p_no.id, rejected_by="human", reason="duplicate")

    review = stats.review_summary(store, since_days=None)
    assert review["approved"] == 1
    assert review["rejected"] == 1
    assert review["approval_rate"] == 0.5
    assert review["by_agent"]["a1"]["approved"] == 1
    assert review["by_agent"]["a2"]["rejected"] == 1


def test_review_summary_respects_window(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="human")
    path = store.kb_dir / "decided" / f"{pr.id}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["decided_at"] = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    path.write_text(yaml.safe_dump(raw, sort_keys=False))

    narrow = stats.review_summary(store, since_days=30)
    wide = stats.review_summary(store, since_days=None)
    assert narrow["approved"] == 0
    assert wide["approved"] == 1


def test_collect_stats_includes_audit_totals(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="human")
    body = stats.collect_stats(store, since_days=30)
    assert body["review"]["audit_totals"]["approved"] >= 1
    assert "citations" in body
    assert body["counts"]["claims"] >= 1


def test_cli_stats_json(store: KBStore) -> None:
    src = store.put_source(b"x")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    result = CliRunner().invoke(cli, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert data["citations"]["coverage_rate"] == 1.0


def test_jsonl_kb_stats(store: KBStore) -> None:
    resp = handle_request({"id": "1", "method": "kb.stats", "params": {}})
    assert resp["ok"] is True
    assert "pending" in resp["result"]
    assert "review" in resp["result"]


def test_stats_marks_expired_decisions(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="stale", evidence=[src.id], proposed_by="a")
    path = store.kb_dir / "proposed" / f"{pr.id}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["proposed_at"] = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    expire_one(store, pr.id)
    review = stats.review_summary(store, since_days=None)
    assert review["expired"] == 1
    assert review["by_agent"]["a"]["expired"] == 1


# --- kb.activity -----------------------------------------------------------


def _append_event(store: KBStore, *, event: str, actor: str, created_at: str) -> None:
    import json

    line = {
        "id": "0" * 32,
        "event": event,
        "actor": actor,
        "created_at": created_at,
        "object_ids": [],
        "dry_run": False,
        "reversible": True,
        "data": {},
        "prev_hash": None,
        "hash": None,
    }
    with (store.kb_dir / "audit.log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def test_collect_activity_buckets_events(store: KBStore) -> None:
    src = store.put_source(b"x")
    p1 = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent-a")
    propose_claim(store, text="b", evidence=[src.id], proposed_by="agent-b")
    approve(store, p1.id, approved_by="human")

    body = stats.collect_activity(store)
    assert body["by_event"]["proposal.claim.create"] == 2
    assert body["by_event"]["proposal.claim.approve"] == 1
    assert body["active_days"] == 1
    day = body["by_day"][body["first_event_day"]]
    assert day["proposals"] == 2
    assert day["decisions"] == 1
    assert day["total"] == body["total_events"]
    assert len(body["by_hour"]) == 7
    assert all(len(row) == 24 for row in body["by_hour"])
    assert sum(sum(row) for row in body["by_hour"]) == body["total_events"]
    assert body["by_actor"]


def test_collect_activity_tz_offset_shifts_buckets(store: KBStore) -> None:
    from vouch import audit as audit_mod

    store.put_source(b"x")
    events = list(audit_mod.read_events(store.kb_dir))
    for offset in (840, -840):
        body = stats.collect_activity(store, tz_offset_minutes=offset)
        expected = {
            (stats._utc(e.created_at) + timedelta(minutes=offset)).date().isoformat()
            for e in events
        }
        assert set(body["by_day"]) == expected


def test_collect_activity_clamps_tz_offset(store: KBStore) -> None:
    body = stats.collect_activity(store, tz_offset_minutes=10_000)
    assert body["tz_offset_minutes"] == 840


def test_collect_activity_window_excludes_old_events(store: KBStore) -> None:
    _append_event(
        store, event="proposal.claim.create", actor="ancient",
        created_at=(datetime.now(UTC) - timedelta(days=400)).isoformat(),
    )

    windowed = stats.collect_activity(store, days=365)
    all_time = stats.collect_activity(store, days=0)
    assert "ancient" not in windowed["by_actor"]
    assert all_time["by_actor"]["ancient"] == 1
    assert all_time["window_days"] is None


def test_collect_activity_rejects_negative_days(store: KBStore) -> None:
    with pytest.raises(ValueError, match="days"):
        stats.collect_activity(store, days=-5)


def test_collect_activity_iana_tz_is_dst_correct(store: KBStore) -> None:
    # 23:30 UTC on a January Thursday is 17:30 the same Thursday in Chicago
    # (CST, UTC-6); the viewer's *current* summer offset (-5) would put it at
    # 18:30 — the IANA path must use the offset in effect at the event.
    _append_event(
        store, event="kb.init", actor="winter",
        created_at="2026-01-15T23:30:00+00:00",
    )
    body = stats.collect_activity(store, days=0, tz="America/Chicago")
    assert body["by_day"] == {"2026-01-15": {"total": 1, "proposals": 0, "decisions": 0}}
    assert body["by_hour"][3][17] == 1  # Thursday row, 17:00 column


def test_collect_activity_bad_tz_falls_back_to_offset(store: KBStore) -> None:
    _append_event(
        store, event="kb.init", actor="x",
        created_at="2026-01-15T23:30:00+00:00",
    )
    body = stats.collect_activity(store, days=0, tz="Not/AZone", tz_offset_minutes=60)
    assert body["by_day"] == {"2026-01-16": {"total": 1, "proposals": 0, "decisions": 0}}


def test_collect_activity_respects_viewer_scope(store: KBStore) -> None:
    from vouch.models import ArtifactScope, Visibility
    from vouch.scoping import ViewerContext

    src = store.put_source(b"evidence")
    store.put_claim(Claim(
        id="billing-secret",
        text="billing",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="billing"),
    ))
    from vouch import audit as audit_mod

    audit_mod.log_event(
        store.kb_dir, event="claim.create", actor="billing-bot",
        object_ids=["billing-secret"],
    )

    unscoped = stats.collect_activity(store, days=0)
    scoped = stats.collect_activity(
        store, days=0, viewer=ViewerContext(project="design"),
    )
    assert "billing-bot" in unscoped["by_actor"]
    assert "billing-bot" not in scoped["by_actor"]
    assert scoped["viewer"] == {"project": "design", "agent": None}


def test_jsonl_kb_activity(store: KBStore) -> None:
    src = store.put_source(b"x")
    propose_claim(store, text="a", evidence=[src.id], proposed_by="agent-a")
    resp = handle_request({"id": "1", "method": "kb.activity", "params": {"days": 0}})
    assert resp["ok"] is True
    assert resp["result"]["total_events"] >= 1
    assert len(resp["result"]["by_hour"]) == 7


def test_cli_activity_json(store: KBStore) -> None:
    import json

    src = store.put_source(b"x")
    propose_claim(store, text="a", evidence=[src.id], proposed_by="agent-a")
    result = CliRunner().invoke(cli, ["activity", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total_events"] >= 1
    assert data["active_days"] >= 1
