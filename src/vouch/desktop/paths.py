"""XDG-compliant config paths for the vouch desktop shell."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIRNAME = "vouch-desktop"
STATE_FILENAME = "state.json"


def _windows_config_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def _xdg_config_home() -> Path:
    explicit = os.environ.get("XDG_CONFIG_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        return _windows_config_root()
    return Path.home() / ".config"


def config_dir() -> Path:
    """Return ``~/.config/vouch-desktop`` (or platform equivalent)."""
    return _xdg_config_home() / APP_DIRNAME


def state_file_path() -> Path:
    """Absolute path to the persisted desktop state file."""
    return config_dir() / STATE_FILENAME


def ensure_config_dir() -> Path:
    """Create the config directory if missing; return its path."""
    root = config_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root
