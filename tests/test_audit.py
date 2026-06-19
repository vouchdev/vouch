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
