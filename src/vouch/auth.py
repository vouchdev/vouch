"""Bearer-token authentication and actor resolution for the HTTP transport.

Three auth modes (VEP-0004):
  none       — no check; server refuses to start unless bind is loopback.
  bearer     — static token from VOUCH_SERVER_TOKEN env or ~/.vouch/server.token.
  token-file — same as bearer but token is re-read on every request,
               enabling hot rotation without a restart.

Tokens are compared with hmac.compare_digest to avoid timing oracles.
Config stores sha256:<hex> hashes, not plaintext tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .models import ServerConfig


class AuthError(Exception):
    """Raised when a request fails authentication."""


_DEFAULT_TOKEN_FILE = Path.home() / ".vouch" / "server.token"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _load_server_config(kb_dir: Path) -> ServerConfig | None:
    cfg_path = kb_dir / "config.yaml"
    if not cfg_path.exists():
        return None
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    server_raw = raw.get("server")
    if not server_raw:
        return None
    from .models import ServerConfig
    return ServerConfig.model_validate(server_raw)


def resolve_actor(token: str, kb_dir: Path, fallback: str) -> str:
    """Return the actor name for a validated token.

    Checks the config.yaml server.tokens list first; falls back to fallback
    (which should be the VOUCH_AGENT env var or 'unknown-agent').
    """
    cfg = _load_server_config(kb_dir)
    if cfg is None or not cfg.tokens:
        return fallback
    candidate_hash = _sha256_hex(token)
    for entry in cfg.tokens:
        stored = entry.token_hash
        if stored.startswith("sha256:"):
            stored = stored[len("sha256:"):]
        if _safe_eq(candidate_hash, stored):
            return entry.name
    return fallback


def verify_token(token: str, mode: str, kb_dir: Path) -> bool:
    """Return True if the token is valid for the given auth mode."""
    if mode == "none":
        return True
    raw_token = _read_static_token(mode)
    if raw_token is None:
        return False
    return _safe_eq(_sha256_hex(token), _sha256_hex(raw_token))


def _read_static_token(mode: str) -> str | None:
    """Read the configured token from env or file."""
    env_token = os.environ.get("VOUCH_SERVER_TOKEN")
    if env_token:
        return env_token
    if _DEFAULT_TOKEN_FILE.exists():
        return _DEFAULT_TOKEN_FILE.read_text().strip()
    return None


def require_auth(authorization_header: str | None, mode: str, kb_dir: Path) -> str:
    """Extract and validate the bearer token from an Authorization header.

    Returns the raw token string so the caller can do actor resolution.
    Raises AuthError on any failure.
    """
    if mode == "none":
        return ""
    if not authorization_header:
        raise AuthError("missing Authorization header")
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header must be 'Bearer <token>'")
    token = parts[1].strip()
    if not token:
        raise AuthError("empty bearer token")
    if not verify_token(token, mode, kb_dir):
        raise AuthError("invalid token")
    return token


def assert_loopback_for_no_auth(bind: str) -> None:
    """Raise if bind address is not loopback when auth=none."""
    host = bind.split(":")[0]
    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_loopback:
            raise ValueError(
                f"--auth none is only allowed when --bind is a loopback address "
                f"(got {host!r}). Use --auth bearer or --auth token-file for "
                f"non-loopback binds."
            )
    except ValueError as e:
        if "loopback" in str(e):
            raise
        if host not in ("localhost",):
            raise ValueError(
                f"--auth none with non-loopback host {host!r} is not allowed"
            ) from e
