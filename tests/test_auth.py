"""Auth layer — token validation, timing-safe compare, loopback enforcement."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from vouch.auth import (
    AuthError,
    assert_loopback_for_no_auth,
    require_auth,
    resolve_actor,
    verify_token,
)
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _write_token_config(store: KBStore, token: str, name: str) -> None:
    token_hash = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("server", {}).setdefault("tokens", []).append(
        {"name": name, "token_hash": token_hash}
    )
    cfg_path.write_text(yaml.safe_dump(cfg))


# --- verify_token -----------------------------------------------------------


def test_verify_token_none_mode_always_passes(store: KBStore, monkeypatch) -> None:
    monkeypatch.delenv("VOUCH_SERVER_TOKEN", raising=False)
    assert verify_token("anything", "none", store.kb_dir)


def test_verify_token_bearer_correct(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "secret123")
    assert verify_token("secret123", "bearer", store.kb_dir)


def test_verify_token_bearer_wrong(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "secret123")
    assert not verify_token("wrongtoken", "bearer", store.kb_dir)


def test_verify_token_token_file_reads_file(store: KBStore, monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "server.token"
    token_file.write_text("filetoken\n")
    monkeypatch.delenv("VOUCH_SERVER_TOKEN", raising=False)
    monkeypatch.setattr("vouch.auth._DEFAULT_TOKEN_FILE", token_file)
    assert verify_token("filetoken", "token-file", store.kb_dir)


def test_verify_token_missing_token_returns_false(store: KBStore, monkeypatch) -> None:
    monkeypatch.delenv("VOUCH_SERVER_TOKEN", raising=False)
    monkeypatch.setattr("vouch.auth._DEFAULT_TOKEN_FILE", Path("/nonexistent/token"))
    assert not verify_token("anything", "bearer", store.kb_dir)


# --- require_auth -----------------------------------------------------------


def test_require_auth_none_mode_no_header(store: KBStore) -> None:
    token = require_auth(None, "none", store.kb_dir)
    assert token == ""


def test_require_auth_bearer_valid(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "mytoken")
    token = require_auth("Bearer mytoken", "bearer", store.kb_dir)
    assert token == "mytoken"


def test_require_auth_missing_header_raises(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "mytoken")
    with pytest.raises(AuthError, match="missing Authorization"):
        require_auth(None, "bearer", store.kb_dir)


def test_require_auth_wrong_scheme_raises(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "mytoken")
    with pytest.raises(AuthError, match="Bearer"):
        require_auth("Basic dXNlcjpwYXNz", "bearer", store.kb_dir)


def test_require_auth_invalid_token_raises(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "mytoken")
    with pytest.raises(AuthError, match="invalid token"):
        require_auth("Bearer wrongtoken", "bearer", store.kb_dir)


# --- resolve_actor ----------------------------------------------------------


def test_resolve_actor_from_config(store: KBStore, monkeypatch) -> None:
    _write_token_config(store, "agenttoken", "ci-agent")
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "agenttoken")
    actor = resolve_actor("agenttoken", store.kb_dir, "fallback-actor")
    assert actor == "ci-agent"


def test_resolve_actor_falls_back_when_no_match(store: KBStore, monkeypatch) -> None:
    _write_token_config(store, "othertoken", "other-agent")
    monkeypatch.setenv("VOUCH_SERVER_TOKEN", "mytoken")
    actor = resolve_actor("mytoken", store.kb_dir, "fallback-actor")
    assert actor == "fallback-actor"


def test_resolve_actor_no_config(store: KBStore) -> None:
    actor = resolve_actor("anytoken", store.kb_dir, "default-agent")
    assert actor == "default-agent"


# --- loopback enforcement ---------------------------------------------------


def test_loopback_ok_for_127() -> None:
    assert_loopback_for_no_auth("127.0.0.1:7749")


def test_loopback_ok_for_localhost() -> None:
    assert_loopback_for_no_auth("localhost:7749")


def test_loopback_rejects_0000() -> None:
    with pytest.raises(ValueError, match="loopback"):
        assert_loopback_for_no_auth("0.0.0.0:7749")


def test_loopback_rejects_external_ip() -> None:
    with pytest.raises(ValueError, match="loopback"):
        assert_loopback_for_no_auth("10.0.0.1:7749")
