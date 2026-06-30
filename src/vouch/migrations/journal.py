"""The rollback journal.

Before a manifest rewrites any artifact, the *prior* content of every file it
will touch is captured to ``.vouch/migrations/rollback-<id>.jsonl`` (header line
+ one line per file). Rollback replays it to restore those exact bytes, which is
what makes ``apply`` → ``rollback`` byte-equivalent regardless of how lossy the
forward transform was. The journal is written and fsynced *before* the first
rewrite, so an interrupted apply always leaves a journal to recover from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..storage import KBStore
from .rewriter import atomic_write_text

MIGRATIONS_SUBDIR = "migrations"  # under .vouch/


@dataclass(frozen=True)
class JournalEntry:
    rel_path: str
    before: str | None  # None => the file did not exist before (rollback deletes it)


def journal_dir(store: KBStore) -> Path:
    return store.kb_dir / MIGRATIONS_SUBDIR


def journal_path(store: KBStore, journal_id: str) -> Path:
    return journal_dir(store) / f"rollback-{journal_id}.jsonl"


def write_journal(
    store: KBStore, journal_id: str, header: dict[str, object], entries: list[JournalEntry]
) -> Path:
    path = journal_path(store, journal_id)
    lines = [json.dumps({"_header": header}, sort_keys=True)]
    for e in entries:
        lines.append(json.dumps({"path": e.rel_path, "before": e.before}, sort_keys=True))
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def list_journals(store: KBStore) -> list[Path]:
    d = journal_dir(store)
    if not d.is_dir():
        return []
    return sorted(d.glob("rollback-*.jsonl"))


def latest_journal(store: KBStore) -> Path | None:
    journals = list_journals(store)
    return journals[-1] if journals else None


def read_journal(path: Path) -> tuple[dict[str, object], list[JournalEntry]]:
    header: dict[str, object] = {}
    entries: list[JournalEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "_header" in rec:
            header = rec["_header"]
        else:
            entries.append(JournalEntry(rel_path=rec["path"], before=rec["before"]))
    return header, entries
