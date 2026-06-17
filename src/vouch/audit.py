"""Append-only audit log at .vouch/audit.log.jsonl.

Every mutation goes through `log_event()`. Read with `read_events()` or
the `vouch audit` CLI command. The file is plain JSONL so it diffs and
greps cleanly in git history.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import AuditEvent

if TYPE_CHECKING:
    from .scoping import ViewerContext
    from .storage import KBStore

AUDIT_FILENAME = "audit.log.jsonl"


def _audit_path(kb_dir: Path) -> Path:
    return kb_dir / AUDIT_FILENAME


def new_event_id() -> str:
    return uuid.uuid4().hex


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
    ev = AuditEvent(
        id=new_event_id(),
        event=event,
        actor=actor,
        object_ids=object_ids or [],
        dry_run=dry_run,
        reversible=reversible,
        data=data or {},
    )
    path = _audit_path(kb_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(ev.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    # Open-write-close for crash safety — if the process dies mid-append the
    # log is still parseable up to the last newline.
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return ev


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
