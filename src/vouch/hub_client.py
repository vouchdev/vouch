"""Client for VouchHub — the authorization + sync option for a local KB.

The hub stores only approved knowledge; this client never sends sessions,
decided proposals, or config.yaml (`SYNC_EXCLUDE`). The secret token lives
OUTSIDE the KB (vouch KBs are meant to be committed to git):

  - link metadata (url, remote kb, last seen bundle id) → .vouch/hub.yaml
    (never exported: bundles carry only artifact subdirs + config.yaml)
  - token → $XDG_CONFIG_HOME|~/.config/vouch/hub.yaml, chmod 0600,
    keyed by hub url; the VOUCH_HUB_TOKEN env var overrides.

Pulls are gated: a bundle is applied only when conflict-free, unless the
caller explicitly chooses --on-conflict skip|overwrite. The hub is additive
and conflict-free on push; conflicts must be resolved here, at the owner's
gate, never server-side.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import bundle
from .storage import KBStore

SYNC_EXCLUDE = ("config.yaml", *bundle.KNOWLEDGE_EXCLUDE)
LINK_FILE = "hub.yaml"
_TIMEOUT = 30.0


class HubError(RuntimeError):
    """Any hub interaction failure with a human-readable message."""


class HubConflict(HubError):
    def __init__(self, conflicts: list[str]):
        super().__init__(
            f"{len(conflicts)} conflicting artifact(s) on the hub — pull, resolve locally, push again"
        )
        self.conflicts = conflicts


@dataclass
class HubLink:
    url: str
    kb: str  # "user/slug"
    last_bundle_id: str | None = None


# --- link metadata (inside the KB, no secrets) ---------------------------------


def load_link(kb_dir: Path) -> HubLink | None:
    path = kb_dir / LINK_FILE
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data.get("url") or not data.get("kb"):
        return None
    return HubLink(
        url=str(data["url"]).rstrip("/"),
        kb=str(data["kb"]),
        last_bundle_id=data.get("last_bundle_id"),
    )


def save_link(kb_dir: Path, link: HubLink) -> None:
    path = kb_dir / LINK_FILE
    path.write_text(
        yaml.safe_dump(
            {"url": link.url.rstrip("/"), "kb": link.kb, "last_bundle_id": link.last_bundle_id},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# --- token store (outside the KB) -----------------------------------------------


def _creds_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vouch" / "hub.yaml"


def resolve_token(url: str) -> str | None:
    env = os.environ.get("VOUCH_HUB_TOKEN")
    if env:
        return env
    path = _creds_path()
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    token = (data.get("tokens") or {}).get(url.rstrip("/"))
    return str(token) if token else None


def save_token(url: str, token: str) -> None:
    path = _creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("tokens", {})[url.rstrip("/")] = token
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


# --- http ------------------------------------------------------------------------


def _bundle_url(link: HubLink) -> str:
    return f"{link.url}/api/u/{link.kb}/bundle"


def _request(
    method: str,
    url: str,
    token: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "vouch-hub-client")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 — user-configured hub url
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()
    except urllib.error.URLError as e:
        raise HubError(f"cannot reach hub at {url}: {e.reason}") from e


def _error_message(status: int, payload: bytes) -> str:
    try:
        msg = json.loads(payload.decode()).get("error", "")
    except (ValueError, UnicodeDecodeError):
        msg = ""
    hints = {
        401: "token invalid or revoked — run `vouch hub link` again",
        403: "token lacks the sync scope",
        404: "kb not found on the hub (or you are not its owner)",
    }
    return msg or hints.get(status, f"hub returned HTTP {status}")


# --- operations -------------------------------------------------------------------


def push(store: KBStore, link: HubLink, token: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="vouch-hub-") as tmp:
        out = Path(tmp) / "knowledge.tar.gz"
        bundle.export(store.kb_dir, dest=out, actor="hub-push", exclude=SYNC_EXCLUDE)
        status, _headers, payload = _request(
            "PUT",
            _bundle_url(link),
            token,
            body=out.read_bytes(),
            headers={"Content-Type": "application/gzip"},
        )
    if status == 409:
        try:
            conflicts = json.loads(payload.decode()).get("conflicts", [])
        except (ValueError, UnicodeDecodeError):
            conflicts = []
        raise HubConflict(list(conflicts))
    if status != 200:
        raise HubError(_error_message(status, payload))
    result = json.loads(payload.decode())
    link.last_bundle_id = result.get("bundle_id")
    save_link(store.kb_dir, link)
    written = int(result.get("written", 0))
    return {
        "status": "pushed" if written else "up_to_date",
        "bundle_id": result.get("bundle_id"),
        "written": written,
        "identical": int(result.get("identical", 0)),
    }


def pull(
    store: KBStore,
    link: HubLink,
    token: str,
    *,
    on_conflict: str | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if link.last_bundle_id:
        headers["If-None-Match"] = f'"{link.last_bundle_id}"'
    status, resp_headers, payload = _request("GET", _bundle_url(link), token, headers=headers)
    if status == 304:
        return {"status": "up_to_date", "bundle_id": link.last_bundle_id}
    if status != 200:
        raise HubError(_error_message(status, payload))
    remote_id = (resp_headers.get("ETag") or "").strip('"') or None

    with tempfile.TemporaryDirectory(prefix="vouch-hub-") as tmp:
        bundle_path = Path(tmp) / "pulled.tar.gz"
        bundle_path.write_bytes(payload)
        check = bundle.import_check(store.kb_dir, bundle_path)
        if check.issues:
            raise HubError(f"pulled bundle failed validation: {check.issues[0]}")
        if check.conflicts and on_conflict is None:
            return {"status": "conflicts", "conflicts": check.conflicts}
        result = bundle.import_apply(
            store.kb_dir,
            bundle_path,
            on_conflict=on_conflict or "fail",
            actor="hub-pull",
        )
    link.last_bundle_id = remote_id
    save_link(store.kb_dir, link)
    return {
        "status": "applied",
        "bundle_id": remote_id,
        "written": len(result.get("written", [])),
        "skipped_conflicts": result.get("skipped_conflicts", []),
    }


def status(store: KBStore, link: HubLink, token: str | None) -> dict[str, Any]:
    local_id = bundle.build_manifest(store.kb_dir, SYNC_EXCLUDE)["bundle_id"]
    out: dict[str, Any] = {
        "linked": True,
        "url": link.url,
        "kb": link.kb,
        "local_bundle_id": local_id,
        "in_sync": None,
    }
    if token:
        code, _headers, _payload = _request(
            "GET", _bundle_url(link), token, headers={"If-None-Match": f'"{local_id}"'}
        )
        out["in_sync"] = code == 304
    return out
