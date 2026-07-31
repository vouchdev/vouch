"""Agent registry — who can write to this KB, and what did each one do? (#607)

Bearer tokens are matched in ``trust.py`` and hashed into an ``auth_subject``,
but nothing enumerated them, nothing could suspend one, and nothing mapped a
subject back to a readable name. Removing a token from config was the only
revocation available, and it took down every agent sharing that token.

The registry is keyed on ``auth_subject`` — the sha256 prefix ``trust.py``
already derives — so registering an agent never requires storing, echoing, or
even seeing its credential. That is the split ``secrets.py`` already draws and
the reason the registry can live in committed ``.vouch/agents.yaml``: names,
status, scopes and timestamps are reviewable in a PR; the token itself stays
in local config.

Three things follow from that:

* **Revocation is enforced at one chokepoint.** ``trust.authorized_bearer_token``
  wraps the existing token match, so MCP-over-HTTP and JSONL-over-HTTP inherit
  it without two implementations. Tokenless stdio callers are trusted by
  construction and unaffected.
* **Unregistered tokens keep working.** An existing deployment with a plain
  bearer token authenticates exactly as before, as an unnamed active agent,
  until someone registers it. Registration is opt-in, not a migration.
* **Every transition is audited.** The control plane's own history is as
  auditable as the KB's, which is what makes ``vouch agents show`` — a replay
  of everything one agent did — trustworthy rather than decorative.

Revocation is terminal on purpose: a revoked credential is one you have
decided to stop trusting, and an undo button on that is a footgun. Re-admitting
an agent means issuing it a new token, which is a new subject.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml

from . import audit as audit_mod
from . import scopes as scopes_mod

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .storage import KBStore

logger = logging.getLogger(__name__)

REGISTRY_FILENAME = "agents.yaml"

# The actor string transports record for a token-authenticated call — see
# `_agent()` in server.py / jsonl_server.py. Replay keys off this.
ACTOR_PREFIX = "token:"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"


class AgentError(RuntimeError):
    """A registry operation could not be completed."""


@dataclass(frozen=True)
class Agent:
    """One registered credential holder, keyed by its token's subject."""

    subject: str
    name: str
    status: AgentStatus = AgentStatus.ACTIVE
    scopes: tuple[str, ...] = ()
    claimed_at: datetime | None = None
    note: str | None = None

    @property
    def actor(self) -> str:
        """The audit-log actor string this agent's calls are attributed to."""
        return f"{ACTOR_PREFIX}{self.subject}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "subject": self.subject,
            "name": self.name,
            "status": self.status.value,
        }
        if self.scopes:
            out["scopes"] = list(self.scopes)
        if self.claimed_at is not None:
            out["claimed_at"] = self.claimed_at.isoformat(timespec="seconds")
        if self.note:
            out["note"] = self.note
        return out


def _registry_path(store: KBStore):  # type: ignore[no-untyped-def]
    return store.kb_dir / REGISTRY_FILENAME


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_status(value: Any) -> AgentStatus:
    try:
        return AgentStatus(str(value))
    except ValueError:
        # An unreadable status must not silently read as active — that would
        # turn a corrupted registry into an authentication bypass.
        return AgentStatus.REVOKED


def load_registry(store: KBStore) -> list[Agent]:
    """Every registered agent. A malformed row is skipped, never fatal."""
    path = _registry_path(store)
    if not path.exists():
        return []
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    rows = loaded.get("agents") if isinstance(loaded, dict) else loaded
    if not isinstance(rows, list):
        return []
    out: list[Agent] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject", "")).strip()
        name = str(row.get("name", "")).strip()
        if not subject or not name or subject in seen:
            continue
        seen.add(subject)
        scopes = row.get("scopes")
        out.append(Agent(
            subject=subject,
            name=name,
            status=_parse_status(row.get("status", AgentStatus.ACTIVE)),
            scopes=tuple(str(s) for s in scopes) if isinstance(scopes, list) else (),
            claimed_at=_parse_dt(row.get("claimed_at")),
            note=row.get("note") if isinstance(row.get("note"), str) else None,
        ))
    return out


def _write_registry(store: KBStore, agents: list[Agent]) -> None:
    # Always non-empty: `register` appends and `set_status` replaces in place,
    # and there is no de-register — revocation keeps the row so the history of
    # who held the credential survives.
    path = _registry_path(store)
    path.write_text(
        yaml.safe_dump({"agents": [a.to_dict() for a in agents]}, sort_keys=False),
        encoding="utf-8",
    )


def find(store: KBStore, name_or_subject: str) -> Agent | None:
    """Look an agent up by readable name or by subject."""
    needle = name_or_subject.strip()
    if not needle:
        return None
    for agent in load_registry(store):
        if agent.name == needle or agent.subject == needle:
            return agent
    return None


def register(
    store: KBStore,
    *,
    subject: str,
    name: str,
    actor: str,
    scopes: tuple[str, ...] = (),
    note: str | None = None,
) -> Agent:
    """Give a token's subject a readable name and a registry row."""
    subject = subject.strip()
    name = name.strip()
    if not subject:
        raise AgentError("register needs the token's auth subject")
    if not name:
        raise AgentError("register needs a name")
    try:
        scopes = scopes_mod.parse_scopes(scopes)
    except scopes_mod.ScopeError as e:
        raise AgentError(str(e)) from e

    agents = load_registry(store)
    for existing in agents:
        if existing.name == name and existing.subject != subject:
            raise AgentError(
                f"name {name!r} is already registered to subject "
                f"{existing.subject!r}"
            )
        if existing.subject == subject:
            raise AgentError(
                f"subject {subject!r} is already registered as {existing.name!r}"
            )

    agent = Agent(
        subject=subject,
        name=name,
        status=AgentStatus.ACTIVE,
        scopes=scopes,
        claimed_at=datetime.now(UTC),
        note=note,
    )
    _write_registry(store, [*agents, agent])
    audit_mod.log_event(
        store.kb_dir, event="agent.register", actor=actor, object_ids=[subject],
        data={"name": name, "scopes": list(scopes)},
    )
    return agent


_TRANSITIONS: dict[AgentStatus, str] = {
    AgentStatus.PAUSED: "agent.pause",
    AgentStatus.ACTIVE: "agent.resume",
    AgentStatus.REVOKED: "agent.revoke",
}


def set_status(
    store: KBStore, name_or_subject: str, status: AgentStatus, *, actor: str
) -> Agent:
    """Move an agent to ``status``, writing the transition to the audit log."""
    agents = load_registry(store)
    target = next(
        (a for a in agents
         if a.name == name_or_subject.strip() or a.subject == name_or_subject.strip()),
        None,
    )
    if target is None:
        raise AgentError(f"unknown agent {name_or_subject!r}")
    if target.status is AgentStatus.REVOKED:
        # Terminal by design: re-admitting an agent means issuing a new
        # credential, which is a different subject and a different row.
        raise AgentError(
            f"{target.name} is revoked; revocation is terminal — issue a new "
            f"token instead of resuming this one"
        )
    if target.status is status:
        return target

    updated = replace(target, status=status)
    _write_registry(store, [updated if a.subject == target.subject else a
                            for a in agents])
    audit_mod.log_event(
        store.kb_dir, event=_TRANSITIONS[status], actor=actor,
        object_ids=[target.subject], data={"name": target.name},
        reversible=status is not AgentStatus.REVOKED,
    )
    return updated


def is_active(store: KBStore, subject: str) -> bool:
    """Whether a subject may authenticate.

    Unknown subjects are active: an existing deployment whose token predates
    the registry must keep working, so registration is opt-in rather than a
    migration. Only an explicit paused/revoked row denies.
    """
    agent = next(
        (a for a in load_registry(store) if a.subject == subject.strip()), None
    )
    return agent is None or agent.status is AgentStatus.ACTIVE


def scopes_for_subject(store: KBStore, subject: str) -> tuple[str, ...]:
    """The scopes a registered subject holds; empty (= unscoped) if unknown.

    Unknown subjects stay unscoped so a token that predates the registry keeps
    every power it had — the same back-compat rule `is_active` follows.
    """
    agent = next(
        (a for a in load_registry(store) if a.subject == subject.strip()), None
    )
    return agent.scopes if agent is not None else ()


def subject_scopes(subject: str) -> tuple[str, ...]:
    """Store-resolving scope lookup for the transport chokepoint."""
    from .storage import KBStore, discover_root

    try:
        store = KBStore(discover_root())
    except Exception:
        return ()
    try:
        return scopes_for_subject(store, subject)
    except Exception:  # pragma: no cover - defensive
        logger.debug("agents: registry unreadable, treating subject as unscoped")
        return ()


def subject_is_active(subject: str) -> bool:
    """Store-resolving gate for the transport chokepoint.

    Best-effort by design: a request that cannot resolve a KB has no registry
    to be denied by, and an unreadable registry must not lock every agent out
    of a running server.
    """
    from .storage import KBStore, discover_root

    try:
        store = KBStore(discover_root())
    except Exception:
        return True
    try:
        return is_active(store, subject)
    except Exception:  # pragma: no cover - defensive
        logger.debug("agents: registry unreadable, allowing subject")
        return True


def last_seen(store: KBStore, agent: Agent) -> datetime | None:
    """When this agent last did something, from the audit log.

    Derived rather than stored: keeping a ``last_seen_at`` column current
    would mean a disk write on every authenticated request, on the auth path,
    for a field nobody reads in the hot loop. The audit log already knows.
    """
    actors = {agent.actor, agent.name}
    stamps = [
        ev.created_at for ev in audit_mod.read_events(store.kb_dir)
        if ev.actor in actors
    ]
    return max(stamps) if stamps else None


def replay(
    store: KBStore, name_or_subject: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """One agent's audit trail, oldest first.

    This is the half ditto's own docs stop short of: their agent accounts show
    status and last-used, not what the agent actually did.

    Two kinds of event qualify, and the ``by_agent`` flag distinguishes them:
    what the agent *did* (it is the actor), and what was done *to* it (its
    subject is the object of a control-plane transition). Both belong on one
    timeline — "proposed 40 claims, then was paused" is the sentence an
    operator is trying to read, and splitting it across two commands would
    hide the causal bit.
    """
    agent = find(store, name_or_subject)
    if agent is None:
        raise AgentError(f"unknown agent {name_or_subject!r}")
    # Match the token actor and the readable name: calls made before the agent
    # was registered are attributed to whatever actor string the transport
    # recorded then, and dropping them would make the replay lie by omission.
    actors = {agent.actor, agent.name}
    events = [
        {
            "event": ev.event,
            "actor": ev.actor,
            "created_at": ev.created_at.isoformat(timespec="seconds"),
            "object_ids": list(ev.object_ids),
            "by_agent": ev.actor in actors,
        }
        for ev in audit_mod.read_events(store.kb_dir)
        if ev.actor in actors or agent.subject in ev.object_ids
    ]
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events
