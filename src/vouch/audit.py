"""Append-only audit log at .vouch/audit.log.jsonl.

Every mutation goes through `log_event()`. Read with `read_events()` or
the `vouch audit` CLI command. The file is plain JSONL so it diffs and
greps cleanly in git history.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import AuditEvent

AUDIT_FILENAME = "audit.log.jsonl"
GENESIS_HASH = "0" * 64


def _audit_path(kb_dir: Path) -> Path:
    return kb_dir / AUDIT_FILENAME


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
    """Append one AuditEvent. Returns the persisted event."""
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
    path = _audit_path(kb_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical_json(ev.model_dump(mode="json"))
    # Open-write-close for crash safety — if the process dies mid-append the
    # log is still parseable up to the last newline.
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


def read_events(kb_dir: Path) -> Iterator[AuditEvent]:
    """Stream every event in order. Safely skips malformed lines."""
    path = _audit_path(kb_dir)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield AuditEvent.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue


def count_events(kb_dir: Path) -> int:
    path = _audit_path(kb_dir)
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())
