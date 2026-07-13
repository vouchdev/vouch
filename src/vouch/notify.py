"""Outbound reviewer notifications: the gate only works if a human notices.

Config-declared webhooks, fired by an operator-run sweep (cron / systemd
timer). Vouch is strictly the HTTP client; the endpoint belongs to the
operator — no inbound service, no polling by anyone else, no hosted
component. Read-and-notify only: nothing here proposes, approves, or edits;
a webhook's whole job is to make a human come look at the queue.

```yaml
notify:
  webhooks:
    - url: env:VOUCH_NOTIFY_URL          # env: ref keeps the secret out of git
      events: [proposal.created, queue.backlogged, proposal.aged]
      backlog_threshold: 25
      age_threshold: 48h
      secret: env:VOUCH_NOTIFY_SECRET    # optional hmac-sha256 body signing
      include_summary: false
```

Delivery is best-effort: timeouts and non-2xx are logged and swallowed so a
dead endpoint can never wedge a sweep. Idempotence state (which proposal ids
already fired which event) lives in the derived state.db — losing it merely
re-notifies, it can never lose knowledge.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import index_db
from .metrics import parse_since
from .models import ProposalStatus
from .storage import KBStore

logger = logging.getLogger(__name__)

EVENT_CREATED = "proposal.created"
EVENT_BACKLOGGED = "queue.backlogged"
EVENT_AGED = "proposal.aged"
ALL_EVENTS = (EVENT_CREATED, EVENT_BACKLOGGED, EVENT_AGED)

DEFAULT_TIMEOUT = 5.0
_STATE_KEY = "notify_state"
SIGNATURE_HEADER = "X-Vouch-Signature"


class NotifyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Webhook:
    url: str
    events: tuple[str, ...] = ALL_EVENTS
    backlog_threshold: int | None = None
    age_threshold: str | None = None
    secret: str | None = None
    include_summary: bool = False


def _resolve_env(raw: str, *, what: str) -> str:
    if raw.startswith("env:"):
        var = raw.removeprefix("env:").strip()
        if not var:
            raise NotifyConfigError(f"{what} 'env:' must be followed by a variable name")
        value = os.environ.get(var)
        if not value:
            raise NotifyConfigError(f"{what} references env:{var} but the variable is unset")
        return value
    return raw


def load_webhooks(store: KBStore) -> list[Webhook]:
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(loaded, dict):
        return []
    raw = loaded.get("notify")
    if not isinstance(raw, dict):
        return []
    hooks: list[Webhook] = []
    for item in raw.get("webhooks") or []:
        if not isinstance(item, dict) or not item.get("url"):
            raise NotifyConfigError("notify.webhooks entries need a url")
        events = item.get("events")
        hooks.append(
            Webhook(
                url=_resolve_env(str(item["url"]), what="notify.webhooks.url"),
                events=(
                    tuple(str(e) for e in events) if isinstance(events, list) else ALL_EVENTS
                ),
                backlog_threshold=(
                    int(item["backlog_threshold"])
                    if item.get("backlog_threshold") is not None
                    else None
                ),
                age_threshold=(
                    str(item["age_threshold"]) if item.get("age_threshold") else None
                ),
                secret=(
                    _resolve_env(str(item["secret"]), what="notify.webhooks.secret")
                    if item.get("secret")
                    else None
                ),
                include_summary=bool(item.get("include_summary", False)),
            )
        )
    return hooks


def deliver(
    hook_url: str,
    envelope: dict[str, object],
    *,
    secret: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """POST the envelope; log-and-swallow every failure. Returns success."""
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "vouch-notify"}
    if secret:
        headers[SIGNATURE_HEADER] = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
    req = urllib.request.Request(hook_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("notify delivery to %s failed: %s", hook_url, e)
        return False
    if not ok:
        logger.warning("notify delivery to %s got HTTP %s", hook_url, resp.status)
    return ok


def _load_state(kb_dir: Path) -> dict[str, list[str]]:
    raw = index_db.get_meta(kb_dir, _STATE_KEY)
    try:
        loaded = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        loaded = {}
    return {
        "created": list(loaded.get("created", [])),
        "aged": list(loaded.get("aged", [])),
        "backlogged": list(loaded.get("backlogged", [])),
    }


def _save_state(kb_dir: Path, state: dict[str, list[str]]) -> None:
    with index_db.open_db(kb_dir) as conn:
        index_db.set_meta(conn, _STATE_KEY, json.dumps(state, sort_keys=True))


def _envelope(
    store: KBStore,
    event: str,
    *,
    proposal_ids: list[str],
    pending_count: int,
    now: datetime,
    summaries: list[str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "event": event,
        "timestamp": now.isoformat(timespec="seconds"),
        "kb_path": str(store.kb_dir),
        "proposal_ids": proposal_ids,
        "pending_count": pending_count,
    }
    if summaries:
        body["summaries"] = summaries
    return body


def sweep(store: KBStore, *, now: datetime | None = None) -> list[str]:
    """Evaluate triggers against the pending queue and fire subscribed hooks.

    Idempotent per (event, proposal id): a re-run fires nothing new unless
    the queue changed. Returns the event names fired (with repeats per hook).
    """
    hooks = load_webhooks(store)
    if not hooks:
        return []
    now = now or datetime.now(UTC)
    pending = store.list_proposals(ProposalStatus.PENDING)
    pending_ids = [p.id for p in pending]
    state = _load_state(store.kb_dir)
    fired: list[str] = []

    for hook in hooks:
        if EVENT_CREATED in hook.events:
            new_ids = [pid for pid in pending_ids if pid not in state["created"]]
            if new_ids:
                summaries = (
                    [
                        str(p.payload.get("title") or p.payload.get("text") or "")[:120]
                        for p in pending
                        if p.id in new_ids
                    ]
                    if hook.include_summary
                    else None
                )
                if deliver(
                    hook.url,
                    _envelope(
                        store, EVENT_CREATED,
                        proposal_ids=new_ids, pending_count=len(pending),
                        now=now, summaries=summaries,
                    ),
                    secret=hook.secret,
                ):
                    fired.append(EVENT_CREATED)

        if (
            EVENT_BACKLOGGED in hook.events
            and hook.backlog_threshold is not None
            and len(pending) >= hook.backlog_threshold
        ):
            marker = f"{hook.url}:{hook.backlog_threshold}"
            if marker not in state["backlogged"] and deliver(
                hook.url,
                _envelope(
                    store, EVENT_BACKLOGGED,
                    proposal_ids=pending_ids, pending_count=len(pending), now=now,
                ),
                secret=hook.secret,
            ):
                fired.append(EVENT_BACKLOGGED)
                state["backlogged"].append(marker)

        if EVENT_AGED in hook.events and hook.age_threshold:
            cutoff = parse_since(hook.age_threshold, now=now)
            aged_ids = [
                p.id
                for p in pending
                if cutoff is not None
                and (
                    p.proposed_at.replace(tzinfo=UTC)
                    if p.proposed_at.tzinfo is None
                    else p.proposed_at
                )
                < cutoff
                and p.id not in state["aged"]
            ]
            if aged_ids and deliver(
                hook.url,
                _envelope(
                    store, EVENT_AGED,
                    proposal_ids=aged_ids, pending_count=len(pending), now=now,
                ),
                secret=hook.secret,
            ):
                fired.append(EVENT_AGED)
                state["aged"].extend(aged_ids)

    # created-state is hook-independent: a proposal counts as announced once
    # any hook accepted it. drop decided proposals so the state can't grow
    # without bound.
    if any(e == EVENT_CREATED for e in fired):
        state["created"] = sorted(set(state["created"]) | set(pending_ids))
    state["created"] = [pid for pid in state["created"] if pid in pending_ids]
    state["aged"] = [pid for pid in state["aged"] if pid in pending_ids]
    if not pending:
        state["backlogged"] = []
    _save_state(store.kb_dir, state)
    return fired


def send_test(url: str, *, secret: str | None = None) -> bool:
    """Deliver a synthetic event so an operator can verify the endpoint."""
    now = datetime.now(UTC)
    return deliver(
        url,
        {
            "event": "notify.test",
            "timestamp": now.isoformat(timespec="seconds"),
            "proposal_ids": [],
            "pending_count": 0,
        },
        secret=secret,
    )
