"""Reviewer notification webhooks — read-and-notify only."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from vouch import notify
from vouch.models import Proposal, ProposalKind
from vouch.storage import KBStore

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


class _Sink(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[dict, dict]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _Sink.received.append((body, dict(self.headers)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def sink_url() -> str:
    _Sink.received = []
    server = HTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def _pending(store: KBStore, pid: str, *, age_hours: int = 1) -> None:
    store.put_proposal(
        Proposal(
            id=pid, kind=ProposalKind.CLAIM, proposed_by="agent-a",
            proposed_at=NOW - timedelta(hours=age_hours),
            payload={"text": f"fact from {pid}"},
        )
    )


def _configure(store: KBStore, yaml_block: str) -> None:
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8") + "\n" + yaml_block,
        encoding="utf-8",
    )


def test_sweep_without_config_is_noop(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    _pending(store, "p1")
    assert notify.sweep(store, now=NOW) == []


def test_sweep_fires_created_once(tmp_path: Path, sink_url: str) -> None:
    store = KBStore.init(tmp_path)
    _configure(store, f"notify:\n  webhooks:\n    - url: {sink_url}\n")
    _pending(store, "p1")
    _pending(store, "p2")

    first = notify.sweep(store, now=NOW)
    second = notify.sweep(store, now=NOW)

    assert first == [notify.EVENT_CREATED]
    assert second == []
    body, _ = _Sink.received[0]
    assert body["event"] == "proposal.created"
    assert sorted(body["proposal_ids"]) == ["p1", "p2"]
    assert body["pending_count"] == 2
    # no proposal content unless include_summary is opted into
    assert "summaries" not in body


def test_sweep_aged_and_backlogged(tmp_path: Path, sink_url: str) -> None:
    store = KBStore.init(tmp_path)
    _configure(
        store,
        "notify:\n  webhooks:\n"
        f"    - url: {sink_url}\n"
        "      events: [proposal.aged, queue.backlogged]\n"
        "      backlog_threshold: 2\n"
        "      age_threshold: 48h\n",
    )
    _pending(store, "old", age_hours=72)
    _pending(store, "young", age_hours=1)

    fired = notify.sweep(store, now=NOW)
    assert sorted(fired) == ["proposal.aged", "queue.backlogged"]
    aged = next(b for b, _ in _Sink.received if b["event"] == "proposal.aged")
    assert aged["proposal_ids"] == ["old"]

    # idempotent: same state, nothing re-fires
    assert notify.sweep(store, now=NOW) == []


def test_sweep_signs_body_with_env_secret(
    tmp_path: Path, sink_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_TEST_NOTIFY_SECRET", "s3kr1t")
    store = KBStore.init(tmp_path)
    _configure(
        store,
        "notify:\n  webhooks:\n"
        f"    - url: {sink_url}\n"
        "      secret: env:VOUCH_TEST_NOTIFY_SECRET\n",
    )
    _pending(store, "p1")

    notify.sweep(store, now=NOW)

    body, headers = _Sink.received[0]
    expected = hmac.new(
        b"s3kr1t", json.dumps(body, sort_keys=True).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert headers.get(notify.SIGNATURE_HEADER) == expected


def test_unset_env_secret_fails_loudly(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    _configure(
        store,
        "notify:\n  webhooks:\n"
        "    - url: http://127.0.0.1:9/hook\n"
        "      secret: env:VOUCH_TEST_NOTIFY_UNSET\n",
    )
    with pytest.raises(notify.NotifyConfigError):
        notify.sweep(store, now=NOW)


def test_dead_endpoint_is_swallowed_and_retried(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    # port 9 (discard) refuses connections immediately
    _configure(store, "notify:\n  webhooks:\n    - url: http://127.0.0.1:9/hook\n")
    _pending(store, "p1")

    assert notify.sweep(store, now=NOW) == []
    # failed delivery is not marked announced — a later sweep retries
    assert notify._load_state(store.kb_dir)["created"] == []
