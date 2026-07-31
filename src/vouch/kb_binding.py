"""Credential-to-KB binding: a token that reaches exactly one KB (#609).

A scheduled agent — a PR triager in CI, an incident summariser — gets its own
KB so a thousand machine-written memories never bury the knowledge a human
curated. Provisioning that KB is only half the isolation. The other half is
the credential: without a binding, a token issued for the agent's KB still
authenticates against the project KB, because `agents.is_active` deliberately
fails open for subjects it has never heard of (an existing deployment whose
token predates the registry must keep working).

So the binding lives here, keyed on the ``auth_subject`` — the sha256 prefix
``trust.py`` already derives — and never on the credential itself. Registering
a binding does not require storing, echoing, or even seeing the secret again,
which is the same split `agents.py` draws and the reason the agent registry
can live in committed YAML.

Two properties follow, and they are the whole point:

* **Unbound subjects are unaffected.** A token with no binding row
  authenticates exactly as before, against whatever KB it is presented to.
  Binding is opt-in, not a migration.
* **A bound subject reaches one KB and fails closed everywhere else.** That
  is what makes a leaked CI secret harmless to the project KB *regardless of
  the agent's working directory* — the failure mode `vouch init` in a
  subdirectory cannot protect against, because upward discovery makes the
  target a function of cwd.

The file is machine-local (``~/.config/vouch/credentials.yaml``, 0600) rather
than committed inside a KB, for a reason worth stating: a binding stored in
the KB it names is circular — KB-B's copy has nothing to say about a token
issued for KB-A, so presenting that token to KB-B would still fall open. The
question "which KB may this subject reach" is machine-scoped, so the answer
has to be too. This is the same trust level as the bearer accept-list it
guards: anyone who can rewrite this file can already rewrite `serve.
bearer_tokens`.

Unlike the hub registry — advisory routing state that degrades to per-project
behaviour when absent — this file is consulted for an authorization decision.
A missing or unreadable one therefore degrades to *today's* behaviour (no
bindings, nothing denied), never to "deny everything": an unreadable file must
not lock every agent out of a running server.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .hub import _registry_lock as _file_lock  # one lock implementation, not two
from .models import utcnow_iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .storage import KBStore

BINDINGS_ENV = "VOUCH_CREDENTIALS_PATH"
BINDINGS_VERSION = 1

# Long enough that guessing is hopeless, url-safe so it survives an env var,
# a CI secret store and a shell without quoting games.
TOKEN_BYTES = 32


class BindingError(RuntimeError):
    """A binding could not be created or removed."""


def bindings_path() -> Path:
    """Where the binding file lives (env > XDG_CONFIG_HOME > ~/.config).

    A sibling of the hub registry: both are machine-local config about KBs
    rather than content belonging to any one of them.
    """
    forced = os.environ.get(BINDINGS_ENV)
    if forced:
        return Path(forced)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "vouch" / "credentials.yaml"


@dataclass(frozen=True)
class Binding:
    """One credential pinned to one KB, identified by its token's subject."""

    subject: str
    kb_id: str
    kb_name: str = ""
    agent: str = ""
    bound_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"subject": self.subject, "kb_id": self.kb_id}
        if self.kb_name:
            out["kb_name"] = self.kb_name
        if self.agent:
            out["agent"] = self.agent
        if self.bound_at:
            out["bound_at"] = self.bound_at
        return out


def _parse_binding(raw: object) -> Binding | None:
    """One row, or None if malformed — a bad row never breaks the rest."""
    if not isinstance(raw, dict):
        return None
    subject = raw.get("subject")
    kb_id = raw.get("kb_id")
    if not isinstance(subject, str) or not subject:
        return None
    if not isinstance(kb_id, str) or not kb_id:
        return None
    return Binding(
        subject=subject,
        kb_id=kb_id,
        kb_name=str(raw.get("kb_name") or ""),
        agent=str(raw.get("agent") or ""),
        bound_at=str(raw.get("bound_at") or ""),
    )


def load_bindings(path: Path | None = None) -> list[Binding]:
    """Read the binding file defensively: missing/corrupt -> no bindings.

    Degrading to "nothing is bound" is the only safe direction here — see the
    module docstring. A corrupt file must not lock out every agent.
    """
    p = path or bindings_path()
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(loaded, dict):
        return []
    rows = loaded.get("bindings")
    if not isinstance(rows, list):
        return []
    out: list[Binding] = []
    seen: set[str] = set()
    for raw in rows:
        entry = _parse_binding(raw)
        # First row wins on a duplicated subject: two answers to "which KB may
        # this reach" is exactly the ambiguity the binding exists to remove.
        if entry is not None and entry.subject not in seen:
            seen.add(entry.subject)
            out.append(entry)
    return out


def save_bindings(bindings: list[Binding], path: Path | None = None) -> Path:
    """Atomically write the binding file, owner-readable only."""
    p = path or bindings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": BINDINGS_VERSION,
        "bindings": [b.to_dict() for b in bindings],
    }
    fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(body, sort_keys=False, allow_unicode=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return p


def binding_for(subject: str, *, path: Path | None = None) -> Binding | None:
    """The binding pinning `subject` to a KB, or None if it is unbound."""
    subject = subject.strip()
    return next((b for b in load_bindings(path) if b.subject == subject), None)


def allows(subject: str, kb_id: str, *, path: Path | None = None) -> bool:
    """Whether `subject` may authenticate against the KB with id `kb_id`.

    Unbound subjects are allowed everywhere (today's behaviour); a bound one
    is allowed against its own KB and refused against every other.
    """
    bound = binding_for(subject, path=path)
    return bound is None or bound.kb_id == kb_id


def store_allows(store: KBStore, subject: str, *, path: Path | None = None) -> bool:
    """`allows`, resolving the KB id from the store being served.

    A KB with no minted identity cannot be the target of any binding, so a
    *bound* subject is refused there rather than falling through to it — the
    binding would otherwise be escapable by pointing an agent at a KB whose
    `kb.id` had been removed.
    """
    identity = store.identity()
    if identity is None:
        return binding_for(subject, path=path) is None
    return allows(subject, identity[0], path=path)


def bind(
    *,
    subject: str,
    kb_id: str,
    kb_name: str = "",
    agent: str = "",
    path: Path | None = None,
) -> Binding:
    """Pin `subject` to `kb_id`. Refuses to move an existing binding.

    Re-binding in place would silently move a live credential between KBs,
    which is the one transition this file exists to make impossible. Retiring
    the old binding is an explicit `unbind`.
    """
    subject = subject.strip()
    if not subject:
        raise BindingError("bind needs the token's auth subject")
    if not kb_id:
        raise BindingError("bind needs the target kb id")
    entry = Binding(
        subject=subject,
        kb_id=kb_id,
        kb_name=kb_name,
        agent=agent,
        bound_at=utcnow_iso(),
    )
    p = path or bindings_path()
    with _file_lock(p):
        existing = load_bindings(p)
        for b in existing:
            if b.subject == subject and b.kb_id != kb_id:
                raise BindingError(
                    f"subject {subject} is already bound to kb {b.kb_id}; "
                    "unbind it first"
                )
        kept = [b for b in existing if b.subject != subject]
        kept.append(entry)
        save_bindings(kept, p)
    return entry


def unbind(subject: str, *, path: Path | None = None) -> Binding | None:
    """Drop `subject`'s binding. Returns the removed row, or None."""
    subject = subject.strip()
    p = path or bindings_path()
    with _file_lock(p):
        existing = load_bindings(p)
        removed = next((b for b in existing if b.subject == subject), None)
        if removed is not None:
            save_bindings([b for b in existing if b.subject != subject], p)
    return removed


def bindings_for_kb(kb_id: str, *, path: Path | None = None) -> list[Binding]:
    """Every credential pinned to one KB."""
    return [b for b in load_bindings(path) if b.kb_id == kb_id]


def issue_token() -> str:
    """A fresh credential. Returned once and never persisted by vouch."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_env_var(agent: str) -> str:
    """The env var name a KB's accept-list references for `agent`'s token.

    `serve.bearer_tokens` supports an ``env:VAR`` indirection precisely so a
    committed config never carries the secret; this picks the variable name so
    the operator does not have to.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", agent).strip("_").upper()
    return f"VOUCH_TOKEN_{slug}" if slug else "VOUCH_TOKEN"
