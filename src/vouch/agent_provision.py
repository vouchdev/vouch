"""Agent-native provisioning — ``vouch init --agent`` + ``vouch agents claim``.

An agent that wants durable memory today has to be handed a KB by a human
first. This module inverts that (issue #606): the agent runs one command,
provisions its own identity and an agent-scoped KB, keeps a local credential
that is never printed, and emits a claim token the human runs to bind the
agent to a project KB. Claiming reuses the adopt gate path so nothing lands
past review; the agent's credential and its own KB artifacts stay untouched.

Secrets live outside any ``.vouch/`` (same split as hub tokens):
``$XDG_CONFIG_HOME/vouch/agent-credentials.yaml`` (chmod 0600). Agent KB
content lives under ``$XDG_DATA_HOME/vouch/agents/<caller>/``.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import adopt as adopt_mod
from . import audit as audit_mod
from .models import utcnow_iso
from .storage import KBStore

CALLER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CREDS_ENV = "VOUCH_AGENT_CREDS_PATH"
AGENTS_DATA_ENV = "VOUCH_AGENTS_DATA"
CLAIM_ACTOR = "vouch-agent-claim"


class AgentProvisionError(RuntimeError):
    """A provision or claim operation could not be completed."""


@dataclass(frozen=True)
class AgentRecord:
    """One provisioned agent, secrets included — never log or echo ``credential``."""

    caller: str
    kb_root: str
    credential: str
    claim_token: str
    created_at: str
    claimed_at: str | None = None
    claimed_project_kb_id: str | None = None
    claimed_project_root: str | None = None

    @property
    def kb_dir(self) -> Path:
        return Path(self.kb_root) / ".vouch"

    def public_dict(self) -> dict[str, Any]:
        """JSON-safe view with the credential omitted."""
        return {
            "caller": self.caller,
            "kb_root": self.kb_root,
            "kb_dir": str(self.kb_dir),
            "claim_token": self.claim_token,
            "claim_command": f"vouch agents claim {self.claim_token}",
            "credential_path": str(credentials_path()),
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "claimed_project_kb_id": self.claimed_project_kb_id,
            "claimed_project_root": self.claimed_project_root,
            "unclaimed": self.claimed_at is None,
        }


@dataclass(frozen=True)
class ProvisionResult:
    record: AgentRecord
    created_kb: bool
    credential_path: Path

    def public_dict(self) -> dict[str, Any]:
        out = self.record.public_dict()
        out["created_kb"] = self.created_kb
        out["credential_path"] = str(self.credential_path)
        return out


@dataclass(frozen=True)
class ClaimResult:
    record: AgentRecord
    adopt: adopt_mod.AdoptReport
    already_claimed: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "caller": self.record.caller,
            "kb_root": self.record.kb_root,
            "already_claimed": self.already_claimed,
            "claimed_at": self.record.claimed_at,
            "claimed_project_kb_id": self.record.claimed_project_kb_id,
            "claimed_project_root": self.record.claimed_project_root,
            "adopt": self.adopt.as_dict(),
        }


def credentials_path() -> Path:
    forced = os.environ.get(CREDS_ENV)
    if forced:
        return Path(forced).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "vouch" / "agent-credentials.yaml"


def agents_data_root() -> Path:
    forced = os.environ.get(AGENTS_DATA_ENV)
    if forced:
        return Path(forced).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "vouch" / "agents"
    return Path.home() / ".local" / "share" / "vouch" / "agents"


def validate_caller(caller: str) -> str:
    name = caller.strip()
    if not CALLER_RE.fullmatch(name):
        raise AgentProvisionError(
            "agent-caller must be 1-64 chars: letters, digits, "
            "`.`, `_`, `-`, starting with alphanumeric"
        )
    return name


def _load_raw() -> dict[str, Any]:
    path = credentials_path()
    if not path.exists():
        return {"version": 1, "agents": {}}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise AgentProvisionError(f"cannot read {path}: {e}") from e
    if not isinstance(loaded, dict):
        return {"version": 1, "agents": {}}
    agents = loaded.get("agents")
    if not isinstance(agents, dict):
        loaded["agents"] = {}
    loaded.setdefault("version", 1)
    return loaded


def _save_raw(data: dict[str, Any]) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        path.chmod(0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def _parse_record(caller: str, raw: object) -> AgentRecord | None:
    if not isinstance(raw, dict):
        return None
    kb_root = raw.get("kb_root")
    credential = raw.get("credential")
    claim_token = raw.get("claim_token")
    if not (
        isinstance(kb_root, str)
        and kb_root
        and isinstance(credential, str)
        and credential
        and isinstance(claim_token, str)
        and claim_token
    ):
        return None
    return AgentRecord(
        caller=caller,
        kb_root=kb_root,
        credential=credential,
        claim_token=claim_token,
        created_at=str(raw.get("created_at") or ""),
        claimed_at=raw.get("claimed_at") if isinstance(raw.get("claimed_at"), str) else None,
        claimed_project_kb_id=(
            raw.get("claimed_project_kb_id")
            if isinstance(raw.get("claimed_project_kb_id"), str)
            else None
        ),
        claimed_project_root=(
            raw.get("claimed_project_root")
            if isinstance(raw.get("claimed_project_root"), str)
            else None
        ),
    )


def _record_to_raw(record: AgentRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "caller": record.caller,
        "kb_root": record.kb_root,
        "credential": record.credential,
        "claim_token": record.claim_token,
        "created_at": record.created_at,
    }
    if record.claimed_at is not None:
        out["claimed_at"] = record.claimed_at
    if record.claimed_project_kb_id is not None:
        out["claimed_project_kb_id"] = record.claimed_project_kb_id
    if record.claimed_project_root is not None:
        out["claimed_project_root"] = record.claimed_project_root
    return out


def list_records() -> list[AgentRecord]:
    data = _load_raw()
    agents = data.get("agents") or {}
    out: list[AgentRecord] = []
    if not isinstance(agents, dict):
        return out
    for caller, raw in agents.items():
        if not isinstance(caller, str):
            continue
        rec = _parse_record(caller, raw)
        if rec is not None:
            out.append(rec)
    return out


def find_by_caller(caller: str) -> AgentRecord | None:
    name = validate_caller(caller)
    for rec in list_records():
        if rec.caller == name:
            return rec
    return None


def find_by_claim_token(token: str) -> AgentRecord | None:
    needle = token.strip()
    if not needle:
        raise AgentProvisionError("claim token is empty")
    for rec in list_records():
        if secrets.compare_digest(rec.claim_token, needle):
            return rec
    return None


def agent_kb_root(caller: str, *, path: Path | None = None) -> Path:
    if path is not None:
        return path.resolve()
    return (agents_data_root() / validate_caller(caller)).resolve()


def stamp_agent_config(store: KBStore, *, caller: str, unclaimed: bool) -> None:
    """Persist the proposer identity in the agent KB's config.yaml."""
    text = store.config_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        loaded = {}
    agent = loaded.get("agent")
    block = dict(agent) if isinstance(agent, dict) else {}
    block["caller"] = caller
    block["unclaimed"] = unclaimed
    if "provisioned_at" not in block:
        block["provisioned_at"] = utcnow_iso()
    loaded["agent"] = block
    store.config_path.write_text(
        yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def caller_from_store(store: KBStore) -> str | None:
    """Read ``agent.caller`` from a KB's config, if stamped."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    agent = loaded.get("agent")
    if not isinstance(agent, dict):
        return None
    caller = agent.get("caller")
    return caller if isinstance(caller, str) and caller.strip() else None


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def provision(
    caller: str,
    *,
    bootstrap,
    path: Path | None = None,
    actor: str,
) -> ProvisionResult:
    """Create (or re-bind) an agent-scoped KB and write local credentials.

    ``bootstrap`` is ``cli._bootstrap_kb`` — injected so this module stays
    free of click and onboarding import cycles. Returns a result whose
    ``public_dict`` never includes the credential value.
    """
    name = validate_caller(caller)
    existing = find_by_caller(name)
    if existing is not None and existing.claimed_at is None:
        # Idempotent re-run before claim: keep the same credential + token so
        # a skill that retries init does not invalidate a token already shown.
        kb_root = Path(existing.kb_root)
        if not (kb_root / ".vouch").is_dir():
            raise AgentProvisionError(
                f"agent {name!r} is registered at {kb_root} but the KB is "
                "missing — remove its credentials row or restore the directory"
            )
        return ProvisionResult(
            record=existing,
            created_kb=False,
            credential_path=credentials_path(),
        )
    if existing is not None and existing.claimed_at is not None:
        raise AgentProvisionError(
            f"agent {name!r} was already claimed "
            f"(project {existing.claimed_project_root}); pick a new --agent-caller"
        )

    root = agent_kb_root(name, path=path)
    root.mkdir(parents=True, exist_ok=True)
    created = not (root / ".vouch" / "config.yaml").exists()
    store, _seed, _template = bootstrap(root)
    stamp_agent_config(store, caller=name, unclaimed=True)
    identity = store.identity()
    audit_mod.log_event(
        store.kb_dir,
        event="agent.provision",
        actor=actor,
        data={
            "caller": name,
            "kb_id": identity[0] if identity else None,
            "created_kb": created,
        },
    )

    record = AgentRecord(
        caller=name,
        kb_root=str(root),
        credential=_new_secret(),
        claim_token=_new_secret(),
        created_at=utcnow_iso(),
    )
    data = _load_raw()
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    agents[name] = _record_to_raw(record)
    cred_path = _save_raw(data)
    return ProvisionResult(record=record, created_kb=created, credential_path=cred_path)


def claim(
    token: str,
    project: KBStore,
    *,
    actor: str,
    dry_run: bool = False,
    retire: bool = False,
) -> ClaimResult:
    """Bind an unclaimed agent to ``project`` and adopt its knowledge through the gate."""
    record = find_by_claim_token(token)
    if record is None:
        raise AgentProvisionError("unknown claim token")

    identity = project.identity()
    if identity is None:
        identity = project.ensure_identity()
    project_id = identity[0]

    if record.claimed_at is not None:
        if record.claimed_project_kb_id == project_id:
            # Idempotent: already bound here — still run adopt so late knowledge moves.
            agent_store = KBStore(Path(record.kb_root))
            report = adopt_mod.adopt_kb(
                project,
                agent_store,
                actor=CLAIM_ACTOR,
                retire=retire,
                dry_run=dry_run,
                origin_label=f"agent:{record.caller}",
            )
            return ClaimResult(record=record, adopt=report, already_claimed=True)
        raise AgentProvisionError(
            f"agent {record.caller!r} is already claimed by "
            f"{record.claimed_project_root} ({record.claimed_project_kb_id})"
        )

    if project.kb_dir.resolve() == record.kb_dir.resolve():
        raise AgentProvisionError("claim must run inside a project KB, not the agent's own KB")

    agent_store = KBStore(Path(record.kb_root))
    if not agent_store.config_path.exists():
        raise AgentProvisionError(f"agent KB at {record.kb_root} is missing — cannot claim")

    report = adopt_mod.adopt_kb(
        project,
        agent_store,
        actor=CLAIM_ACTOR,
        retire=retire,
        dry_run=dry_run,
        origin_label=f"agent:{record.caller}",
    )
    if dry_run:
        return ClaimResult(record=record, adopt=report, already_claimed=False)

    claimed = AgentRecord(
        caller=record.caller,
        kb_root=record.kb_root,
        credential=record.credential,
        claim_token=record.claim_token,
        created_at=record.created_at,
        claimed_at=utcnow_iso(),
        claimed_project_kb_id=project_id,
        claimed_project_root=str(project.root),
    )
    data = _load_raw()
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    agents[claimed.caller] = _record_to_raw(claimed)
    _save_raw(data)
    stamp_agent_config(agent_store, caller=claimed.caller, unclaimed=False)

    audit_mod.log_event(
        project.kb_dir,
        event="agent.claim",
        actor=actor,
        data={
            "caller": claimed.caller,
            "agent_kb_id": (agent_store.identity() or (None, None))[0],
            "sources": len(report.sources),
            "claims_durable": len(report.claims_durable),
            "claims_pending": len(report.claims_pending),
        },
    )
    audit_mod.log_event(
        agent_store.kb_dir,
        event="agent.claim",
        actor=actor,
        data={
            "caller": claimed.caller,
            "project_kb_id": project_id,
            "project_root": str(project.root),
        },
    )
    return ClaimResult(record=claimed, adopt=report, already_claimed=False)
