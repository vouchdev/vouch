"""Audit log — append-only JSONL behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import audit
from vouch.health import doctor
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_audit_log_appends(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="x.test", actor="u", object_ids=["a"])
    audit.log_event(store.kb_dir, event="x.test2", actor="u")
    events = list(audit.read_events(store.kb_dir))
    assert [e.event for e in events] == ["x.test", "x.test2"]
    assert events[0].prev_hash == audit.GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert events[0].hash is not None
    assert audit.count_events(store.kb_dir) == 2
    assert audit.verify_chain(store.kb_dir).ok


def test_audit_log_survives_malformed_lines(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="x", actor="u")
    (store.kb_dir / "audit.log.jsonl").open("a").write("garbage\n")
    events = list(audit.read_events(store.kb_dir))
    assert len(events) == 1


def test_read_events_unfiltered_by_default(store: KBStore) -> None:
    from vouch.models import ArtifactScope, Claim, Visibility

    src = store.put_source(b"e")
    store.put_claim(Claim(
        id="secret",
        text="x",
        evidence=[src.id],
        scope=ArtifactScope(visibility=Visibility.PROJECT, project="billing"),
    ))
    audit.log_event(store.kb_dir, event="claim.create", actor="u", object_ids=["secret"])
    events = list(audit.read_events(store.kb_dir))
    assert any("secret" in e.object_ids for e in events)


def test_audit_chain_detects_tampered_event(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="x.test", actor="u", object_ids=["a"])
    audit.log_event(store.kb_dir, event="x.test2", actor="u")
    path = store.kb_dir / "audit.log.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["event"] = "x.changed"
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")

    status = audit.verify_chain(store.kb_dir)
    assert not status.ok
    assert status.line == 1
    assert status.reason == "event hash mismatch"


def test_doctor_reports_broken_audit_chain(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="x.test", actor="u")
    path = store.kb_dir / "audit.log.jsonl"
    row = json.loads(path.read_text())
    row["hash"] = "f" * 64
    path.write_text(json.dumps(row, sort_keys=True) + "\n")

    report = doctor(store)
    assert not report.ok
    assert any(f.code == "audit_chain_broken" for f in report.findings)


def test_audit_chain_detects_legacy_events(store: KBStore) -> None:
    path = store.kb_dir / "audit.log.jsonl"
    path.write_text(json.dumps({
        "id": "legacy",
        "event": "x.test",
        "actor": "u",
        "created_at": "2026-01-01T00:00:00+00:00",
        "object_ids": [],
        "dry_run": False,
        "reversible": True,
        "data": {},
    }) + "\n")

    status = audit.verify_chain(store.kb_dir)
    assert not status.ok
    assert status.line == 1
    assert status.reason == "legacy event is not hash-chained"


def _writer_worker(kb_dir_str: str, actor: str, count: int) -> None:
    """Subprocess entry point for the concurrency test — module level so
    multiprocessing's spawn context can pickle it."""
    from pathlib import Path as _Path

    from vouch import audit as _audit
    kb_dir = _Path(kb_dir_str)
    for i in range(count):
        _audit.log_event(
            kb_dir, event="claim.create", actor=actor,
            object_ids=[f"{actor}-{i}"],
        )


def test_audit_chain_survives_concurrent_writers(store: KBStore) -> None:
    """Concurrent processes must not fork the chain.

    Without the file lock around _last_hash → derive → append, two workers
    observe the same prev_hash and both chain off it. verify_chain then
    reports "previous hash mismatch" at the second concurrent event. Use
    multiprocessing.Process — the GIL serialises Python code and hides the
    file-level race that threads would expose.
    """
    import multiprocessing as mp

    n_workers = 4
    per_worker = 20

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_writer_worker,
                    args=(str(store.kb_dir), f"agent-{i}", per_worker))
        for i in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    for p in procs:
        assert p.exitcode == 0, f"worker exit={p.exitcode}"

    status = audit.verify_chain(store.kb_dir)
    assert status.ok, f"chain broken at line {status.line}: {status.reason}"
    assert audit.count_events(store.kb_dir) == n_workers * per_worker


def test_audit_lock_released_on_exception(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer raising mid-event releases the lock so the next call lands."""
    import os as _os

    audit.log_event(store.kb_dir, event="x.first", actor="u")

    original_fsync = _os.fsync
    remaining = {"raises": 1}

    def _flaky_fsync(fd: int) -> None:
        if remaining["raises"] > 0:
            remaining["raises"] -= 1
            raise OSError("simulated I/O failure")
        original_fsync(fd)

    monkeypatch.setattr(_os, "fsync", _flaky_fsync)
    with pytest.raises(OSError, match="simulated I/O failure"):
        audit.log_event(store.kb_dir, event="x.boom", actor="u")

    monkeypatch.setattr(_os, "fsync", original_fsync)
    audit.log_event(store.kb_dir, event="x.third", actor="u")
    events = list(audit.read_events(store.kb_dir))
    # x.first and x.third land; x.boom may have written its line before the
    # fsync raised. In either case the surviving entries chain forward.
    assert events[0].event == "x.first"
    assert events[-1].event == "x.third"
