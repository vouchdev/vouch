"""Read-only reviewer-throughput report — vouch reviewers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import reviewers
from vouch.cli import cli
from vouch.proposals import approve, expire_one, propose_claim, reject
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _row(report: reviewers.ReviewerReport, reviewer: str) -> reviewers.ReviewerRow:
    for r in report.reviewers:
        if r.reviewer == reviewer:
            return r
    raise AssertionError(f"{reviewer} not in report")


def _retime(store: KBStore, pid: str, *, proposed: datetime, decided: datetime) -> None:
    path = store.kb_dir / "decided" / f"{pid}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["proposed_at"] = proposed.isoformat()
    raw["decided_at"] = decided.isoformat()
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_counts_and_approval_rate_by_reviewer(store: KBStore) -> None:
    src = store.put_source(b"x")
    a = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    b = propose_claim(store, text="b", evidence=[src.id], proposed_by="agent")
    c = propose_claim(store, text="c", evidence=[src.id], proposed_by="agent")
    approve(store, a.id, approved_by="alice")
    reject(store, b.id, rejected_by="alice", reason="dup")
    approve(store, c.id, approved_by="bob")

    report = reviewers.build(store)
    assert report.decisions_total == 3
    assert report.reviewers_total == 2
    alice = _row(report, "alice")
    assert (alice.decisions, alice.approved, alice.rejected) == (2, 1, 1)
    assert alice.approval_rate == 0.5
    assert _row(report, "bob").approval_rate == 1.0
    # most decisions first.
    assert report.reviewers[0].reviewer == "alice"


def test_expiry_is_excluded_from_reviewers(store: KBStore) -> None:
    src = store.put_source(b"x")
    live = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    stale = propose_claim(store, text="b", evidence=[src.id], proposed_by="agent")
    approve(store, live.id, approved_by="alice")
    # age the pending proposal out, then expire it — a system action, not a review.
    ppath = store.kb_dir / "proposed" / f"{stale.id}.yaml"
    raw = yaml.safe_load(ppath.read_text())
    raw["proposed_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    ppath.write_text(yaml.safe_dump(raw, sort_keys=False))
    expire_one(store, stale.id)

    report = reviewers.build(store)
    assert report.expired_total == 1
    assert report.decisions_total == 1
    assert [r.reviewer for r in report.reviewers] == ["alice"]


def test_turnaround_hours(store: KBStore) -> None:
    src = store.put_source(b"x")
    p = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    approve(store, p.id, approved_by="alice")
    now = datetime.now(UTC)
    _retime(store, p.id, proposed=now - timedelta(hours=3), decided=now)

    row = _row(reviewers.build(store), "alice")
    assert row.turnaround_hours_median == 3.0
    assert row.turnaround_hours_max == 3.0


def test_window_filters_old_decisions(store: KBStore) -> None:
    src = store.put_source(b"x")
    old = propose_claim(store, text="old", evidence=[src.id], proposed_by="agent")
    fresh = propose_claim(store, text="fresh", evidence=[src.id], proposed_by="agent")
    approve(store, old.id, approved_by="alice")
    approve(store, fresh.id, approved_by="alice")
    now = datetime.now(UTC)
    _retime(store, old.id, proposed=now - timedelta(days=11), decided=now - timedelta(days=10))
    _retime(store, fresh.id, proposed=now - timedelta(hours=2), decided=now)

    report = reviewers.build(store, since=now - timedelta(days=7))
    assert report.decisions_total == 1
    assert _row(report, "alice").decisions == 1


def test_missing_reviewer_falls_back_to_unknown(store: KBStore) -> None:
    src = store.put_source(b"x")
    p = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    approve(store, p.id, approved_by="alice")
    path = store.kb_dir / "decided" / f"{p.id}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["decided_by"] = None
    path.write_text(yaml.safe_dump(raw, sort_keys=False))

    report = reviewers.build(store)
    assert [r.reviewer for r in report.reviewers] == ["(unknown)"]


def test_negative_turnaround_from_clock_skew_is_dropped(store: KBStore) -> None:
    src = store.put_source(b"x")
    p = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    approve(store, p.id, approved_by="alice")
    now = datetime.now(UTC)
    # decided before proposed — skew; the decision still counts, turnaround does not.
    _retime(store, p.id, proposed=now, decided=now - timedelta(hours=1))

    row = _row(reviewers.build(store), "alice")
    assert row.decisions == 1
    assert row.turnaround_hours_median is None


def test_to_dict_schema(store: KBStore) -> None:
    src = store.put_source(b"x")
    p = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    approve(store, p.id, approved_by="alice")
    body = reviewers.build(store).to_dict()
    assert set(body) == {
        "generated_at",
        "since",
        "reviewers_total",
        "decisions_total",
        "expired_total",
        "reviewers",
    }
    assert set(body["reviewers"][0]) == {
        "reviewer",
        "decisions",
        "approved",
        "rejected",
        "approval_rate",
        "turnaround_hours_median",
        "turnaround_hours_max",
    }


def test_empty_kb(store: KBStore) -> None:
    report = reviewers.build(store)
    assert report.reviewers == []
    assert report.decisions_total == 0
    assert "reviewers: 0" in reviewers.render_text(report)


def test_cli_reviewers_json(store: KBStore) -> None:
    src = store.put_source(b"x")
    p = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    approve(store, p.id, approved_by="alice")
    result = CliRunner().invoke(cli, ["reviewers", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["decisions_total"] == 1
    assert data["reviewers"][0]["reviewer"] == "alice"


def test_cli_reviewers_text_and_markdown(store: KBStore) -> None:
    src = store.put_source(b"x")
    p = propose_claim(store, text="a", evidence=[src.id], proposed_by="agent")
    approve(store, p.id, approved_by="alice")
    text = CliRunner().invoke(cli, ["reviewers"])
    assert text.exit_code == 0, text.output
    assert "alice" in text.output
    md = CliRunner().invoke(cli, ["reviewers", "--format", "markdown"])
    assert md.exit_code == 0, md.output
    assert "# reviewer throughput" in md.output
