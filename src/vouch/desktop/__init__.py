"""Desktop shell helpers for the vouch review console (#207).

The native Tauri app under ``desktop/app/`` owns menus and window chrome;
this package holds the shared logic the shell can invoke via ``vouch desktop
…`` subprocesses or import in tests: XDG config paths, recent-KB
persistence, folder validation, and review-ui sidecar spawning.
"""

from .kb import KbCheckResult, check_kb_folder, init_kb_at, kb_label
from .paths import config_dir, state_file_path
from .ports import pick_free_port
from .protocol import kb_check_to_json, kb_init_to_json, state_to_json
from .sidecar import SidecarConfig, SidecarHandle, spawn_review_ui, terminate_sidecar
from .state import DesktopState, RecentKbEntry, load_state, save_state, touch_recent_kb

__all__ = [
    "DesktopState",
    "KbCheckResult",
    "RecentKbEntry",
    "SidecarConfig",
    "SidecarHandle",
    "check_kb_folder",
    "config_dir",
    "init_kb_at",
    "kb_check_to_json",
    "kb_init_to_json",
    "kb_label",
    "load_state",
    "pick_free_port",
    "save_state",
    "spawn_review_ui",
    "state_file_path",
    "state_to_json",
    "terminate_sidecar",
    "touch_recent_kb",
]
