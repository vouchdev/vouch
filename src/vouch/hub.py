"""Machine-level hub: the registry of KBs this machine knows about.

`~/.config/vouch/registry.yaml` (override with VOUCH_REGISTRY_PATH; honours
XDG_CONFIG_HOME) lists known KBs — one row per KB instance: id, display
name, role (project | personal | team), path. The registry is advisory
routing state, never authority: identity and content live in each KB's own
`.vouch/`, so a stale or deleted registry degrades to today's per-project
behaviour instead of breaking anything. It is machine-local and never
committed.

This file is the local seed of the vouchhub registry of connected KBs
(ROADMAP 2.0): resolution/routing decisions that will one day live in a hub
daemon start here as plain functions over a YAML file.

`resolve()` wraps `storage.discover_root` with the registry-aware safety
check: a KB registered with role `personal` is never an ambient capture
target for a directory below it — capture refuses, reads warn. This is the
second belt on top of the structural $HOME walk-stop in `discover_root`
(which needs no registry state at all).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import audit as audit_mod
from .models import utcnow_iso
from .storage import KB_DIRNAME, KBNotFoundError, KBStore, discover_root

REGISTRY_ENV = "VOUCH_REGISTRY_PATH"
REGISTRY_VERSION = 1
ROLES = ("project", "personal", "team")


def registry_path() -> Path:
    """Where the machine registry lives (env > XDG_CONFIG_HOME > ~/.config)."""
    forced = os.environ.get(REGISTRY_ENV)
    if forced:
        return Path(forced)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "vouch" / "registry.yaml"


@dataclass(frozen=True)
class RegistryEntry:
    kb_id: str
    name: str
    role: str
    path: str
    added_at: str


def _parse_entry(raw: object) -> RegistryEntry | None:
    """One registry row, or None if malformed — a bad row never breaks the rest."""
    if not isinstance(raw, dict):
        return None
    kb_id = raw.get("kb_id")
    path = raw.get("path")
    if not isinstance(kb_id, str) or not kb_id or not isinstance(path, str) or not path:
        return None
    role = raw.get("role")
    if role not in ROLES:
        role = "project"
    name = raw.get("name")
    return RegistryEntry(
        kb_id=kb_id,
        name=name if isinstance(name, str) and name else Path(path).name or "kb",
        role=str(role),
        path=path,
        added_at=str(raw.get("added_at") or ""),
    )


def load_registry(path: Path | None = None) -> list[RegistryEntry]:
    """Read the registry defensively: missing/corrupt file -> empty list."""
    p = path or registry_path()
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(loaded, dict):
        return []
    rows = loaded.get("kbs")
    if not isinstance(rows, list):
        return []
    out: list[RegistryEntry] = []
    for raw in rows:
        entry = _parse_entry(raw)
        if entry is not None:
            out.append(entry)
    return out


@contextlib.contextmanager
def _registry_lock(p: Path) -> Iterator[None]:
    """Exclusive cross-process lock for registry read-modify-write cycles.

    Same shape as audit._audit_lock: a sidecar `.lock` file so the registry
    itself is never opened in a truncating mode. Best-effort no-op on
    non-POSIX platforms — the unique-tempfile rename in save_registry still
    prevents torn files there.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    lockfile = p.with_name(p.name + ".lock")
    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "posix":
            fcntl = __import__("fcntl")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            yield
    finally:
        os.close(fd)


def save_registry(entries: list[RegistryEntry], path: Path | None = None) -> Path:
    """Atomically write the registry (unique tmp file + rename)."""
    p = path or registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "version": REGISTRY_VERSION,
        "kbs": [
            {
                "kb_id": e.kb_id,
                "name": e.name,
                "role": e.role,
                "path": e.path,
                "added_at": e.added_at,
            }
            for e in entries
        ],
    }
    # A per-writer tempfile (not a shared fixed name) so two concurrent
    # writers can never truncate or rename-steal each other's staging file.
    fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(body, sort_keys=False, allow_unicode=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return p


def ensure_kb_identity(store: KBStore, *, actor: str) -> tuple[str, str]:
    """Return (kb_id, name), minting + audit-logging a backfill if absent.

    Minting for an existing KB is an additive `kb.identity` audit event —
    never a history rewrite. New KBs get their id inside `KBStore.init`,
    before the `kb.init` event, so init history is stamped from birth.
    """
    existing = store.identity()
    if existing is not None:
        return existing
    kb_id, name = store.ensure_identity()
    audit_mod.log_event(
        store.kb_dir,
        event="kb.identity",
        actor=actor,
        data={"kb_id": kb_id, "name": name},
    )
    return kb_id, name


def register_kb(
    root: Path,
    *,
    role: str = "project",
    name: str | None = None,
    actor: str,
    path: Path | None = None,
) -> RegistryEntry:
    """Add (or refresh) one KB in the machine registry. Idempotent on kb_id."""
    root = root.resolve()
    if not (root / KB_DIRNAME).is_dir():
        raise KBNotFoundError(f"no {KB_DIRNAME}/ at {root} — run `vouch init` there first")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    store = KBStore(root)
    kb_id, kb_name = ensure_kb_identity(store, actor=actor)
    entry = RegistryEntry(
        kb_id=kb_id,
        name=name or kb_name,
        role=role,
        path=str(root),
        added_at=utcnow_iso(),
    )
    with _registry_lock(path or registry_path()):
        existing = load_registry(path)
        entries = [e for e in existing if e.kb_id != kb_id]
        # A moved/re-registered KB keeps one row: the kb_id is the key, the
        # path is metadata. Preserve the original added_at on refresh.
        previous = next((e for e in existing if e.kb_id == kb_id), None)
        if previous is not None and previous.added_at:
            entry = RegistryEntry(
                kb_id=entry.kb_id,
                name=entry.name,
                role=entry.role,
                path=entry.path,
                added_at=previous.added_at,
            )
        entries.append(entry)
        save_registry(entries, path)
    return entry


def unregister_kb(token: str, *, path: Path | None = None) -> RegistryEntry | None:
    """Remove a KB by kb_id or path. Returns the removed row, or None."""
    token_path = (
        str(Path(token).expanduser().resolve())
        if token.startswith((".", "/", "~"))
        else None
    )
    with _registry_lock(path or registry_path()):
        entries = load_registry(path)
        removed = None
        kept: list[RegistryEntry] = []
        for e in entries:
            if removed is None and (
                e.kb_id == token or e.path == token or e.path == token_path
            ):
                removed = e
                continue
            kept.append(e)
        if removed is not None:
            save_registry(kept, path)
    return removed


def entry_for_root(root: Path, *, path: Path | None = None) -> RegistryEntry | None:
    """The registry row whose path is `root`, preferring kb_id match when known."""
    root = root.resolve()
    entries = load_registry(path)
    identity = KBStore(root).identity()
    if identity is not None:
        for e in entries:
            if e.kb_id == identity[0]:
                return e
    root_str = str(root)
    for e in entries:
        if e.path == root_str:
            return e
    return None


@dataclass(frozen=True)
class Resolution:
    """Outcome of KB resolution: where, why, and whether capture may write."""

    root: Path | None
    why: list[str]
    # Non-None => ambient capture must refuse (reads may proceed with a
    # warning). Set when resolution lands on a registered personal-role KB
    # from a directory that is not the KB root itself.
    guard: str | None = None


def resolve(start: Path | None = None) -> Resolution:
    """Registry-aware KB resolution with a human-readable why-chain.

    Never raises: a missing KB is `Resolution(root=None, why=[...])` so hook
    callers can no-op quietly and status callers can print the chain.
    """
    trace: list[str] = []
    try:
        root = discover_root(start, trace=trace)
    except KBNotFoundError as e:
        trace.append(str(e))
        return Resolution(root=None, why=trace)

    if os.environ.get("VOUCH_KB_PATH"):
        # An explicit override is always deliberate — never guard it.
        return Resolution(root=root, why=trace)

    entry = entry_for_root(root)
    if entry is not None and entry.role == "personal":
        origin = (start or Path.cwd()).resolve()
        if os.environ.get("VOUCH_PROJECT_DIR") and start is None:
            candidate = Path(os.environ["VOUCH_PROJECT_DIR"])
            if candidate.is_dir():
                origin = candidate.resolve()
        if origin != root.resolve():
            guard = (
                f"KB at {root} is registered as a personal KB; refusing ambient "
                f"capture from {origin}. Run `vouch init` in the project root, or "
                f"set VOUCH_KB_PATH={root / KB_DIRNAME} to target it deliberately."
            )
            trace.append(guard)
            return Resolution(root=root, why=trace, guard=guard)
    return Resolution(root=root, why=trace)


def resolve_for_capture(start: Path | None = None) -> KBStore | None:
    """The write-plane resolver: a guarded or missing KB yields None."""
    res = resolve(start)
    if res.root is None or res.guard is not None:
        return None
    return KBStore(res.root)


def resolve_for_read(start: Path | None = None) -> tuple[KBStore | None, str | None]:
    """The read-plane resolver: (store, warning).

    Reads are never blacked out by the personal-role guard — recall going
    silent is indistinguishable from vouch being broken. The guard text
    comes back as a warning for the caller to surface on stderr.
    """
    res = resolve(start)
    if res.root is None:
        return None, None
    return KBStore(res.root), res.guard
