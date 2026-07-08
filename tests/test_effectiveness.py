from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from vouch import index_db
from vouch.audit import log_event, read_events
from vouch.cli import cli
from vouch.eval.effectiveness import compute_effectiveness
from vouch.models import Claim
from vouch.storage import KBStore

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def _ev(kb_dir: Path, event: str, actor: str, object_ids: list[str], ts: datetime) -> None:
    log_event(kb_dir, event=event, actor=actor, object_ids=object_ids)
    path = kb_dir / "audit.log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[-1])
    payload["created_at"] = ts.isoformat()
    lines[-1] = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_artifacts(store: KBStore) -> None:
    src = store.put_source(b"effectiveness fixture")
    store.put_claim(Claim(id="c-good", text="good artifact", evidence=[src.id]))
    store.put_claim(Claim(id="c-noisy", text="noisy artifact", evidence=[src.id]))


def test_effectiveness_insufficient_sample_path(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    _seed_artifacts(store)
    base = NOW - timedelta(days=1)
    _ev(store.kb_dir, "session.start", "a1", ["sess-g1"], base)
    _ev(store.kb_dir, "claim.confirm", "a1", ["c-good"], base + timedelta(minutes=1))
    _ev(store.kb_dir, "session.end", "a1", ["sess-g1"], base + timedelta(minutes=2))
    _ev(store.kb_dir, "session.start", "a2", ["sess-b1"], base + timedelta(hours=1))
    _ev(
        store.kb_dir,
        "claim.contradict",
        "a2",
        ["c-good", "c-noisy"],
        base + timedelta(hours=1, minutes=1),
    )
    _ev(store.kb_dir, "session.end", "a2", ["sess-b1"], base + timedelta(hours=1, minutes=2))
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_context_surface(
            conn,
            session_id="sess-g1",
            query="q",
            surfaced_at=(base + timedelta(minutes=1)).isoformat(),
            items=[("claim", "c-good")],
        )

    report = compute_effectiveness(store, window="7d", min_samples=3, now=NOW)
    row = next(r for r in report["artifacts"] if r["artifact_id"] == "c-good")
    assert row["samples"] == 1
    assert row["verdict"] == "insufficient"


def test_effectiveness_clear_signal_with_fixed_clock(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    _seed_artifacts(store)
    base = NOW - timedelta(days=2)
    with index_db.open_db(store.kb_dir) as conn:
        for i in range(12):
            sid = f"sess-{i}"
            actor = f"agent-{i}"
            start = base + timedelta(minutes=i * 10)
            _ev(store.kb_dir, "session.start", actor, [sid], start)
            if i < 6:
                _ev(store.kb_dir, "claim.confirm", actor, ["c-good"], start + timedelta(minutes=2))
                index_db.index_context_surface(
                    conn,
                    session_id=sid,
                    query="good-query",
                    surfaced_at=(start + timedelta(minutes=1)).isoformat(),
                    items=[("claim", "c-good")],
                )
            else:
                _ev(
                    store.kb_dir,
                    "claim.contradict",
                    actor,
                    ["c-good", "c-noisy"],
                    start + timedelta(minutes=2),
                )
            _ev(store.kb_dir, "session.end", actor, [sid], start + timedelta(minutes=5))

    report = compute_effectiveness(store, window="30d", min_samples=5, now=NOW)
    row = next(r for r in report["artifacts"] if r["artifact_id"] == "c-good")
    assert report["sessions"]["baseline_rate"] == 0.5
    assert row["samples"] == 6
    assert row["verdict"] == "useful"
    assert row["wilson_95"]["low"] > report["sessions"]["baseline_rate"]


def test_effectiveness_is_read_only_and_reset_clears_surface_table(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    _seed_artifacts(store)
    base = NOW - timedelta(hours=5)
    _ev(store.kb_dir, "session.start", "r", ["sess-r"], base)
    _ev(store.kb_dir, "claim.confirm", "r", ["c-good"], base + timedelta(minutes=1))
    _ev(store.kb_dir, "session.end", "r", ["sess-r"], base + timedelta(minutes=2))
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_context_surface(
            conn,
            session_id="sess-r",
            query="read-only",
            surfaced_at=(base + timedelta(minutes=1)).isoformat(),
            items=[("claim", "c-good")],
        )
    events_before = len(list(read_events(store.kb_dir)))
    rows_before = len(index_db.read_context_surfaces(store.kb_dir))

    _ = compute_effectiveness(store, window="7d", min_samples=1, now=NOW)

    assert len(list(read_events(store.kb_dir))) == events_before
    assert len(index_db.read_context_surfaces(store.kb_dir)) == rows_before

    index_db.reset(store.kb_dir)
    assert index_db.read_context_surfaces(store.kb_dir) == []


def test_cli_health_effectiveness_json(tmp_path: Path, monkeypatch) -> None:
    store = KBStore.init(tmp_path)
    _seed_artifacts(store)
    base = NOW - timedelta(hours=4)
    _ev(store.kb_dir, "session.start", "a", ["sess-a"], base)
    _ev(store.kb_dir, "claim.confirm", "a", ["c-good"], base + timedelta(minutes=1))
    _ev(store.kb_dir, "session.end", "a", ["sess-a"], base + timedelta(minutes=2))
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_context_surface(
            conn,
            session_id="sess-a",
            query="cli",
            surfaced_at=(base + timedelta(minutes=1)).isoformat(),
            items=[("claim", "c-good")],
        )
    monkeypatch.chdir(store.root)
    res = CliRunner().invoke(cli, ["health", "effectiveness", "--format", "json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["schema_version"] == 1
    assert "artifacts" in payload
