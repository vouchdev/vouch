"""Tests for the browser review console (`vouch review-ui`).

Covers the MVP-slice acceptance criteria from issue #194:

* queue renders an HTML response listing pending proposals
* approve POST routes through ``proposals.approve`` and lands the durable
  artifact + the audit-log entry (same code path as the CLI)
* reject POST requires a reason and lands the rejection in the audit log
* the audit timeline view surfaces those decisions
* missing-reason on reject returns 400 (no silent half-state)
* a non-existent KB root raises a clean error rather than 500-ing per
  request
* `vouch review-ui --bind 0.0.0.0:...` refuses to start in the MVP slice
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import audit as audit_mod
from vouch.cli import cli
from vouch.models import ProposalStatus
from vouch.proposals import propose_claim
from vouch.storage import KBStore
from vouch.web import create_app

# The web surface lives behind the [web] extra. Skip the whole module cleanly
# when it isn't installed (CI installs `.[dev,web]`, so it runs there).
pytest.importorskip("fastapi", reason="vouch review-ui needs the [web] extra")

from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


@pytest.fixture
def app(store: KBStore):
    return create_app(str(store.root))


@pytest.fixture
def client(app):
    return TestClient(app)


def _seed_proposal(store: KBStore, text: str = "the sky is blue") -> str:
    """Helper: register a tiny source, then file a claim proposal that
    cites it. Returns the proposal id."""
    src = store.put_source(b"some evidence")
    pr = propose_claim(
        store, text=text, evidence=[src.id], proposed_by="agent-A",
    )
    return pr.id


# --- queue ---------------------------------------------------------------


def test_queue_renders_html_with_pending(client: TestClient, store: KBStore) -> None:
    pid = _seed_proposal(store, "the queue should render this claim")
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert pid in r.text
    assert "the queue should render this claim" in r.text
    assert "agent-A" in r.text


def test_queue_empty_state(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "no pending proposals" in r.text


def test_api_pending_json(client: TestClient, store: KBStore) -> None:
    pid = _seed_proposal(store)
    r = client.get("/api/pending")
    assert r.status_code == 200
    body = r.json()
    # Full-spec shape: paginated envelope {count, page, pages, items}.
    assert body["count"] == 1
    items = body["items"]
    assert any(item["id"] == pid for item in items)
    assert items[0]["proposed_by"] == "agent-A"


# --- claim detail --------------------------------------------------------


def test_claim_detail_renders(client: TestClient, store: KBStore) -> None:
    pid = _seed_proposal(store, "detail view shows the full payload")
    r = client.get(f"/claim/{pid}")
    assert r.status_code == 200
    assert pid in r.text
    assert "detail view shows the full payload" in r.text
    assert "rationale" not in r.text or "agent-A" in r.text


def test_claim_detail_404_for_unknown_id(client: TestClient) -> None:
    r = client.get("/claim/proposal-does-not-exist")
    assert r.status_code == 404


# --- approve / reject ----------------------------------------------------


def test_approve_routes_through_proposals_module(
    client: TestClient, store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approve POST must hit the same code path as `vouch approve`,
    which means the proposal moves to `decided/` and a durable claim
    lands in `claims/` + an audit entry in `audit.log.jsonl`."""
    # The MVP web layer uses VOUCH_AGENT as the reviewer identity; set it
    # to something other than the proposer so we don't trip self-approval.
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")

    pid = _seed_proposal(store, "approve me")
    r = client.post(f"/approve/{pid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    # The proposal moved out of pending.
    assert not store.list_proposals(ProposalStatus.PENDING)
    # And a durable claim now exists with the same payload text.
    assert any(c.text == "approve me" for c in store.list_claims())
    # And an audit event was logged with the right shape.
    events = [
        e for e in audit_mod.read_events(store.kb_dir)
        if e.event == "proposal.claim.approve"
    ]
    assert len(events) == 1
    assert events[0].actor == "human-reviewer"
    assert pid in events[0].object_ids


def test_reject_requires_reason(
    client: TestClient, store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    pid = _seed_proposal(store, "reject me")

    # Missing form field → 422 (FastAPI's validation surface).
    r = client.post(f"/reject/{pid}", follow_redirects=False)
    assert r.status_code in (400, 422)

    # Whitespace-only reason → 400 from our explicit guard.
    r = client.post(
        f"/reject/{pid}", data={"reason": "   "}, follow_redirects=False,
    )
    assert r.status_code == 400


def test_reject_lands_audit_event(
    client: TestClient, store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    pid = _seed_proposal(store, "wrong claim")

    r = client.post(
        f"/reject/{pid}",
        data={"reason": "not a fact, an opinion"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    events = [
        e for e in audit_mod.read_events(store.kb_dir)
        if e.event == "proposal.claim.reject"
    ]
    assert len(events) == 1
    assert events[0].data.get("reason") == "not a fact, an opinion"


def test_approve_unknown_proposal_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    r = client.post("/approve/nope", follow_redirects=False)
    assert r.status_code == 400


# --- audit timeline ------------------------------------------------------


def test_audit_view_shows_recent_decisions(
    client: TestClient, store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    pid = _seed_proposal(store, "audit me")
    client.post(f"/approve/{pid}")

    r = client.get("/audit")
    assert r.status_code == 200
    assert "proposal.claim.approve" in r.text
    assert "human-reviewer" in r.text


def test_audit_view_empty_state(client: TestClient) -> None:
    r = client.get("/audit")
    assert r.status_code == 200
    assert "no review decisions" in r.text


# --- progressive enhancement: form posts work without JS -----------------


def test_form_post_redirects_and_renders_updated_queue(
    client: TestClient, store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approve flow must work as a plain browser form: submit the
    POST, follow the 303 redirect, see the queue rendered without the
    just-approved proposal. No JS in the loop."""
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    pid = _seed_proposal(store, "form-flow claim")

    r = client.post(f"/approve/{pid}", follow_redirects=True)
    assert r.status_code == 200
    assert pid not in r.text  # the approved row is gone from the queue
    assert "no pending proposals" in r.text


# --- healthz --------------------------------------------------------------


def test_healthz(client: TestClient, store: KBStore) -> None:
    _seed_proposal(store)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["pending"] == 1


# --- bring-up errors ------------------------------------------------------


def test_create_app_errors_without_vouch_dir(tmp_path: Path) -> None:
    """Building the app against a directory with no `.vouch/` should fail
    at bring-up time, not 500 on the first request."""
    from vouch.storage import KBNotFoundError

    bare = tmp_path / "empty"
    bare.mkdir()
    with pytest.raises(KBNotFoundError):
        create_app(str(bare))


# --- CLI: non-loopback bind requires --auth -------------------------------


def test_cli_review_ui_refuses_non_localhost_bind_without_auth(tmp_path: Path) -> None:
    """A non-loopback bind with no --auth is refused — we won't expose an
    unauthenticated approve surface on the network."""
    runner = CliRunner()
    KBStore.init(tmp_path)
    result = runner.invoke(
        cli,
        ["review-ui", "--bind", "0.0.0.0:7780", "--no-open-browser",
         "--kb", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "--auth" in result.output
    assert "non-loopback" in result.output.lower()
