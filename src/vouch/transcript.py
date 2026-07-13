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

from .capture import _read_observations, buffer_path

if TYPE_CHECKING:
    from .storage import KBStore

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_MESSAGES = 2000

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


def _norm_tokens(usage: dict[str, Any]) -> dict[str, int]:
    def i(key: str) -> int:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    return {
        "input": i("input_tokens"),
        "output": i("output_tokens"),
        "cache_read": i("cache_read_input_tokens"),
        "cache_creation": i("cache_creation_input_tokens"),
    }


def _result_text(content: Any) -> str:
    """tool_result.content is a string, or a list of {type:text,text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def parse_claude_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]:
    """Parse a Claude Code JSONL into the normalized transcript schema.

    Single forward pass: assistant content blocks that share a
    ``message.id`` merge into one logical message; a later ``tool_result``
    (in a user entry) is paired into the matching ``tool_use`` block by id and
    its user entry is not emitted as a standalone message.
    """
    messages: list[dict[str, Any]] = []
    tool_by_id: dict[str, dict[str, Any]] = {}
    session: dict[str, Any] = {
        "id": path.stem, "agent": "claude", "cwd": None, "git_branch": None,
        "title": None, "started_at": None, "ended_at": None, "model": None,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    truncated = False
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current["blocks"]:
            messages.append(current)
        current = None

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if session["cwd"] is None and isinstance(obj.get("cwd"), str):
                session["cwd"] = obj["cwd"]
            if session["git_branch"] is None and isinstance(obj.get("gitBranch"), str):
                session["git_branch"] = obj["gitBranch"]
            ts = obj.get("timestamp")
            if isinstance(ts, str):
                if session["started_at"] is None:
                    session["started_at"] = ts
                session["ended_at"] = ts
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                session["title"] = obj["aiTitle"]
                continue
            if t not in ("user", "assistant"):
                continue
            if len(messages) >= max_messages:
                truncated = True
                break
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")

            if t == "assistant":
                mid = msg.get("id") if isinstance(msg.get("id"), str) else None
                if current is None or current.get("id") != mid:
                    flush()
                    raw_usage = msg.get("usage")
                    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
                    model = msg.get("model") if isinstance(msg.get("model"), str) else None
                    if model and session["model"] is None:
                        session["model"] = model
                    current = {
                        "role": "assistant", "id": mid, "model": model,
                        "timestamp": ts if isinstance(ts, str) else None,
                        "tokens": _norm_tokens(usage), "blocks": [],
                    }
                    tok = current["tokens"]
                    for k in session["tokens"]:
                        session["tokens"][k] += tok[k]
                parts = content if isinstance(content, list) else []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "thinking":
                        text = str(part.get("thinking", "")).strip()
                        if text:
                            current["blocks"].append({"type": "thinking", "text": text})
                    elif ptype == "text":
                        text = str(part.get("text", "")).strip()
                        if text:
                            current["blocks"].append({"type": "text", "text": text})
                    elif ptype == "tool_use":
                        tid = part.get("id")
                        raw_input = part.get("input")
                        block: dict[str, Any] = {
                            "type": "tool_use", "id": tid,
                            "name": str(part.get("name", "")),
                            "input": raw_input if isinstance(raw_input, dict) else {},
                            "result": None,
                        }
                        current["blocks"].append(block)
                        if isinstance(tid, str):
                            tool_by_id[tid] = block
                continue

            # user entry
            flush()
            if isinstance(content, str):
                text = content.strip()
                if text:
                    messages.append({
                        "role": "user", "id": None, "model": None,
                        "timestamp": ts if isinstance(ts, str) else None,
                        "tokens": None, "blocks": [{"type": "text", "text": text}],
                    })
                continue
            parts = content if isinstance(content, list) else []
            user_blocks: list[dict[str, Any]] = []
            agent_id = None
            tur = obj.get("toolUseResult")
            if isinstance(tur, dict) and isinstance(tur.get("agentId"), str):
                agent_id = tur["agentId"]
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result":
                    tid = part.get("tool_use_id")
                    paired = tool_by_id.get(tid) if isinstance(tid, str) else None
                    if paired is not None:
                        paired["result"] = {
                            "content": _result_text(part.get("content")),
                            "is_error": bool(part.get("is_error", False)),
                            "subagent_session_id": agent_id,
                        }
                elif part.get("type") == "text":
                    text = str(part.get("text", "")).strip()
                    if text:
                        user_blocks.append({"type": "text", "text": text})
            if user_blocks:
                messages.append({
                    "role": "user", "id": None, "model": None,
                    "timestamp": ts if isinstance(ts, str) else None,
                    "tokens": None, "blocks": user_blocks,
                })
    flush()
    return {"session": session, "messages": messages, "truncated": truncated}


def _codex_message_text(content: Any) -> str:
    """Join a codex message's content parts (input_text / output_text / text)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(p.get("text", ""))
        for p in content
        if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text")
    ]
    return "\n".join(x for x in parts if x).strip()


def parse_codex_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]:
    """Parse a Codex rollout JSONL into the normalized transcript schema.

    The canonical conversation is the sequence of ``response_item`` records:
    ``message`` (role user/assistant; developer/system boilerplate skipped),
    ``function_call`` / ``custom_tool_call`` and their ``*_output`` pairs, and
    ``reasoning`` (encrypted, so dropped). Assistant activity between user
    messages groups into one assistant message; an output pairs into its call
    by ``call_id``. ``session_meta`` supplies cwd / branch / timestamps.
    """
    session: dict[str, Any] = {
        "id": path.stem, "agent": "codex", "cwd": None, "git_branch": None,
        "title": None, "started_at": None, "ended_at": None, "model": None,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    messages: list[dict[str, Any]] = []
    tool_by_call: dict[str, dict[str, Any]] = {}
    truncated = False
    current: dict[str, Any] | None = None

    def new_assistant() -> dict[str, Any]:
        return {
            "role": "assistant", "id": None, "model": None,
            "timestamp": None, "tokens": None, "blocks": [],
        }

    def flush() -> None:
        nonlocal current
        if current is not None and current["blocks"]:
            messages.append(current)
        current = None

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            rtype = rec.get("type")

            if rtype == "session_meta":
                sid = payload.get("id") or payload.get("session_id")
                if isinstance(sid, str) and sid.strip():
                    session["id"] = sid.strip()
                if isinstance(payload.get("cwd"), str):
                    session["cwd"] = payload["cwd"]
                ts = payload.get("timestamp")
                if isinstance(ts, str):
                    session["started_at"] = ts
                    session["ended_at"] = ts
                git = payload.get("git")
                if isinstance(git, dict) and isinstance(git.get("branch"), str):
                    session["git_branch"] = git["branch"]
                continue

            if rtype != "response_item":
                continue
            if len(messages) >= max_messages:
                truncated = True
                break
            ptype = payload.get("type")

            if ptype == "message":
                role = payload.get("role")
                text = _codex_message_text(payload.get("content"))
                if role == "user":
                    flush()
                    if text:
                        messages.append({
                            "role": "user", "id": None, "model": None,
                            "timestamp": None, "tokens": None,
                            "blocks": [{"type": "text", "text": text}],
                        })
                elif role == "assistant":
                    if current is None:
                        current = new_assistant()
                    if text:
                        current["blocks"].append({"type": "text", "text": text})
                # developer / system messages are instruction boilerplate: skip.
            elif ptype in ("function_call", "custom_tool_call"):
                if current is None:
                    current = new_assistant()
                cid = payload.get("call_id")
                if ptype == "function_call":
                    tool_input: dict[str, Any] = {"arguments": payload.get("arguments")}
                else:
                    tool_input = {"input": payload.get("input")}
                block: dict[str, Any] = {
                    "type": "tool_use", "id": cid,
                    "name": str(payload.get("name", "")),
                    "input": tool_input, "result": None,
                }
                current["blocks"].append(block)
                if isinstance(cid, str):
                    tool_by_call[cid] = block
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                cid = payload.get("call_id")
                paired = tool_by_call.get(cid) if isinstance(cid, str) else None
                if paired is not None:
                    paired["result"] = {
                        "content": str(payload.get("output", "")),
                        "is_error": False, "subagent_session_id": None,
                    }
    flush()
    return {"session": session, "messages": messages, "truncated": truncated}


def _degraded(store: KBStore, session_id: str, reason: str) -> dict[str, Any]:
    obs = _read_observations(buffer_path(store, session_id))
    return {"available": False, "reason": reason, "observations": obs}


def load_transcript(
    store: KBStore, session_id: str, *, agent: str | None = None
) -> dict[str, Any]:
    """Locate + parse the raw transcript for ``session_id``.

    ``agent`` restricts the search ("claude" | "codex"); when None both are
    tried. Returns the normalized schema on success, or a degraded result
    (compact capture observations) when the raw file is missing/too large.
    """
    path: Path | None = None
    source_agent = ""
    if agent in (None, "claude"):
        path = find_claude_file(session_id)
        if path is not None:
            source_agent = "claude"
    if path is None and agent in (None, "codex"):
        from . import codex_rollout

        path = codex_rollout.find_rollout_by_session_id(session_id)
        if path is not None:
            source_agent = "codex"
    if path is None:
        return _degraded(store, session_id, f"raw transcript not found for session {session_id}")
    try:
        size = path.stat().st_size
    except OSError as e:
        return _degraded(store, session_id, f"cannot read transcript: {e}")
    if size > MAX_FILE_BYTES:
        return _degraded(store, session_id, f"transcript too large to render ({size} bytes)")

    if source_agent == "claude":
        parsed = parse_claude_transcript(path, max_messages=MAX_MESSAGES)
    else:
        parsed = parse_codex_transcript(path, max_messages=MAX_MESSAGES)
    return {"available": True, "source": {"agent": source_agent, "path": str(path)}, **parsed}
