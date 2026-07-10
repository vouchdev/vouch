"""Locate and parse raw agent session transcripts on demand.

Given a captured session id, find the raw JSONL the agent wrote (Claude Code
under ``~/.claude/projects``, Codex rollouts under ``$CODEX_HOME/sessions``)
and normalize it into a block schema the vouch console renders. Read-only:
never writes to the KB. When the raw file is gone we degrade to vouch's
compact capture observations instead.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .storage import KBStore

# Session ids are UUID-shaped; reject anything else so a hostile id can't
# widen a glob or traverse out of the projects tree.
_VALID_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _claude_projects_root() -> Path:
    env = os.environ.get("VOUCH_CLAUDE_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def find_claude_file(session_id: str) -> Path | None:
    """The raw Claude Code JSONL for ``session_id``, or None.

    Claude names each session file ``<id>.jsonl`` under a per-cwd project
    dir; subagent transcripts live under ``<parent>/subagents/**``. The file
    stem is the id, so a literal name match (no id interpolation into a glob)
    locates it.
    """
    if not _VALID_ID.match(session_id):
        return None
    root = _claude_projects_root()
    if not root.is_dir():
        return None
    name = f"{session_id}.jsonl"
    for project in root.iterdir():
        if not project.is_dir():
            continue
        top = project / name
        if top.is_file():
            return top
    for candidate in root.glob(f"*/*/subagents/**/{name}"):
        if candidate.is_file():
            return candidate
    return None
