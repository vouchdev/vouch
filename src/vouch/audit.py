"""Append-only audit log at .vouch/audit.log.jsonl.

Every mutation goes through `log_event()`. Read with `read_events()` or
the `vouch audit` CLI command. The file is plain JSONL so it diffs and
greps cleanly in git history.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import AuditEvent

if TYPE_CHECKING:
    from .scoping import ViewerContext
    from .storage import KBStore

AUDIT_FILENAME = "audit.log.jsonl"
AUDIT_LOCKFILE = AUDIT_FILENAME + ".lock"
GENESIS_HASH = "0" * 64


def _audit_path(kb_dir: Path) -> Path:
    return kb_dir / AUDIT_FILENAME


def _audit_lockfile(kb_dir: Path) -> Path:
    return kb_dir / AUDIT_LOCKFILE


@contextlib.contextmanager
def _audit_lock(kb_dir: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock for the duration of a log_event.

    Serialises the read-then-append sequence in `log_event` so two concurrent
    writers cannot both observe the same `prev_hash` and fork the chain.
    Lock is held on a sibling `audit.log.jsonl.lock` file so the audit log
    itself is never opened in a mode that could truncate it. Blocks until
    acquired and is always released, including on exceptions.
    """
    lockfile = _audit_lockfile(kb_dir)
    lockfile.parent.mkdir(parents=True, exist_ok=True)
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
            msvcrt = __import__("msvcrt")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def new_event_id() -> str:
    return uuid.uuid4().hex


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _event_payload_for_hash(ev: AuditEvent) -> dict[str, Any]:
    return ev.model_dump(mode="json", exclude={"hash"})


def _compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical_json(payload)).encode()).hexdigest()


def _last_hash(kb_dir: Path) -> str:
    path = _audit_path(kb_dir)
    if not path.exists():
        return GENESIS_HASH
    last_hash = GENESIS_HASH
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw.get("hash"), str):
                last_hash = raw["hash"]
    return last_hash


def log_event(
    kb_dir: Path,
    *,
    event: str,
    actor: str,
    object_ids: list[str] | None = None,
    dry_run: bool = False,
    reversible: bool = True,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one AuditEvent. Returns the persisted event.

    Holds an exclusive cross-process lock around read-prev-hash → derive →
    append so concurrent writers can't fork the chain. Without the lock, two
    log_event calls racing on the same KB observe the same prev_hash and both
    chain off it — verify_chain then reports "previous hash mismatch" forever.
    """
    path = _audit_path(kb_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _audit_lock(kb_dir):
        prev_hash = _last_hash(kb_dir)
        ev = AuditEvent(
            id=new_event_id(),
            event=event,
            actor=actor,
            object_ids=object_ids or [],
            dry_run=dry_run,
            reversible=reversible,
            data=data or {},
            prev_hash=prev_hash,
        )
        ev.hash = _compute_hash(prev_hash, _event_payload_for_hash(ev))
        line = _canonical_json(ev.model_dump(mode="json"))
        # Open-write-close for crash safety — if the process dies mid-append
        # the log is still parseable up to the last newline.
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return ev


@dataclass(frozen=True)
class AuditChainStatus:
    ok: bool
    line: int | None = None
    reason: str | None = None


def verify_chain(kb_dir: Path) -> AuditChainStatus:
    """Verify the tamper-evident audit hash chain."""
    path = _audit_path(kb_dir)
    if not path.exists():
        return AuditChainStatus(True)
    prev_hash = GENESIS_HASH
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                return AuditChainStatus(False, line_no, "malformed JSON")
            if raw.get("prev_hash") is None or raw.get("hash") is None:
                return AuditChainStatus(False, line_no, "legacy event is not hash-chained")
            if raw["prev_hash"] != prev_hash:
                return AuditChainStatus(False, line_no, "previous hash mismatch")
            expected = _compute_hash(prev_hash, {k: v for k, v in raw.items() if k != "hash"})
            if raw["hash"] != expected:
                return AuditChainStatus(False, line_no, "event hash mismatch")
            prev_hash = raw["hash"]
    return AuditChainStatus(True)


def read_events(
    kb_dir: Path,
    *,
    store: KBStore | None = None,
    viewer: ViewerContext | None = None,
) -> Iterator[AuditEvent]:
    """Stream events in order. Safely skips malformed lines.

    When *viewer* is set, *store* must also be provided so scoped
    ``object_ids`` can be resolved. Events referencing artifacts outside
    the viewer context are omitted. Events with empty ``object_ids`` are
    always included.
    """
    if viewer is not None and store is None:
        raise ValueError("read_events with viewer requires store for scope resolution")
    path = _audit_path(kb_dir)
    if not path.exists():
        return
    scoped = viewer is not None and store is not None
    if scoped:
        from .scoping import event_visible_to_viewer
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = AuditEvent.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
            if scoped and not event_visible_to_viewer(store, event, viewer):  # type: ignore[arg-type]
                continue
            yield event


def count_events(kb_dir: Path) -> int:
    path = _audit_path(kb_dir)
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())
