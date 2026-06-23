"""JSON shapes exchanged between the desktop shell and ``vouch desktop`` CLI."""

from __future__ import annotations

from typing import Any

from .kb import KbCheckResult, kb_label
from .state import DesktopState, RecentKbEntry


def state_to_json(state: DesktopState) -> dict[str, Any]:
    return state.to_dict()


def recent_entry_to_json(entry: RecentKbEntry) -> dict[str, str]:
    return {
        "path": entry.path,
        "label": entry.label,
        "opened_at": entry.opened_at,
    }


def kb_check_to_json(result: KbCheckResult) -> dict[str, Any]:
    payload = result.to_dict()
    if result.ok and result.project_root:
        payload["label"] = kb_label(result.project_root)
    return payload


def kb_init_to_json(result: dict[str, str | bool]) -> dict[str, Any]:
    return dict(result)


__all__ = [
    "kb_check_to_json",
    "kb_init_to_json",
    "recent_entry_to_json",
    "state_to_json",
]
