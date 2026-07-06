"""Ingest OpenAI Codex CLI session rollouts into review-gated summaries.

Codex has no live hook stream the way claude-code does, but it persists
every session as a rollout file — ``$CODEX_HOME/sessions/YYYY/MM/DD/``
``rollout-<timestamp>-<uuid>.jsonl`` — holding user messages, tool calls,
and outputs: everything ``capture.build_summary_body`` needs, just after
the fact instead of live. ``vouch capture ingest-codex`` maps rollout
records into the same observation shape ``capture.observe`` produces, then
reuses the existing rollup (``build_summary_body`` -> ``propose_page``) so
a codex session yields the same kind of PENDING session-summary proposal a
claude session does: one code path from observation to proposal, two front
doors.

The rollout format is not a stable public contract. Parsing is therefore
tolerant — unknown record types are skipped — but a file that doesn't look
like a rollout at all degrades to :class:`CodexRolloutError` with an
actionable message, never a stack trace. Ingesting the same session twice
is a no-op: the session id in the rollout is the natural dedup key.

Never calls ``approve()`` — the review gate stays intact. A human reviews
the proposal with ``vouch review`` like any other write.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit, capture
from .models import Proposal, ProposalStatus
from .proposals import propose_page
from .storage import KBStore

# The default proposer when VOUCH_AGENT isn't set: rollouts are codex
# sessions, so the audit trail attributes them to the codex actor the
# adapter configures (`VOUCH_AGENT=codex` in adapters/codex/config.toml).
CODEX_ACTOR = "codex"

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_MAX_PROMPT_CHARS = 240
_PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.MULTILINE)
_EXIT_CODE_RE = re.compile(r"exited with code (\d+)")

# Codex tool calls that are session mechanics, not work worth summarizing.
_IGNORED_CALLS = frozenset({"update_plan", "write_stdin", "list_mcp_resources"})
_SHELL_CALLS = frozenset({"exec_command", "shell", "local_shell", "container.exec"})


class CodexRolloutError(RuntimeError):
    """Raised when a rollout file can't be read or doesn't parse as one.

    The CLI layer translates this into a clean ``Error: ...`` line via
    ``_cli_errors``; nothing is written to the KB when it's raised.
    """


@dataclass
class CodexSession:
    """The capture-relevant slice of one parsed rollout."""

    session_id: str
    cwd: str | None = None
    started_at: str | None = None
    first_prompt: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)


def _clean_prompt(raw: str) -> str | None:
    """Mirror ``capture.first_user_prompt``'s hygiene: skip host wrapper
    messages and meta lines, collapse whitespace, cap the length."""
    text = raw.strip()
    if not text or text.startswith("<"):
        return None
    if text.lower().startswith("caveat:"):
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_PROMPT_CHARS:
        collapsed = collapsed[: _MAX_PROMPT_CHARS - 1].rstrip() + "…"
    return collapsed


def _patch_observation(patch: str) -> dict[str, Any] | None:
    """Turn an apply_patch payload into an Edit/Created observation."""
    matches = _PATCH_FILE_RE.findall(patch)
    if not matches:
        return None
    files = [path.strip() for _, path in matches]
    verbs = {verb for verb, _ in matches}
    if verbs == {"Add"}:
        verb = "Created"
    elif verbs == {"Delete"}:
        verb = "Deleted"
    else:
        verb = "Edited"
    name = files[0].rsplit("/", 1)[-1]
    summary = f"{verb} {name}" if len(files) == 1 else f"{verb} {len(files)} files"
    return {"tool": "Edit", "summary": summary, "files": files}


def _observation_from_call(name: str, arguments: object) -> dict[str, Any] | None:
    """Map one codex ``function_call`` record into an observation, or None
    to skip. Shapes match ``capture.summarize_tool``'s conventions so the
    rollup renders codex and claude sessions identically."""
    if not name or name in _IGNORED_CALLS:
        return None
    args: dict[str, Any] = {}
    if isinstance(arguments, str):
        try:
            loaded = json.loads(arguments)
            if isinstance(loaded, dict):
                args = loaded
        except json.JSONDecodeError:
            args = {}
    elif isinstance(arguments, dict):
        args = arguments

    if name == "apply_patch":
        patch = str(args.get("input") or args.get("patch") or "")
        obs = _patch_observation(patch)
        if obs is not None:
            return obs

    if name in _SHELL_CALLS or name == "apply_patch":
        cmd = args.get("cmd") or args.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        first_line = cmd.splitlines()[0] if cmd else ""
        # Codex often applies patches through the shell tool as an
        # `apply_patch <<EOF` heredoc; surface those as file edits.
        if "apply_patch" in first_line:
            obs = _patch_observation(cmd)
            if obs is not None:
                return obs
        short = first_line[:60] if cmd else "command"
        out: dict[str, Any] = {"tool": "Bash", "summary": f"Ran: {short}"}
        if cmd:
            out["cmd"] = cmd[:200]
        return out

    # Anything else — MCP tools, custom tools — is still activity worth a
    # line in the summary, under its own name.
    return {"tool": str(name), "summary": f"{name} completed"}


def _iter_rollout_lines(path: Path) -> Iterator[str]:
    """Yield the rollout's lines one at a time, decoded as UTF-8.

    Streams from the file handle rather than slurping the whole file into
    memory — codex rollouts can carry large tool outputs. The zstd magic
    header is checked up front (compressed rollouts aren't parsed here).
    """
    try:
        fh = path.open("rb")
    except OSError as e:
        raise CodexRolloutError(f"cannot read rollout file {path}: {e}") from e
    with fh:
        if fh.read(4) == _ZSTD_MAGIC:
            raise CodexRolloutError(
                f"{path.name} is zstd-compressed; decompress it first "
                f"(`zstd -d {path.name}`) and ingest the .jsonl"
            )
        fh.seek(0)
        for raw_line in fh:
            yield raw_line.decode("utf-8", errors="replace")


def parse_rollout(path: Path) -> CodexSession:
    """Parse one rollout jsonl into a :class:`CodexSession`.

    Unknown record types are tolerated (the format drifts); a file that is
    unreadable, compressed, or has no ``session_meta`` record raises
    :class:`CodexRolloutError` with a message that says what to do next.
    """
    session_id: str | None = None
    cwd: str | None = None
    started_at: str | None = None
    first_prompt: str | None = None
    observations: list[dict[str, Any]] = []
    # call_id -> index into observations, so a later function_call_output
    # can mark the command as failed the way summarize_tool does live.
    open_calls: dict[str, int] = {}

    for line in _iter_rollout_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        if rtype == "session_meta" and session_id is None:
            sid = payload.get("id") or payload.get("session_id")
            if isinstance(sid, str) and sid.strip():
                session_id = sid.strip()
            raw_cwd = payload.get("cwd")
            if isinstance(raw_cwd, str) and raw_cwd:
                cwd = raw_cwd
            raw_ts = payload.get("timestamp")
            if isinstance(raw_ts, str) and raw_ts:
                started_at = raw_ts

        elif rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "function_call":
                obs = _observation_from_call(
                    str(payload.get("name") or ""), payload.get("arguments")
                )
                if obs is not None:
                    observations.append(obs)
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str) and obs["tool"] == "Bash":
                        open_calls[call_id] = len(observations) - 1
            elif ptype == "function_call_output":
                call_id = payload.get("call_id")
                idx = open_calls.pop(call_id, None) if isinstance(call_id, str) else None
                if idx is not None:
                    output = payload.get("output")
                    match = _EXIT_CODE_RE.search(str(output))
                    if match and match.group(1) != "0":
                        obs = observations[idx]
                        obs["summary"] = "Command failed: " + obs["summary"].removeprefix(
                            "Ran: "
                        )

        elif rtype == "event_msg":
            if payload.get("type") == "user_message" and first_prompt is None:
                msg = payload.get("message")
                if isinstance(msg, str):
                    first_prompt = _clean_prompt(msg)

    if session_id is None:
        raise CodexRolloutError(
            f"{path.name}: no session_meta record found — this doesn't look "
            f"like a codex rollout, or its schema has drifted; expected a "
            f"session_meta record carrying an `id`"
        )
    return CodexSession(
        session_id=session_id,
        cwd=cwd,
        started_at=started_at,
        first_prompt=first_prompt,
        observations=observations,
    )


def default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _rollout_meta_cwd(path: Path) -> str | None:
    """The ``cwd`` recorded in a rollout's ``session_meta``, or None."""
    try:
        with path.open(encoding="utf-8") as fh:
            first = json.loads(fh.readline())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return None
    payload = first.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
        return payload["cwd"]
    return None


def find_latest_rollout(cwd: Path, *, codex_home: Path | None = None) -> Path | None:
    """Newest rollout whose session ran in ``cwd``, or None.

    Rollout filenames embed the full timestamp
    (``rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl``), so ``path.name`` orders
    chronologically regardless of directory. Rather than sort the whole
    ``sessions/`` tree up front, keep the newest match seen and skip
    opening any candidate that can't beat it — most rollouts never get read.
    """
    sessions = (codex_home or default_codex_home()) / "sessions"
    if not sessions.is_dir():
        return None
    target = str(cwd.resolve())
    best: Path | None = None
    for path in sessions.rglob("rollout-*.jsonl"):
        if best is not None and path.name <= best.name:
            continue
        if _rollout_meta_cwd(path) == target:
            best = path
    return best


def find_existing_proposal(store: KBStore, session_id: str) -> Proposal | None:
    """Any proposal (any status) already filed for this session."""
    for proposal in store.list_proposals(None):
        if proposal.session_id == session_id:
            return proposal
    return None


def find_rollout_by_session_id(
    session_id: str, *, codex_home: Path | None = None
) -> Path | None:
    """The rollout file for one session id — codex embeds the id in the
    filename (``rollout-<stamp>-<uuid>.jsonl``), so no file needs opening.

    The session id comes from a hook payload, so it's matched as a literal
    filename suffix rather than interpolated into the glob pattern: a
    payload carrying glob metacharacters (``*``, ``?``, ``[``) can't widen
    the search or change its semantics.
    """
    sessions = (codex_home or default_codex_home()) / "sessions"
    if not sessions.is_dir() or not session_id.strip():
        return None
    suffix = f"-{session_id}.jsonl"
    best: Path | None = None
    for path in sessions.rglob("rollout-*.jsonl"):
        if not path.name.endswith(suffix):
            continue
        if best is None or path.name > best.name:
            best = path
    return best


def _comparable_body(body: str) -> str:
    """The summary body minus its generation timestamp, so re-ingesting an
    unchanged rollout compares equal across runs."""
    return "\n".join(
        line for line in body.splitlines() if not line.startswith("- generated:")
    )


def ingest_rollout(
    store: KBStore,
    path: Path,
    *,
    actor: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Roll one rollout into a PENDING summary proposal. No ``approve()``.

    Honours the same ``capture:`` config as live capture (``enabled``,
    ``min_observations``) so the two front doors gate identically, and
    dedups on the rollout's session id: at most one proposal per session,
    ever. Because codex's Stop hook fires per *turn* rather than at session
    end, re-ingesting a session that grew since the last ingest refreshes
    the still-PENDING proposal in place (same id, updated summary) instead
    of filing a duplicate; an unchanged rollout is a flat no-op, and a
    decided proposal is history — it blocks re-ingest regardless.
    """
    cfg = capture.load_config(store)
    session = parse_rollout(path)
    if not cfg.enabled:
        return {
            "session_id": session.session_id,
            "captured": len(session.observations),
            "summary_proposal_id": None,
            "skipped": "disabled",
        }

    existing = find_existing_proposal(store, session.session_id)
    if existing is not None and existing.status != ProposalStatus.PENDING:
        return {
            "session_id": session.session_id,
            "captured": len(session.observations),
            "summary_proposal_id": existing.id,
            "skipped": "already-ingested",
        }

    if len(session.observations) < cfg.min_observations:
        return {
            "session_id": session.session_id,
            "captured": len(session.observations),
            "summary_proposal_id": None,
            "skipped": "below-min",
        }

    project = None
    if session.cwd:
        project = session.cwd.rstrip("/").rsplit("/", 1)[-1] or None
    title, body = capture.build_summary_body(
        session.session_id,
        session.observations,
        [],  # no git backstop: the session's working tree is long gone
        "",
        project=project,
        generated_at=generated_at or session.started_at,
        first_prompt=session.first_prompt,
    )
    resolved_actor = actor or os.environ.get("VOUCH_AGENT") or CODEX_ACTOR

    if existing is not None:
        if _comparable_body(body) == _comparable_body(
            str(existing.payload.get("body", ""))
        ):
            return {
                "session_id": session.session_id,
                "captured": len(session.observations),
                "summary_proposal_id": existing.id,
                "skipped": "already-ingested",
            }
        refreshed = existing.model_copy(deep=True)
        refreshed.payload["title"] = title.strip()
        refreshed.payload["body"] = body
        store.update_proposal(refreshed)
        audit.log_event(
            store.kb_dir,
            event="proposal.page.update",
            actor=resolved_actor,
            object_ids=[existing.id],
            data={"reason": "codex rollout re-ingest", "captured": len(session.observations)},
        )
        return {
            "session_id": session.session_id,
            "captured": len(session.observations),
            "summary_proposal_id": existing.id,
            "updated": True,
        }

    proposal = propose_page(
        store,
        title=title,
        body=body,
        page_type=capture.CAPTURE_PAGE_TYPE,
        proposed_by=resolved_actor,
        session_id=session.session_id,
        rationale="ingested codex session rollout",
    )
    return {
        "session_id": session.session_id,
        "captured": len(session.observations),
        "summary_proposal_id": proposal.id,
    }


def ingest_hook_payload(
    store: KBStore | None,
    payload: dict[str, Any],
    *,
    codex_home: Path | None = None,
) -> dict[str, Any] | None:
    """Handle one codex Stop-hook payload; never raises.

    The hook wire (`vouch capture ingest-codex --hook`) must exit 0 even on
    failure — a capture problem must never break the user's codex turn,
    the same rule ``capture observe`` follows. Returns the ingest result,
    or None when there was nothing safe to do.
    """
    try:
        if store is None:
            return None
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        rollout: Path | None = None
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript.endswith(".jsonl"):
            candidate = Path(transcript)
            if candidate.is_file():
                rollout = candidate
        if rollout is None:
            rollout = find_rollout_by_session_id(session_id, codex_home=codex_home)
        if rollout is None:
            return None
        return ingest_rollout(store, rollout)
    except Exception:
        return None
