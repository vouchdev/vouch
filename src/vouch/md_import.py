"""Import a markdown folder one file at a time, through the receipt gate.

The argument for building this rather than telling people "convert your
notes and use ``vouch ingest`` per file": registering the source from the
note's own bytes means an extracted claim quotes real offsets and its
receipt verifies -- a property you cannot get by converting first, and it
falls out of ``extract.ingest_source`` for free.

``vouch import-md <folder>`` walks a folder recursively for ``*.md`` files
and runs the same mechanical ingest ``vouch ingest`` runs on one file:
each file is registered as a content-addressed source via
``extract.ingest_source``, receipt-backed claims are filed for its
quotable spans, and they are auto-approved when (and only when)
``review.auto_approve_on_receipt`` is on. An import is a capture
firehose, never a review-gate bypass.

Re-running is idempotent for unchanged files: a per-file content hash in
``.vouch/md_import_state.json`` makes a re-run against the same vault a
no-op for anything that has not changed. An EDITED file is fully
re-ingested -- a documented limitation, not a silent gap:
``extract.ingest_source`` has no way to diff against what a prior version
of the same file already contributed, so a changed note files a fresh
batch of claims for its current content, on top of what is already
there. True claim-level diffing is a harder problem the #612
multi-format track can take on later.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import extract as extract_mod
from .models import ProposalKind, ProposalStatus
from .storage import KBStore, sha256_hex

STATE_FILENAME = "md_import_state.json"

# The default proposer when VOUCH_AGENT isn't set -- mirroring
# "chatgpt-import": an import is a human choosing to file their vault,
# so admission verdicts stay advisory and its claims reach review
# instead of being auto-rejected as capture noise.
MD_IMPORT_ACTOR = "md-import"

# The inbox importer's noise floor: below this many non-whitespace
# characters a file has no quotable span worth a claim.
DEFAULT_MIN_CHARS = 40


def _state_path(store: KBStore) -> Path:
    return store.kb_dir / STATE_FILENAME


def _load_state(store: KBStore) -> dict[str, str]:
    try:
        loaded = json.loads(_state_path(store).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): str(v) for k, v in loaded.items()}


def _save_state(store: KBStore, state: dict[str, str]) -> None:
    _state_path(store).write_text(
        json.dumps(state, indent=1, sort_keys=True), encoding="utf-8"
    )


def _iter_markdown(root: Path) -> list[Path]:
    """Every ``*.md`` file under ``root``, in deterministic review order.

    ``os.walk`` with ``followlinks=False`` never chases symlinked
    directories, and dot-directories (``.git``, ``.vouch``,
    ``.obsidian``) are pruned -- vault tooling writes state there, not
    notes. Per-file safety after the walk stays with ``read_under_root``.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        found.extend(
            Path(dirpath) / name
            for name in sorted(filenames)
            if name.lower().endswith(".md")
        )
    return found


def import_folder(
    store: KBStore,
    directory: Path,
    *,
    actor: str | None = None,
    auto_approve: bool = True,
    max_claims: int | None = None,
    budget_chars: int | None = None,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> dict[str, Any]:
    """Ingest every ``*.md`` file under ``directory``, one ingest per file.

    Unchanged files (same sha256 as the last run) and files shorter than
    ``min_chars`` after whitespace-stripping are skipped. Everything else
    goes through ``extract.ingest_source``: the file is registered as a
    content-addressed source, receipt-backed claims are filed, and those
    claims auto-approve only when ``review.auto_approve_on_receipt`` is
    on. Returns a machine-readable report for the CLI to render.

    The per-row ``approved`` count is that file's ingest call -- the
    receipt drain inside ``ingest_source`` is KB-wide by design, so a
    straggler from an earlier file's ingest would be counted in the row
    it first surfaced in.
    """
    actor = actor or os.environ.get("VOUCH_AGENT") or MD_IMPORT_ACTOR
    root = Path(directory).resolve()
    state = _load_state(store)
    rows: list[dict[str, Any]] = []
    ingested = 0
    skipped = 0
    approved_total = 0

    for path in _iter_markdown(root):
        rel = path.relative_to(root).as_posix()
        resolved, data = store.read_under_root(path)
        digest = sha256_hex(data)
        if state.get(str(resolved)) == digest:
            skipped += 1
            rows.append({"path": rel, "action": "skipped", "reason": "unchanged"})
            continue
        if len(data.decode("utf-8", errors="replace").strip()) < min_chars:
            skipped += 1
            rows.append({"path": rel, "action": "skipped", "reason": "too-short"})
            continue
        source, approved = extract_mod.ingest_source(
            store,
            data,
            proposed_by=actor,
            title=rel,
            auto_approve=auto_approve,
            max_claims=max_claims,
            budget_chars=budget_chars,
        )
        state[str(resolved)] = source.id
        ingested += 1
        approved_total += len(approved)
        rows.append(
            {
                "path": rel,
                "action": "ingested",
                "source": source.id,
                "approved": len(approved),
            }
        )

    _save_state(store, state)
    pending = sum(
        1
        for p in store.list_proposals(ProposalStatus.PENDING)
        if p.kind == ProposalKind.CLAIM
    )
    return {
        "files": len(rows),
        "ingested": ingested,
        "skipped": skipped,
        "approved": approved_total,
        "pending_claims": pending,
        "rows": rows,
    }
