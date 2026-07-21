"""`vouch health effectiveness` / kb.effectiveness (vouchdev/vouch#426).

Read-only, measurement-only correlation between context-pack surfacing
(`index_db.context_surfacing`) and a coarse audit-derived session outcome.
These tests pin: the insufficient-sample gate, a clear useful/harmful signal
under a fixed clock, and the read-only invariant -- the three paths the
issue calls out explicitly -- plus the surfacing-table plumbing and the
three registration surfaces (CLI, MCP, JSONL).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import health, index_db
from vouch.audit import log_event
from vouch.cli import cli
from vouch.eval.effectiveness import (
    DEFAULT_MIN_SAMPLES,
    EffectivenessError,
    compute,
    render_text,
    wilson_interval,
)
from vouch.models import Claim, Session
from vouch.storage import KBStore

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _ev(kb_dir: Path, event: str, actor: str, ids: list[str], ts: datetime) -> None:
    """Append an audit event, then rewrite its created_at to `ts` (see
    test_metrics.py's identical helper -- log_event stamps utcnow, so
    deterministic fixtures patch the just-written line in place)."""
    log_event(kb_dir, event=event, actor=actor, object_ids=ids)
    path = kb_dir / "audit.log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[-1])
    obj["created_at"] = ts.isoformat()
    lines[-1] = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session(store: KBStore, sid: str, *, agent: str, started: datetime, ended: datetime) -> None:
    store.put_session(Session(id=sid, agent=agent, started_at=started, ended_at=ended))


# --- wilson_interval --------------------------------------------------------


def test_wilson_interval_zero_samples_is_maximally_uncertain() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_narrows_with_more_samples() -> None:
    """Same 50% observed rate, ten times the samples -> a tighter interval."""
    lo_small, hi_small = wilson_interval(5, 10)
    lo_big, hi_big = wilson_interval(500, 1000)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_wilson_interval_is_bounded() -> None:
    for k, n in [(0, 1), (1, 1), (3, 7), (100, 100)]:
        lo, hi = wilson_interval(k, n)
        assert 0.0 <= lo <= hi <= 1.0


def test_compute_rejects_bad_min_samples(store: KBStore) -> None:
    with pytest.raises(EffectivenessError):
        compute(store, min_samples=0, now=NOW)


# --- index_db plumbing -------------------------------------------------------


def test_record_and_read_context_surfacing_round_trip(store: KBStore) -> None:
    index_db.record_context_surfacing(
        store.kb_dir, session_id="s1", items=[("claim", "c1"), ("page", "p1")],
    )
    index_db.record_context_surfacing(store.kb_dir, session_id="", items=[("claim", "cX")])
    index_db.record_context_surfacing(store.kb_dir, session_id="s2", items=[])
    rows = index_db.read_context_surfacing(store.kb_dir)
    assert {(r[0], r[1], r[2]) for r in rows} == {
        ("s1", "claim", "c1"),
        ("s1", "page", "p1"),
    }


def test_reset_clears_context_surfacing(store: KBStore) -> None:
    index_db.record_context_surfacing(store.kb_dir, session_id="s1", items=[("claim", "c1")])
    assert index_db.read_context_surfacing(store.kb_dir)
    index_db.reset(store.kb_dir)
    assert index_db.read_context_surfacing(store.kb_dir) == []


# --- compute: insufficient-sample path --------------------------------------


def test_insufficient_sample_path(store: KBStore) -> None:
    """One session of signal is real evidence but not enough to speak from."""
    _session(
        store, "s1", agent="agent-a",
        started=NOW - timedelta(days=1, minutes=10), ended=NOW - timedelta(days=1),
    )
    _ev(store.kb_dir, "claim.confirm", "agent-a", ["c1"], NOW - timedelta(days=1, minutes=5))
    index_db.record_context_surfacing(store.kb_dir, session_id="s1", items=[("claim", "c1")])

    report = compute(store, since=None, min_samples=DEFAULT_MIN_SAMPLES, now=NOW)

    assert report.sessions_considered == 1
    assert len(report.artifacts) == 1
    a = report.artifacts[0]
    assert (a.kind, a.id) == ("claim", "c1")
    assert a.samples == 1
    assert a.good == 1
    assert a.verdict == "insufficient"


# --- compute: a clear useful/harmful signal under a fixed clock ------------


def _outcome_session(
    store: KBStore, kb_dir: Path, sid: str, *, agent: str, good: bool, base: datetime,
    surface: tuple[str, str] | None,
) -> None:
    started = base
    ended = base + timedelta(minutes=5)
    _session(store, sid, agent=agent, started=started, ended=ended)
    event = "claim.confirm" if good else "claim.contradict"
    _ev(kb_dir, event, agent, [f"target-{sid}"], base + timedelta(minutes=2))
    if surface is not None:
        index_db.record_context_surfacing(kb_dir, session_id=sid, items=[surface])


def test_clear_signal_useful_and_harmful_with_fixed_clock(store: KBStore) -> None:
    """Population baseline is an exact 50/50 split; an artifact surfaced only
    alongside good outcomes clears baseline as useful, one surfaced only
    alongside bad outcomes clears it as harmful -- both well outside the
    Wilson interval's uncertainty at n=8, so the assertions aren't flaky."""
    kb_dir = store.kb_dir
    base = NOW - timedelta(days=10)

    # Population: 5 good + 5 bad sessions that surface nothing, establishing
    # baseline_rate == 0.5 exactly. Distinct agents per session sidestep any
    # same-actor time-window overlap.
    for i in range(5):
        _outcome_session(
            store, kb_dir, f"pop-good-{i}", agent=f"pop-good-agent-{i}", good=True,
            base=base + timedelta(hours=i), surface=None,
        )
        _outcome_session(
            store, kb_dir, f"pop-bad-{i}", agent=f"pop-bad-agent-{i}", good=False,
            base=base + timedelta(hours=i + 100), surface=None,
        )

    # "good1" surfaces only in sessions that end well: 8/8 good.
    for i in range(8):
        _outcome_session(
            store, kb_dir, f"useful-{i}", agent=f"useful-agent-{i}", good=True,
            base=base + timedelta(hours=i + 200), surface=("claim", "good1"),
        )

    # "bad1" surfaces only in sessions that end badly: 0/8 good.
    for i in range(8):
        _outcome_session(
            store, kb_dir, f"harmful-{i}", agent=f"harmful-agent-{i}", good=False,
            base=base + timedelta(hours=i + 300), surface=("claim", "bad1"),
        )

    report = compute(store, since=None, min_samples=5, now=NOW)

    assert report.sessions_with_signal == 26
    assert report.baseline_rate == pytest.approx(0.5)
    assert [a.id for a in report.artifacts] == ["good1", "bad1"]

    good1, bad1 = report.artifacts
    assert good1.samples == 8 and good1.good == 8
    assert good1.verdict == "useful"
    assert good1.ci_low > report.baseline_rate

    assert bad1.samples == 8 and bad1.good == 0
    assert bad1.verdict == "harmful"
    assert bad1.ci_high < report.baseline_rate

    # Rendering must not choke on either verdict shape.
    text = render_text(report)
    assert "useful" in text
    assert "harmful" in text
    json.dumps(report.to_dict())


# --- read-only invariant -----------------------------------------------------


def test_effectiveness_is_read_only(store: KBStore) -> None:
    _session(
        store, "s1", agent="agent-a",
        started=NOW - timedelta(minutes=10), ended=NOW - timedelta(minutes=5),
    )
    _ev(store.kb_dir, "claim.confirm", "agent-a", ["c1"], NOW - timedelta(minutes=7))
    index_db.record_context_surfacing(store.kb_dir, session_id="s1", items=[("claim", "c1")])

    audit_before = (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8")
    surfacing_before = index_db.read_context_surfacing(store.kb_dir)
    proposed_before = sorted(p.name for p in (store.kb_dir / "proposed").glob("*"))
    sessions_before = sorted(p.name for p in (store.kb_dir / "sessions").glob("*"))

    report = compute(store, now=NOW)
    render_text(report)
    json.dumps(report.to_dict())

    assert (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8") == audit_before
    assert index_db.read_context_surfacing(store.kb_dir) == surfacing_before
    assert sorted(p.name for p in (store.kb_dir / "proposed").glob("*")) == proposed_before
    assert sorted(p.name for p in (store.kb_dir / "sessions").glob("*")) == sessions_before


def test_empty_kb_effectiveness(tmp_path: Path) -> None:
    s = KBStore.init(tmp_path)
    report = compute(s, now=NOW)
    assert report.sessions_considered == 0
    assert report.baseline_rate is None
    assert report.artifacts == []
    assert "no surfaced artifacts" in render_text(report)


# --- the read-path hook: kb.context records surfacing when session_id given -


def test_jsonl_context_records_surfacing_only_with_session_id(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    from vouch.jsonl_server import HANDLERS

    src = store.put_source(b"evidence body")
    store.put_claim(Claim(id="c-widget", text="the widget frobnicator", evidence=[src.id]))
    health.rebuild_index(store)

    no_session = HANDLERS["kb.context"]({"task": "widget frobnicator"})
    assert no_session["items"]
    assert index_db.read_context_surfacing(store.kb_dir) == []

    with_session = HANDLERS["kb.context"](
        {"task": "widget frobnicator", "session_id": "sess-1"}
    )
    assert with_session["items"]
    rows = index_db.read_context_surfacing(store.kb_dir)
    assert rows
    assert all(r[0] == "sess-1" for r in rows)
    surfaced_ids = {r[2] for r in rows}
    assert "c-widget" in surfaced_ids


def test_jsonl_effectiveness_handler(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    from vouch.jsonl_server import HANDLERS

    body = HANDLERS["kb.effectiveness"]({"window": "all", "min_samples": 1})
    assert body["sessions_considered"] == 0
    assert body["artifacts"] == []


# --- CLI ----------------------------------------------------------------


def test_cli_health_effectiveness(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    runner = CliRunner()

    as_json = runner.invoke(cli, ["health", "effectiveness", "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    body = json.loads(as_json.output)
    assert body["sessions_considered"] == 0
    assert body["artifacts"] == []

    as_text = runner.invoke(cli, ["health", "effectiveness"])
    assert as_text.exit_code == 0, as_text.output
    assert "kb.effectiveness" in as_text.output

    bad_window = runner.invoke(cli, ["health", "effectiveness", "--window", "notaspec"])
    assert bad_window.exit_code != 0

    bad_samples = runner.invoke(cli, ["health", "effectiveness", "--min-samples", "0"])
    assert bad_samples.exit_code != 0
