"""HTTP server — route dispatch, auth rejection, error mapping."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

starlette = pytest.importorskip("starlette", reason="starlette not installed; skipping HTTP tests")

from starlette.testclient import TestClient  # noqa: E402

from vouch.http_server import build_app  # noqa: E402
from vouch.storage import KBStore  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


@pytest.fixture
def client_none_auth(store: KBStore, monkeypatch) -> TestClient:
    monkeypatch.chdir(store.root)
    app = build_app(auth_mode="none")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_bearer(store: KBStore, monkeypatch) -> TestClient:
    monkeypatch.chdir(store.root)
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "testtoken")
    app = build_app(auth_mode="bearer")
    return TestClient(app, raise_server_exceptions=False)


# --- auth rejection ---------------------------------------------------------


def test_missing_auth_returns_401(client_bearer: TestClient) -> None:
    resp = client_bearer.post("/kb/status", content="{}")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth_error"


def test_wrong_token_returns_401(client_bearer: TestClient) -> None:
    resp = client_bearer.post(
        "/kb/status", content="{}",
        headers={"Authorization": "Bearer wrongtoken"},
    )
    assert resp.status_code == 401


def test_valid_token_accepted(client_bearer: TestClient) -> None:
    resp = client_bearer.post(
        "/kb/status", content="{}",
        headers={"Authorization": "Bearer testtoken"},
    )
    assert resp.status_code == 200


def test_none_auth_no_header_accepted(client_none_auth: TestClient) -> None:
    resp = client_none_auth.post("/kb/status", content="{}")
    assert resp.status_code == 200


# --- route dispatch ---------------------------------------------------------


def test_status_returns_counts(client_none_auth: TestClient) -> None:
    resp = client_none_auth.post("/kb/status", content="{}")
    assert resp.status_code == 200
    data = resp.json()
    assert "claims" in data
    assert "pending_proposals" in data


def test_capabilities_response(client_none_auth: TestClient) -> None:
    resp = client_none_auth.post("/kb/capabilities", content="{}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["review_gated"] is True
    assert "kb.status" in data["methods"]


def test_unknown_method_returns_404(client_none_auth: TestClient) -> None:
    resp = client_none_auth.post("/kb/bogus_method", content="{}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "method_not_found"


def test_invalid_json_body_returns_422(client_none_auth: TestClient) -> None:
    resp = client_none_auth.post(
        "/kb/status",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_propose_and_list_pending(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "tok")
    app = build_app(auth_mode="bearer")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok"}

    src = store.put_source(b"test evidence")
    body = json.dumps({"text": "HTTP claim", "evidence": [src.id]})
    resp = client.post("/kb/propose_claim", content=body, headers=headers)
    assert resp.status_code == 200
    assert "proposal_id" in resp.json()

    resp2 = client.post("/kb/list_pending", content="{}", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1


def test_approve_flow(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "tok")

    # Configure trusted-agent so self-approval is allowed in this test
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("review", {})["approver_role"] = "trusted-agent"
    cfg_path.write_text(yaml.safe_dump(cfg))

    app = build_app(auth_mode="bearer")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok"}

    src = store.put_source(b"evidence")
    propose_body = json.dumps({"text": "approved claim", "evidence": [src.id]})
    pr_resp = client.post("/kb/propose_claim", content=propose_body, headers=headers)
    assert pr_resp.status_code == 200
    pid = pr_resp.json()["proposal_id"]

    approve_body = json.dumps({"proposal_id": pid})
    ap_resp = client.post("/kb/approve", content=approve_body, headers=headers)
    assert ap_resp.status_code == 200
    assert "id" in ap_resp.json()

    status_resp = client.post("/kb/status", content="{}", headers=headers)
    assert status_resp.json()["claims"] == 1


def test_actor_resolved_from_token_config(store: KBStore, monkeypatch) -> None:
    """Actor written to the audit log should come from the token config name."""
    token = "agenttoken"
    token_hash = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("server", {}).setdefault("tokens", []).append(
        {"name": "ci-agent", "token_hash": token_hash}
    )
    cfg.setdefault("review", {})["approver_role"] = "trusted-agent"
    cfg_path.write_text(yaml.safe_dump(cfg))

    monkeypatch.chdir(store.root)
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", token)
    app = build_app(auth_mode="bearer")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {token}"}

    src = store.put_source(b"ev")
    resp = client.post(
        "/kb/propose_claim",
        content=json.dumps({"text": "actor test", "evidence": [src.id]}),
        headers=headers,
    )
    assert resp.status_code == 200

    from vouch import audit
    events = list(audit.read_events(store.kb_dir))
    proposal_events = [e for e in events if "proposal" in e.event]
    assert proposal_events
    assert proposal_events[0].actor == "ci-agent"
