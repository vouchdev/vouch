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
import re
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
PERSONAL_KB_ENV = "VOUCH_PERSONAL_KB"
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


# --- the personal catch-all KB (phase 3) ----------------------------------


def personal_kb_root() -> Path | None:
    """Default home of the personal catch-all KB. Never auto-created.

    ``VOUCH_PERSONAL_KB`` > ``$XDG_DATA_HOME/vouch/personal`` >
    ``~/.local/share/vouch/personal``. Data path, not config path: the
    personal KB is content (claims, sources, an audit log), the registry
    row pointing at it is config. None when no home can be determined
    (containers) — everything personal degrades to off.
    """
    forced = os.environ.get(PERSONAL_KB_ENV)
    if forced:
        return Path(forced).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "vouch" / "personal"
    try:
        home = Path.home()
    except RuntimeError:
        return None
    return home / ".local" / "share" / "vouch" / "personal"


def personal_entry(*, path: Path | None = None) -> RegistryEntry | None:
    """The registry's personal-role row (first match), or None."""
    for e in load_registry(path):
        if e.role == "personal":
            return e
    return None


def personal_fallback_enabled(root: Path) -> bool:
    """The personal KB's own opt-in: config ``personal.fallback_capture``.

    Authority lives in the KB's own config, not the registry — the registry
    only says "a personal KB exists here"; whether KB-less folders may
    capture into it is that KB's own setting. Defensive read: a missing or
    corrupt config means off.
    """
    cfg = root / KB_DIRNAME / "config.yaml"
    try:
        loaded = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(loaded, dict):
        return False
    personal = loaded.get("personal")
    return isinstance(personal, dict) and personal.get("fallback_capture") is True


def set_personal_fallback(root: Path, enabled: bool) -> None:
    """Flip ``personal.fallback_capture`` in the KB's config.

    Textual edits where possible (mirroring ``KBStore._mint_identity``) so
    hand-written comments survive; a config that is not a yaml mapping is
    refused untouched.
    """
    cfg_path = root / KB_DIRNAME / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"{cfg_path} is not valid yaml — fix it by hand") from e
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{cfg_path} must be a yaml mapping")
    value = "true" if enabled else "false"
    personal = loaded.get("personal")
    if "personal" not in loaded:
        # No block yet: append one, preserving the rest of the file
        # byte-for-byte.
        block = (
            "\n# machine-personal catch-all settings (vouch hub init-personal)\n"
            f"personal:\n  fallback_capture: {value}\n"
        )
        cfg_path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
        return
    if isinstance(personal, dict) and "fallback_capture" in personal:
        new_text, n = re.subn(
            r"(?m)^(\s*fallback_capture:\s*).*$",
            rf"\g<1>{value}",
            text,
            count=1,
        )
        if n == 1:
            cfg_path.write_text(new_text, encoding="utf-8")
            return
    elif isinstance(personal, dict):
        new_text, n = re.subn(
            r"(?m)^personal:[ \t]*$",
            f"personal:\n  fallback_capture: {value}",
            text,
            count=1,
        )
        if n == 1:
            cfg_path.write_text(new_text, encoding="utf-8")
            return
    # A non-mapping `personal:` stray, or inline/flow style the regexes
    # can't see — structural rewrite.
    loaded["personal"] = (
        {**personal, "fallback_capture": enabled}
        if isinstance(personal, dict)
        else {"fallback_capture": enabled}
    )
    cfg_path.write_text(
        yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def personal_fallback_store(*, path: Path | None = None) -> KBStore | None:
    """The opted-in personal KB, or None.

    Both belts must agree: the registry names a personal-role KB AND that
    KB's own config carries ``personal.fallback_capture: true``. Anything
    missing or corrupt along the way degrades to None — fallback capture
    fails off, never open.
    """
    entry = personal_entry(path=path)
    if entry is None:
        return None
    root = Path(entry.path)
    if not (root / KB_DIRNAME).is_dir():
        return None
    if not personal_fallback_enabled(root):
        return None
    return KBStore(root)


@dataclass(frozen=True)
class CaptureTarget:
    """Where a session's capture goes, and why."""

    store: KBStore | None
    # True => writing to the personal catch-all, not a project KB.
    fallback: bool = False
    # The KB-less folder the session ran in — stamped onto fallback captures
    # so `vouch adopt` can drain them into that folder's KB later.
    origin: Path | None = None
    # Guard/refusal or routing text for the caller's stderr.
    note: str | None = None


def _capture_origin(start: Path | None) -> Path:
    """The folder a fallback capture is *about* (mirrors resolve()'s start)."""
    origin = start
    if origin is None and os.environ.get("VOUCH_PROJECT_DIR"):
        candidate = Path(os.environ["VOUCH_PROJECT_DIR"])
        if candidate.is_dir():
            origin = candidate
    if origin is None:
        origin = Path.cwd()
    return origin.resolve()


def capture_target(start: Path | None = None) -> CaptureTarget:
    """Write-plane resolution with the personal-KB fallback.

    Project KB first, exactly as before. When no KB is discoverable AND an
    opted-in personal KB is registered, capture routes there — deliberately,
    via the registry plus the KB's own config flag, never via ambient
    discovery — with the origin folder recorded for `vouch adopt`. A
    personal-role guard refusal stays a refusal: the guard fires when
    discovery lands on a personal KB from below (the hijack shape), which is
    not the fallback shape.
    """
    res = resolve(start)
    if res.root is not None and res.guard is None:
        return CaptureTarget(store=KBStore(res.root))
    if res.guard is not None:
        return CaptureTarget(store=None, note=res.guard)
    fb = personal_fallback_store()
    if fb is None:
        return CaptureTarget(store=None)
    origin = _capture_origin(start)
    return CaptureTarget(
        store=fb,
        fallback=True,
        origin=origin,
        note=(
            f"no project KB at {origin} — capturing to the personal KB at "
            f"{fb.root} (adopt later with `vouch init` + `vouch adopt`)"
        ),
    )


def read_target(start: Path | None = None) -> tuple[KBStore | None, str | None, bool]:
    """(store, warning, fallback) for read surfaces.

    The read-plane twin of ``capture_target``, so recall follows capture: a
    session whose knowledge lands in the personal KB must be able to read it
    back from the same folder. The fallback is reported via the warning
    channel — reads reroute loudly, never silently.
    """
    store, warning = resolve_for_read(start)
    if store is not None:
        return store, warning, False
    fb = personal_fallback_store()
    if fb is None:
        return None, warning, False
    return fb, f"no project KB here — reading the personal KB at {fb.root}", True


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
