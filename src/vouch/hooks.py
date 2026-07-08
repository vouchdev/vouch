"""Host-hook helpers: translate an agent host's prompt hook into KB context.

Claude Code's UserPromptSubmit hook passes a JSON payload on stdin and injects
whatever the hook prints (as an `additionalContext` envelope) before the model
runs. `build_claude_prompt_hook` turns that payload into a compact, relevant
context block drawn from *approved* KB knowledge — so recall costs the agent
zero tool calls. It never raises: on any problem it returns "" (inject
nothing), so a hook failure can never block the user's turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .context import build_context_pack
from .storage import KBStore

_log = logging.getLogger("vouch")

_MAX_ITEMS = 8
_MAX_CHARS = 2000


def _render(pack: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in pack.get("items", []):
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        cites = item.get("citations") or []
        suffix = f"  [{', '.join(cites)}]" if cites else ""
        lines.append(f"- {summary}{suffix}")
    return "\n".join(lines)


def build_claude_prompt_hook(store: KBStore, stdin_text: str) -> str:
    """Return the stdout a host should inject for this prompt, or "" for none."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {"prompt": stdin_text}
    if not isinstance(payload, dict):
        payload = {}
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        return ""
    try:
        pack = build_context_pack(
            store, query=prompt, limit=_MAX_ITEMS, max_chars=_MAX_CHARS,
        )
    except Exception:
        _log.warning("context-hook: build_context_pack failed", exc_info=True)
        return ""
    body = _render(pack) if isinstance(pack, dict) else ""
    if not body:
        return ""
    block = (
        "Relevant knowledge from the project's vouch KB "
        "(approved & cited — consider it before answering):\n" + body
    )
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    })
