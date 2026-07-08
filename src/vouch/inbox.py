"""Inbox folder: dropped files become pending proposals, never approved writes.

Dropping a markdown or text file into a folder is the most natural way to
hand knowledge to a project — meeting notes, a design memo, a pasted
transcript. Each new file is registered as a content-addressed source and
rolled into exactly one PENDING page proposal citing it, the same
"background actor proposes, human approves" shape `capture.finalize()`
ships. Mechanical, no model in the loop, and no code path here calls
`proposals.approve()`.

A content-hash-keyed seen-state sidecar (`.vouch/inbox-state.json`) makes
re-runs cheap and idempotent: an unchanged file is skipped, an edited file
re-proposes. The watch loop is a bounded stdlib poll (the `vault_sync`
precedent) — no daemon, no watchdog dependency.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .proposals import propose_page
from .storage import KBStore

STATE_FILENAME = "inbox-state.json"
DEFAULT_MIN_CHARS = 40
DEFAULT_EXTENSIONS = (".md", ".txt")
DEFAULT_POLL_INTERVAL = 2.0
INBOX_ACTOR = "inbox"


@dataclass(frozen=True)
class InboxConfig:
    enabled: bool = True
    min_chars: int = DEFAULT_MIN_CHARS
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS


@dataclass(frozen=True)
class ScanResult:
    proposed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def proposed_anything(self) -> bool:
        return bool(self.proposed)


def load_config(store: KBStore) -> InboxConfig:
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return InboxConfig()
    if not isinstance(loaded, dict):
        return InboxConfig()
    raw = loaded.get("inbox")
    if not isinstance(raw, dict):
        return InboxConfig()
    extensions = raw.get("extensions")
    return InboxConfig(
        enabled=bool(raw.get("enabled", True)),
        min_chars=int(raw.get("min_chars", DEFAULT_MIN_CHARS)),
        extensions=(
            tuple(str(e) for e in extensions)
            if isinstance(extensions, list)
            else DEFAULT_EXTENSIONS
        ),
    )


def _state_path(store: KBStore) -> Path:
    return store.kb_dir / STATE_FILENAME


def _load_state(store: KBStore) -> dict[str, str]:
    path = _state_path(store)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


def _save_state(store: KBStore, state: dict[str, str]) -> None:
    _state_path(store).write_text(
        json.dumps(state, indent=1, sort_keys=True), encoding="utf-8"
    )


def scan(store: KBStore, directory: Path, *, config: InboxConfig | None = None) -> ScanResult:
    """One pass: register + propose every new or changed eligible file."""
    cfg = config or load_config(store)
    if not cfg.enabled:
        return ScanResult()

    state = _load_state(store)
    proposed: list[str] = []
    skipped: list[str] = []

    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        if path.suffix.lower() not in cfg.extensions:
            skipped.append(path.name)
            continue
        # read through the same containment + O_NOFOLLOW hardening the MCP
        # register_source_from_path entrypoint uses
        resolved, data = store.read_under_root(path)
        text = data.decode("utf-8", errors="replace")
        if len(text.strip()) < cfg.min_chars:
            skipped.append(path.name)
            continue

        source = store.put_source(
            data,
            title=path.name,
            locator=str(resolved),
            source_type="file",
            media_type="text/markdown" if path.suffix.lower() == ".md" else "text/plain",
            tags=["inbox"],
        )
        if state.get(str(resolved)) == source.id:
            skipped.append(path.name)
            continue

        proposal = propose_page(
            store,
            title=f"inbox: {path.stem}",
            body=text,
            page_type="log",
            source_ids=[source.id],
            proposed_by=INBOX_ACTOR,
            rationale=f"imported from inbox file {path.name}; distill durable facts into claims",
            tags=["inbox"],
        )
        state[str(resolved)] = source.id
        proposed.append(proposal.id)

    _save_state(store, state)
    return ScanResult(proposed=proposed, skipped=skipped)


def watch(
    store: KBStore,
    directory: Path,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    iterations: int | None = None,
    on_result: object = None,
) -> None:
    """Bounded stdlib poll loop over `scan`. `iterations` bounds it for tests."""
    ticks = 0
    while iterations is None or ticks < iterations:
        result = scan(store, directory)
        if callable(on_result):
            on_result(result)
        ticks += 1
        if iterations is not None and ticks >= iterations:
            break
        time.sleep(poll_interval)
