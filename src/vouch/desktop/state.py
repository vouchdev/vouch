"""Recent-KB persistence for the desktop shell (#207)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import ensure_config_dir, state_file_path

STATE_VERSION = 1
MAX_RECENT = 5


@dataclass
class RecentKbEntry:
    path: str
    label: str
    opened_at: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RecentKbEntry:
        return cls(
            path=str(raw["path"]),
            label=str(raw.get("label") or Path(raw["path"]).name),
            opened_at=str(raw.get("opened_at") or _now_iso()),
        )


@dataclass
class DesktopState:
    version: int = STATE_VERSION
    last_kb: str | None = None
    recent_kbs: list[RecentKbEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_kb": self.last_kb,
            "recent_kbs": [asdict(e) for e in self.recent_kbs],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DesktopState:
        version = int(raw.get("version", 1))
        recent_raw = raw.get("recent_kbs") or []
        recent = [RecentKbEntry.from_dict(item) for item in recent_raw if isinstance(item, dict)]
        last_kb = raw.get("last_kb")
        return cls(
            version=version,
            last_kb=str(last_kb) if last_kb else None,
            recent_kbs=recent,
        )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _normalize_root(path: str | Path) -> str:
    return str(Path(path).resolve())


def load_state(path: Path | None = None) -> DesktopState:
    """Load desktop state from disk; return defaults when missing."""
    target = path or state_file_path()
    if not target.is_file():
        return DesktopState()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DesktopState()
    if not isinstance(raw, dict):
        return DesktopState()
    return DesktopState.from_dict(raw)


def save_state(state: DesktopState, path: Path | None = None) -> Path:
    """Persist desktop state atomically."""
    target = path or state_file_path()
    ensure_config_dir()
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def touch_recent_kb(
    project_root: str | Path,
    *,
    label: str | None = None,
    state: DesktopState | None = None,
    path: Path | None = None,
) -> DesktopState:
    """Record a KB open: move to front of recents, cap at ``MAX_RECENT``."""
    root = _normalize_root(project_root)
    entry_label = label or Path(root).name or root
    base = state or load_state(path)

    filtered = [e for e in base.recent_kbs if _normalize_root(e.path) != root]
    filtered.insert(
        0,
        RecentKbEntry(path=root, label=entry_label, opened_at=_now_iso()),
    )
    base.recent_kbs = filtered[:MAX_RECENT]
    base.last_kb = root
    base.version = STATE_VERSION
    save_state(base, path)
    return base
