"""End-to-end smoke test for the review console (vouchdev/vouch#194).

The issue names this file explicitly in its acceptance criteria:

    "E2E smoke test (`pytest tests/test_web_e2e.py`) drives the approve flow
     and asserts an `audit.log.jsonl` entry lands."

So this module drives the *whole* flow the way a browser would — propose →
queue render → approve via form POST → durable claim on disk → audit entry —
plus the full-spec surfaces the MVP slice deferred: WebSocket sync, the
session and source views, pagination, and the Bearer-auth gate. Everything
goes through the real FastAPI app via Starlette's TestClient (no mocks), so a
green run is real evidence the gate works through the web surface.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from vouch import audit as audit_mod
from vouch.models import ClaimStatus, ProposalStatus, Session
from vouch.proposals import propose_claim
from vouch.storage import KBStore
from vouch.web import create_app

# The web surface lives behind the [web] extra. Skip the whole module cleanly
# when it isn't installed (CI installs `.[dev,web]`, so it runs there).
pytest.importorskip("fastapi", reason="vouch review-ui needs the [web] extra")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


@pytest.fixture
def client(store: KBStore):
    return TestClient(create_app(str(store.root)))


def _seed(store: KBStore, text: str = "the sky is blue", *, by: str = "agent-A",
          session_id: str | None = None) -> str:
    src = store.put_source(b"evidence-" + text.encode())
    pr = propose_claim(store, text=text, evidence=[src.id], proposed_by=by,
                       session_id=session_id)
    return pr.id


def _audit_events(store: KBStore) -> list:
    return list(audit_mod.read_events(store.kb_dir))


# --- the headline acceptance test -----------------------------------------


def test_approve_flow_lands_durable_claim_and_audit_entry(
    client: TestClient, store: KBStore
) -> None:
    """Drive the approve flow end to end and assert an audit.log.jsonl entry
    lands — the exact criterion the issue calls out for this file."""
    pid = _seed(store, "claims that survive the gate are durable")

    # 1. queue renders the pending proposal.
    r = client.get("/")
    assert r.status_code == 200
    assert pid in r.text

    # 2. approve via the same form POST a browser submits (no-JS path).
    r = client.post(f"/approve/{pid}", data={}, follow_redirects=False)
    assert r.status_code == 303  # see-other back to the queue

    # 3. the proposal is gone from pending and a durable claim exists.
    assert not any(p.id == pid for p in store.list_proposals(ProposalStatus.PENDING))
    claims = store.list_claims()
    assert claims, "approve should have produced a durable claim"

    # 4. the audit log carries the approve event — written through the SAME
    #    proposals.approve code path the CLI uses.
    approves = [e for e in _audit_events(store) if e.event.endswith(".approve")]
    assert approves, "no approve event landed in audit.log.jsonl"
    assert (store.kb_dir / "audit.log.jsonl").exists()
    # and it's real JSONL on disk, not just an in-memory artifact.
    raw = (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["event"].endswith(".approve") for line in raw if line.strip())


def test_reject_flow_lands_rejection_in_audit(client: TestClient, store: KBStore) -> None:
    pid = _seed(store, "this one gets rejected")
    r = client.post(f"/reject/{pid}", data={"reason": "duplicate of c-001"},
                    follow_redirects=False)
    assert r.status_code == 303
    rejects = [e for e in _audit_events(store) if e.event.endswith(".reject")]
    assert rejects
    assert rejects[-1].data.get("reason") == "duplicate of c-001"


def test_reject_without_reason_is_refused(client: TestClient, store: KBStore) -> None:
    pid = _seed(store)
    r = client.post(f"/reject/{pid}", data={"reason": "   "})
    assert r.status_code == 400
    # still pending — no silent half-state.
    assert any(p.id == pid for p in store.list_proposals(ProposalStatus.PENDING))


# --- WebSocket realtime sync (two windows stay in sync) -------------------


def test_websocket_broadcasts_on_approve(client: TestClient, store: KBStore) -> None:
    """A second reviewer's socket receives a refresh frame within the same
    request that performed the approve — the <1s sync criterion. We assert the
    frame arrives well within the budget (it's normally single-digit ms)."""
    pid = _seed(store, "broadcast me")
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        # Perform the approve through the HTTP surface; the route broadcasts.
        t0 = time.monotonic()
        client.post(f"/approve/{pid}", follow_redirects=False)
        frame = ws.receive_json()
        elapsed = time.monotonic() - t0
        assert frame["type"] == "refresh"
        assert frame["view"] == "queue"
        assert frame["proposal_id"] == pid
        # #194 criterion: two windows sync within 1s. Generous bound — the real
        # number is milliseconds — but it fails loudly if a regression made the
        # broadcast block (e.g. a slow-client stall in _Hub.broadcast).
        assert elapsed < 1.0, f"broadcast took {elapsed:.3f}s, must be <1s"


def test_healthz_reports_clients_and_auth(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["auth"] is False
    assert "clients" in body


def test_static_assets_are_served(client: TestClient) -> None:
    """The progressive-enhancement JS/CSS must actually be reachable through
    the app (and therefore present in the package), not just on the dev's
    disk — guards against the assets being dropped from the wheel."""
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "WebSocket" in r.text          # it's our app.js, not a stray file
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert ".queue-row" in r.text         # it's our stylesheet


# --- session view: proposals grouped by agent run ------------------------


def test_session_view_groups_proposals(client: TestClient, store: KBStore) -> None:
    sess = Session(id="run-1", agent="claude", task="seed the KB")
    p1 = _seed(store, "first finding", session_id="run-1")
    p2 = _seed(store, "second finding", session_id="run-1")
    sess.proposal_ids = [p1, p2]
    store.put_session(sess)

    r = client.get("/session/run-1")
    assert r.status_code == 200
    assert "claude" in r.text
    assert p1 in r.text and p2 in r.text
    assert "seed the KB" in r.text


def test_session_view_404_for_unknown(client: TestClient) -> None:
    assert client.get("/session/nope").status_code == 404


def test_queue_links_to_session(client: TestClient, store: KBStore) -> None:
    _seed(store, "from a run", session_id="run-xyz")
    r = client.get("/")
    assert "/session/run-xyz" in r.text


# --- source reverse-index view --------------------------------------------


def test_source_view_reverse_index(client: TestClient, store: KBStore) -> None:
    src = store.put_source(b"the canonical source bytes")
    # Approve a claim that cites the source so it's durable and shows up.
    pr = propose_claim(store, text="cites the source", evidence=[src.id],
                       proposed_by="agent-A")
    client.post(f"/approve/{pr.id}")

    r = client.get(f"/sources/{src.id}")
    assert r.status_code == 200
    assert "reverse index" in r.text.lower()
    assert "cites the source" in r.text


def test_source_view_404_for_unknown(client: TestClient) -> None:
    assert client.get("/sources/deadbeef").status_code == 404


# --- pagination (500-item queue) ------------------------------------------


def test_pagination_first_page_is_bounded(store: KBStore) -> None:
    """A large queue only ever materialises one page of rows server-side."""
    app = create_app(str(store.root), page_size=20)
    client = TestClient(app)
    for i in range(45):
        _seed(store, f"proposal number {i}", by=f"agent-{i % 3}")

    r = client.get("/")
    assert r.status_code == 200
    # 45 items, page size 20 -> 3 pages, first page shows 20 rows.
    assert r.text.count('class="queue-row"') == 20
    assert "page 1 / 3" in r.text

    r2 = client.get("/?page=3")
    assert r2.text.count('class="queue-row"') == 5  # remainder

    # out-of-range clamps to the last page rather than 404-ing.
    r4 = client.get("/?page=99")
    assert r4.status_code == 200
    assert "page 3 / 3" in r4.text


def test_api_pending_pagination_envelope(store: KBStore) -> None:
    app = create_app(str(store.root), page_size=10)
    client = TestClient(app)
    for i in range(25):
        _seed(store, f"p{i}")
    body = client.get("/api/pending").json()
    assert body["count"] == 25
    assert body["pages"] == 3
    assert len(body["items"]) == 10


def test_pagination_parses_only_one_page_of_500(store: KBStore, monkeypatch) -> None:
    """The <200ms criterion rests on NOT deserialising the whole queue. Prove
    it deterministically (no flaky wall-clock): with 500 pending proposals and
    a page size of 50, a first-page request must parse exactly 50 files."""
    from vouch.web import server as web_server

    for i in range(500):
        _seed(store, f"bulk {i}")

    parsed = {"n": 0}
    real_load = web_server._yaml_load

    def counting_load(text: str):
        parsed["n"] += 1
        return real_load(text)

    monkeypatch.setattr(web_server, "_yaml_load", counting_load)

    app = create_app(str(store.root), page_size=50)
    client = TestClient(app)
    r = client.get("/")

    assert r.status_code == 200
    assert r.text.count('class="queue-row"') == 50
    assert "500 proposals" in r.text          # the full count is still honest
    # The mechanism IS the latency guarantee: only one page of files is
    # deserialised, never the whole queue. We assert the parse count rather
    # than a wall-clock bound — the count is machine-independent, whereas a
    # millisecond threshold flakes on a loaded CI runner. A regression to an
    # O(whole-queue) scan would parse 500 here and fail loudly.
    assert parsed["n"] == 50, f"parsed {parsed['n']} files, expected 50 (one page)"


def test_queue_survives_a_malformed_proposal_file(client: TestClient, store: KBStore) -> None:
    """One corrupt proposal YAML must not 500 the whole queue — the gate has to
    stay reviewable. The valid proposals still render; the bad file is skipped."""
    good = _seed(store, "a perfectly good proposal")
    # Drop a garbage file straight into proposed/ (bypassing the model).
    (store.kb_dir / "proposed" / "00000000-garbage.yaml").write_text(
        "this: is: not: valid: yaml: {[", encoding="utf-8",
    )
    r = client.get("/")
    assert r.status_code == 200
    assert good in r.text
    # /api/pending stays up too.
    assert client.get("/api/pending").status_code == 200


# --- Bearer auth (team mode) ----------------------------------------------


def test_auth_required_when_token_set(store: KBStore) -> None:
    app = create_app(str(store.root), auth_token="s3cret", auth_label="alice")
    client = TestClient(app)
    # No token -> 401 on a guarded route.
    assert client.get("/").status_code == 401
    # Wrong token -> 401.
    assert client.get("/", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Right token via header -> 200.
    assert client.get("/", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    # Right token via ?token= (the browser's first navigation) -> 200.
    assert client.get("/?token=s3cret").status_code == 200


def test_auth_label_is_recorded_as_reviewer(store: KBStore) -> None:
    """Token-authed approvals attribute to the token's label in the audit log."""
    app = create_app(str(store.root), auth_token="s3cret", auth_label="alice")
    client = TestClient(app)
    pid = _seed(store, "attribute me to alice")
    r = client.post(f"/approve/{pid}", headers={"Authorization": "Bearer s3cret"},
                    follow_redirects=False)
    assert r.status_code == 303
    approves = [e for e in _audit_events(store) if e.event.endswith(".approve")]
    assert approves[-1].actor == "alice"


def test_healthz_is_open_even_with_auth(store: KBStore) -> None:
    """/healthz stays unauthenticated so a load balancer can probe it."""
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200


def test_websocket_requires_token_when_auth_on(store: KBStore) -> None:
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    # Without the token query param the socket is closed (policy code 4401);
    # the server-side close surfaces as a WebSocketDisconnect on receive.
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.receive_json()
    # With the token it connects.
    with client.websocket_connect("/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "hello"


# --- auth hardening (security review follow-ups) --------------------------


def test_query_token_bootstraps_httponly_cookie_and_strips_url(store: KBStore) -> None:
    """A ?token= navigation must NOT 200 with the token still in the URL: it
    303-redirects to the bare path and moves the token into an HttpOnly,
    SameSite=Strict cookie so it can't be bookmarked, logged, or read by JS."""
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    r = client.get("/?token=s3cret", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # token stripped from the redirect target
    set_cookie = r.headers["set-cookie"].lower()
    assert "vouch_review_token=s3cret" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_cookie_authenticates_subsequent_requests(store: KBStore) -> None:
    """After the bootstrap the HttpOnly cookie alone authenticates — no header,
    no query param needed (this is the steady-state browser path)."""
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    # Follow the bootstrap once; httpx stashes the cookie in the jar.
    assert client.get("/?token=s3cret").status_code == 200
    # A later request with neither header nor query carries only the cookie.
    assert client.get("/", headers={}).status_code == 200
    assert client.get("/audit").status_code == 200


def test_wrong_cookie_is_rejected(store: KBStore) -> None:
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    client.cookies.set("vouch_review_token", "forged")
    assert client.get("/").status_code == 401


def test_websocket_authenticates_via_cookie(store: KBStore) -> None:
    """The browser can't set a WS header, but the same-origin cookie rides the
    handshake — so the realtime channel works in team mode without a token in
    the URL."""
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    client.cookies.set("vouch_review_token", "s3cret")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_post_with_query_token_does_not_redirect(store: KBStore) -> None:
    """A non-GET carrying a valid ?token= authenticates inline rather than
    303-redirecting (a redirect would drop the POST body)."""
    app = create_app(str(store.root), auth_token="s3cret")
    client = TestClient(app)
    pid = _seed(store, "approve via query token on POST")
    r = client.post(f"/approve/{pid}?token=s3cret", follow_redirects=False)
    # 303 back to the queue (the normal approve redirect), NOT the auth bootstrap.
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert not any(p.id == pid for p in store.list_proposals(ProposalStatus.PENDING))


def test_static_assets_carry_no_token_in_js(store: KBStore) -> None:
    """Defence-in-depth: the shipped JS must not handle or store the token
    (the HttpOnly cookie does), so an XSS can't read it from JS state. We
    check for token-*handling* constructs, not the literal word (which appears
    in the comment explaining why there is none)."""
    from vouch.web import server as web_server
    js = (web_server._STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "sessionStorage" not in js
    assert "localStorage" not in js
    # no token plucked from the URL or appended to one
    assert 'getItem("vouch-token")' not in js
    assert "token=" not in js
    assert 'searchParams.get("token")' not in js


# --- contradict gate action -----------------------------------------------


def test_contradict_marks_both_claims_contested(client: TestClient, store: KBStore) -> None:
    src = store.put_source(b"shared")
    a = propose_claim(store, text="the build is green", evidence=[src.id],
                      proposed_by="agent-A")
    b = propose_claim(store, text="the build is red", evidence=[src.id],
                      proposed_by="agent-B")
    client.post(f"/approve/{a.id}")
    client.post(f"/approve/{b.id}")
    ca = next(c for c in store.list_claims() if "green" in c.text)
    cb = next(c for c in store.list_claims() if "red" in c.text)

    r = client.post(f"/contradict/{ca.id}", data={"against": cb.id},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.get_claim(ca.id).status == ClaimStatus.CONTESTED
    assert store.get_claim(cb.id).status == ClaimStatus.CONTESTED
