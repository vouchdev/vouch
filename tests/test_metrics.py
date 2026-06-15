"""`vouch metrics` — first-class observability (vouchdev/vouch#192).

These tests pin the stable JSON schema and verify every emitted metric against
a fixture KB whose statistics are known by construction:

* a controlled audit log (proposal create/approve/reject events with crafted
  timestamps so the lag percentiles are predictable),
* a controlled set of claims (cited / uncited / broken-citation / stale /
  retired) so citation coverage and stale ratio are exact.

Everything derives from ``.vouch/audit.log.jsonl`` + artifact files — no new
on-disk state — so the tests also stand as the contract that the metrics never
secretly depend on anything else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch.audit import log_event
from vouch.cli import cli
from vouch.metrics import (
    DEFAULT_STALE_DAYS,
    Metrics,
    MetricsError,
    compute,
    parse_since,
    percentile,
    render_prometheus,
)
from vouch.models import Claim, ClaimStatus
from vouch.storage import KBStore

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


# --- parse_since ----------------------------------------------------------


@pytest.mark.parametrize("spec", [None, "", "  ", "all", "ALL"])
def test_parse_since_unbounded(spec) -> None:
    assert parse_since(spec, now=NOW) is None


@pytest.mark.parametrize("spec,delta", [
    ("30d", timedelta(days=30)),
    ("12h", timedelta(hours=12)),
    ("2w", timedelta(weeks=2)),
    ("90m", timedelta(minutes=90)),
    ("45s", timedelta(seconds=45)),
    ("  7d ", timedelta(days=7)),
])
def test_parse_since_durations(spec, delta) -> None:
    assert parse_since(spec, now=NOW) == NOW - delta


def test_parse_since_iso_date() -> None:
    got = parse_since("2026-01-01", now=NOW)
    assert got == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_since_iso_datetime_with_tz() -> None:
    got = parse_since("2026-01-01T06:30:00+00:00", now=NOW)
    assert got == datetime(2026, 1, 1, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["yesterday", "30x", "d30", "2026-13-01", "soon"])
def test_parse_since_rejects_garbage(bad) -> None:
    with pytest.raises(MetricsError):
        parse_since(bad, now=NOW)


# --- percentile -----------------------------------------------------------


def test_percentile_empty_is_none() -> None:
    assert percentile([], 0.5) is None


def test_percentile_nearest_rank() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert percentile(vals, 0.5) == 5.0    # ceil(0.5*10)=5 -> index 4 -> 5.0
    assert percentile(vals, 0.9) == 9.0    # ceil(0.9*10)=9 -> index 8 -> 9.0
    assert percentile(vals, 0.0) == 1.0
    assert percentile(vals, 1.0) == 10.0


# --- fixture builder ------------------------------------------------------


def _seed_known_kb(store: KBStore) -> None:
    """Build a KB with statistics known by construction.

    Claims (5 total):
      c_cited     — cited, fresh, active
      c_cited2    — cited, fresh, active
      c_uncited   — written via raw YAML with no resolvable evidence (broken)
      c_stale     — cited, but last_confirmed 300d ago -> stale
      c_archived  — cited, retired (status=archived) -> not active, not stale

    -> claims_total=5, active=4 (all but archived),
       cited = c_cited, c_cited2, c_stale, c_archived = 4 fully-resolvable
       broken = c_uncited (cites a missing id) = 1
       stale = c_stale only (archived is exempt) = 1
       stale_ratio = 1/4

    Audit log: 4 creates, 3 approves, 1 reject across two actors, with
    create->approve gaps of 10s, 20s, 30s for predictable lag percentiles.
    """
    src = store.put_source(b"evidence-bytes")

    base = NOW - timedelta(days=1)

    def claim(cid: str, *, status=ClaimStatus.WORKING, confirmed=None, evidence=None):
        store.put_claim(Claim(
            id=cid, text=f"text {cid}",
            evidence=evidence if evidence is not None else [src.id],
            status=status,
            last_confirmed_at=confirmed,
        ))

    claim("c_cited", confirmed=NOW - timedelta(days=1))
    claim("c_cited2", confirmed=NOW - timedelta(days=2))
    claim("c_stale", confirmed=NOW - timedelta(days=300))
    claim("c_archived", status=ClaimStatus.ARCHIVED,
          confirmed=NOW - timedelta(days=400))

    # A claim that cites a non-existent id. put_claim refuses unresolvable
    # citations, so we write the YAML directly (the model itself only requires
    # >=1 citation, not that it resolves) — this is exactly the broken-citation
    # state that citation_coverage must catch, and it loads via list_claims().
    broken = Claim(id="c_uncited", text="dangling", evidence=["src-does-not-exist"])
    (store.kb_dir / "claims" / "c_uncited.yaml").write_text(
        yaml.safe_dump(broken.model_dump(mode="json")),
        encoding="utf-8",
    )

    # --- audit log: craft create/approve/reject with known timestamps ---
    kb = store.kb_dir
    # proposal p1: created, approved 10s later (alice)
    _ev(kb, "proposal.claim.create", "alice", ["p1"], base)
    _ev(kb, "proposal.claim.approve", "alice", ["p1", "c_cited"], base + timedelta(seconds=10))
    # proposal p2: created, approved 20s later (alice)
    _ev(kb, "proposal.page.create", "alice", ["p2"], base + timedelta(minutes=1))
    _ev(kb, "proposal.page.approve", "bob", ["p2", "pg1"], base + timedelta(minutes=1, seconds=20))
    # proposal p3: created, approved 30s later (bob)
    _ev(kb, "proposal.claim.create", "bob", ["p3"], base + timedelta(minutes=2))
    _ev(kb, "proposal.claim.approve", "bob", ["p3", "c_cited2"],
        base + timedelta(minutes=2, seconds=30))
    # proposal p4: created, rejected (bob) — no lag sample
    _ev(kb, "proposal.claim.create", "bob", ["p4"], base + timedelta(minutes=3))
    _ev(kb, "proposal.claim.reject", "alice", ["p4"], base + timedelta(minutes=3, seconds=5))
    # a confirm event for the actor leaderboard
    _ev(kb, "claim.confirm", "alice", ["c_cited"], base + timedelta(minutes=4))


def _ev(kb_dir: Path, event: str, actor: str, ids: list[str], ts: datetime) -> None:
    """Append an audit event, then rewrite its created_at to ``ts``.

    log_event stamps ``utcnow``; we need deterministic timestamps for the lag
    math, so we patch the just-written line in place. This keeps the test
    honest — it exercises the real reader against a real JSONL file.
    """
    log_event(kb_dir, event=event, actor=actor, object_ids=ids)
    path = kb_dir / "audit.log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[-1])
    obj["created_at"] = ts.isoformat()
    lines[-1] = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- compute: review-gate metrics -----------------------------------------


def test_compute_review_gate_counts(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    assert m.proposals_created == 4
    assert m.approvals == 3
    assert m.rejections == 1
    assert m.approval_rate == pytest.approx(3 / 4)


def test_compute_approval_rate_by_kind(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    # claim kind: 2 approve (p1, p3), 1 reject (p4) -> 2/3
    # page kind: 1 approve (p2), 0 reject -> 1/1
    assert m.approval_rate_by_kind["claim"] == pytest.approx(2 / 3)
    assert m.approval_rate_by_kind["page"] == pytest.approx(1.0)
    assert m.decisions_by_kind["claim"] == {"approve": 2, "reject": 1}


def test_compute_approval_rate_none_when_no_decisions(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    m = compute(store, now=NOW)
    assert m.approvals == 0 and m.rejections == 0
    assert m.approval_rate is None


# --- compute: corpus metrics ----------------------------------------------


def test_compute_citation_coverage(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    assert m.claims_total == 5
    # c_cited, c_cited2, c_stale, c_archived resolve; c_uncited does not.
    assert m.claims_cited == 4
    assert m.citation_broken == 1
    assert m.citation_coverage == pytest.approx(4 / 5)


def test_compute_stale_ratio(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    # active = 4 (everything but the archived claim).
    assert m.claims_active == 4
    # only c_stale is past 180d AND active (c_archived is retired -> exempt).
    assert m.stale_claims == 1
    assert m.stale_ratio == pytest.approx(1 / 4)


def test_compute_stale_threshold_is_configurable(store: KBStore) -> None:
    _seed_known_kb(store)
    # With a 1000d threshold nothing is stale.
    m = compute(store, now=NOW, stale_after_days=1000)
    assert m.stale_claims == 0
    assert m.stale_ratio == pytest.approx(0.0)


def test_compute_claims_by_status(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    assert m.claims_by_status["working"] == 4
    assert m.claims_by_status["archived"] == 1


# --- compute: proposal lag percentiles -------------------------------------


def test_compute_proposal_lag(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    lag = m.proposal_lag
    # three create->approve pairs: 10s, 20s, 30s (p4 was rejected -> no sample)
    assert lag.count == 3
    assert lag.p50 == pytest.approx(20.0)   # nearest-rank of [10,20,30] @0.5
    assert lag.p90 == pytest.approx(30.0)
    assert lag.mean == pytest.approx(20.0)
    assert lag.max == pytest.approx(30.0)


def test_lag_pairs_create_outside_window_with_approve_inside(store: KBStore) -> None:
    """A create older than --since must still pair with an in-window approve,
    or the left edge of the window would systematically undercount lag."""
    _seed_known_kb(store)
    # Window that starts after p1's create (base) but before its approve.
    base = NOW - timedelta(days=1)
    m = compute(store, since=base + timedelta(seconds=5), now=NOW)
    # p1 create (base) is before the window; its approve (base+10s) is inside.
    # The pairing uses the full create index, so p1's 10s sample survives.
    assert m.proposal_lag.count >= 1
    assert m.proposal_lag.p50 is not None


# --- compute: actors -------------------------------------------------------


def test_compute_actor_leaderboard(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW)
    by_name = {a.actor: a for a in m.actors}
    # alice: proposed p1,p2 (2), approved p1 (1), rejected p4 (1), confirmed 1
    assert by_name["alice"].proposed == 2
    assert by_name["alice"].approved == 1
    assert by_name["alice"].rejected == 1
    assert by_name["alice"].confirmed == 1
    # bob: proposed p3,p4 (2), approved p2,p3 (2)
    assert by_name["bob"].proposed == 2
    assert by_name["bob"].approved == 2


def test_top_actors_limit(store: KBStore) -> None:
    _seed_known_kb(store)
    m = compute(store, now=NOW, top_actors=1)
    assert len(m.actors) == 1
    m_all = compute(store, now=NOW, top_actors=0)
    assert len(m_all.actors) == 2


# --- compute: windowing ----------------------------------------------------


def test_window_excludes_old_events(store: KBStore) -> None:
    _seed_known_kb(store)
    # Everything happened ~1 day ago; a 1h window sees nothing.
    m = compute(store, since=NOW - timedelta(hours=1), now=NOW)
    assert m.proposals_created == 0
    assert m.approvals == 0
    assert m.audit_events_in_window == 0
    # Corpus metrics are window-independent — claims are still counted.
    assert m.claims_total == 5


def test_since_after_until_raises(store: KBStore) -> None:
    with pytest.raises(MetricsError):
        compute(store, since=NOW, until=NOW - timedelta(days=1))


def test_negative_stale_days_raises(store: KBStore) -> None:
    with pytest.raises(MetricsError):
        compute(store, stale_after_days=-1)


# --- schema stability ------------------------------------------------------


def test_to_dict_schema_shape(store: KBStore) -> None:
    _seed_known_kb(store)
    d = compute(store, now=NOW).to_dict()
    assert d["schema_version"] == 1
    assert set(d) == {
        "schema_version", "window", "review_gate", "corpus",
        "proposal_lag_seconds", "actors", "audit",
    }
    assert set(d["review_gate"]) == {
        "proposals_created", "approvals", "rejections", "approval_rate",
        "approval_rate_by_kind", "decisions_by_kind", "pending_now",
    }
    assert set(d["corpus"]) == {
        "claims_total", "claims_active", "claims_cited", "citation_coverage",
        "citation_broken", "stale_claims", "stale_ratio", "stale_after_days",
        "claims_by_status",
    }
    assert set(d["proposal_lag_seconds"]) == {
        "count", "p50", "p90", "p99", "mean", "max",
    }
    # JSON-serialisable end to end.
    json.dumps(d)


def test_empty_kb_is_all_none_not_crash(store: KBStore) -> None:
    m = compute(store, now=NOW)
    assert m.approval_rate is None
    assert m.citation_coverage is None
    assert m.stale_ratio is None
    assert m.proposal_lag.count == 0
    # to_dict still serialises.
    json.dumps(m.to_dict())


# --- prometheus rendering --------------------------------------------------


def test_render_prometheus_has_help_type_and_gauges(store: KBStore) -> None:
    _seed_known_kb(store)
    text = render_prometheus(compute(store, now=NOW))
    assert "# HELP vouch_approval_rate" in text
    assert "# TYPE vouch_approval_rate gauge" in text
    assert "vouch_approvals_total 3" in text
    assert "vouch_claims_total 5" in text
    # labelled metrics present.
    assert 'vouch_approval_rate_by_kind{kind="claim"}' in text
    assert 'vouch_claims_by_status{status="archived"} 1' in text


def test_render_prometheus_omits_null_gauges(store: KBStore) -> None:
    # Empty KB -> approval_rate is None -> must NOT appear (no lying zeros).
    text = render_prometheus(compute(store, now=NOW))
    assert "vouch_approval_rate " not in text
    assert "vouch_approval_rate{" not in text


# --- CLI surface -----------------------------------------------------------


def _run(args, cwd: Path):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_cli_metrics_human_table(store: KBStore, monkeypatch) -> None:
    _seed_known_kb(store)
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["metrics"])
    assert res.exit_code == 0, res.output
    assert "review gate" in res.output
    assert "approval rate" in res.output
    assert "75.0%" in res.output       # 3/4
    assert "proposal lag" in res.output


def test_cli_metrics_json(store: KBStore, monkeypatch) -> None:
    _seed_known_kb(store)
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["metrics", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["schema_version"] == 1
    assert payload["review_gate"]["approvals"] == 3
    assert payload["corpus"]["claims_total"] == 5


def test_cli_metrics_prometheus(store: KBStore, monkeypatch) -> None:
    _seed_known_kb(store)
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["metrics", "--prometheus"])
    assert res.exit_code == 0, res.output
    assert "vouch_approvals_total 3" in res.output


def test_cli_metrics_since(store: KBStore, monkeypatch) -> None:
    _seed_known_kb(store)
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["metrics", "--since", "1h", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["review_gate"]["approvals"] == 0   # all events are ~1d old


def test_cli_metrics_bad_since_is_clean_error(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["metrics", "--since", "whenever"])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "Error:" in res.output


def test_cli_metrics_json_and_prometheus_mutually_exclusive(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["metrics", "--json", "--prometheus"])
    assert res.exit_code != 0
    assert "Error:" in res.output


def test_metrics_is_a_dataclass(store: KBStore) -> None:
    m = compute(store, now=NOW)
    assert isinstance(m, Metrics)
    assert m.stale_after_days == DEFAULT_STALE_DAYS
