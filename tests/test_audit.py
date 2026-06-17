"""Audit log — append-only JSONL behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import audit
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_audit_log_appends(store: KBStore) -> None:
    audit.log_event(store.kb_dir, event="x.test", actor="u", object_ids=["a"])
    audit.log_event(store.kb_dir, event="x.test2", actor="u")
    events = list(audit.read_events(store.kb_dir))
    assert [e.event for e in events] == ["x.test", "x.test2"]
    assert audit.count_events(store.kb_dir) == 2


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
