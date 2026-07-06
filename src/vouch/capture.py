"""Auto-capture Claude Code sessions into review-gated summaries.

Passive harvest -> mechanical rollup -> one PENDING claim proposal. No LLM.
`observe` appends compact observations to an ephemeral, gitignored scratch
buffer (`.vouch/captures/<session>.jsonl`); `finalize` rolls the buffer plus a
git-diff backstop into a single session-summary claim proposal that a human
approves like any other write. The mechanical record is registered as an
immutable transcript Source and cited as the claim's evidence, so the
"claims must cite sources" invariant holds for captures too. Never calls
approve() — the review gate stays intact.
See docs/superpowers/specs/2026-07-01-vouch-session-autocapture-design.md

Sessions used to file as PAGE proposals; pending pages from that era are
still recognized everywhere here and in summarize.py (kind=page,
payload.type == "session"), they just no longer get created.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import Proposal, ProposalKind, ProposalStatus
from .proposals import _slugify, propose_claim
from .storage import KBStore

DEFAULT_ENABLED = True
DEFAULT_MIN_OBSERVATIONS = 3
DEFAULT_DEDUP_WINDOW_SECONDS = 60.0
CAPTURE_ACTOR = "vouch-capture"
# Legacy: sessions filed as PAGE proposals with this page type before they
# became claims. Kept so pending pages from that era stay recognizable.
CAPTURE_PAGE_TYPE = "session"
CAPTURE_CLAIM_TYPE = "session"
PROMPT_TOOL = "prompt"
MAX_PROMPT_CHARS = 4000


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = DEFAULT_ENABLED
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS


def load_config(store: KBStore) -> CaptureConfig:
    """Read ``capture:`` from config.yaml; fall back to defaults.

    ``VOUCH_CAPTURE_OFF=1`` wins over config: summarize.generate sets it on
    the LLM subprocess so an agent CLI used as the summarizer (``claude -p``
    fires this repo's own hooks) cannot capture its own summarization run
    and file junk sessions.
    """
    if os.environ.get("VOUCH_CAPTURE_OFF") == "1":
        return CaptureConfig(enabled=False)
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
    except (OSError, yaml.YAMLError):
        return CaptureConfig()
    if not isinstance(loaded, dict):
        return CaptureConfig()
    raw = loaded.get("capture")
    if not isinstance(raw, dict):
        return CaptureConfig()
    return CaptureConfig(
        enabled=bool(raw.get("enabled", DEFAULT_ENABLED)),
        min_observations=int(raw.get("min_observations", DEFAULT_MIN_OBSERVATIONS)),
        dedup_window_seconds=float(
            raw.get("dedup_window_seconds", DEFAULT_DEDUP_WINDOW_SECONDS)
        ),
    )


def captures_dir(store: KBStore) -> Path:
    return store.kb_dir / "captures"


def buffer_path(store: KBStore, session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_").strip() or "unknown"
    return captures_dir(store) / f"{safe}.jsonl"


_OBSERVED_TOOLS = frozenset({
    "Read", "Edit", "Write", "Update", "Bash",
    "Grep", "Glob", "WebFetch", "WebSearch", "Task", "NotebookEdit",
})


def _read_observations(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _dedup_key(tool: str, summary: str) -> str:
    return f"{tool}\x00{summary}"


def observe(
    store: KBStore,
    session_id: str,
    *,
    tool: str,
    summary: str,
    files: list[str] | None = None,
    cmd: str | None = None,
    now: float | None = None,
    config: CaptureConfig | None = None,
) -> bool:
    """Append one observation to the session buffer. Returns True if written."""
    cfg = config or load_config(store)
    if not cfg.enabled:
        return False
    ts = time.time() if now is None else now
    path = buffer_path(store, session_id)
    key = _dedup_key(tool, summary)
    for obs in reversed(_read_observations(path)):
        if ts - float(obs.get("ts", 0.0)) > cfg.dedup_window_seconds:
            break
        if _dedup_key(str(obs.get("tool", "")), str(obs.get("summary", ""))) == key:
            return False
    record: dict[str, Any] = {"ts": ts, "tool": tool, "summary": summary}
    if files:
        record["files"] = files
    if cmd:
        record["cmd"] = cmd
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return True


def _sentence_clip(text: str, limit: int = 200) -> str:
    """First non-empty line, whole. Only walls of text past `limit` get cut,
    and then at a word boundary — never mid-word."""
    line = next((ln.strip() for ln in text.strip().splitlines() if ln.strip()), "")
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0].rstrip()
    return f"{cut}…"


def record_prompt(
    store: KBStore,
    session_id: str,
    prompt: str,
    *,
    now: float | None = None,
    config: CaptureConfig | None = None,
) -> bool:
    """Append the user's prompt to the session buffer. Returns True if written.

    Prompts don't count toward the min-observations gate — a chat-only
    session with no tool activity still produces nothing.
    """
    cfg = config or load_config(store)
    if not cfg.enabled:
        return False
    text = str(prompt).strip()
    if not text:
        return False
    ts = time.time() if now is None else now
    path = buffer_path(store, session_id)
    record = {
        "ts": ts,
        "tool": PROMPT_TOOL,
        "summary": _sentence_clip(text),
        "prompt": text[:MAX_PROMPT_CHARS],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return True


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] or path


def summarize_tool(
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    tool_response: object,
) -> dict[str, Any] | None:
    """Turn a PostToolUse payload into a compact observation, or None to skip."""
    if not tool_name or tool_name not in _OBSERVED_TOOLS:
        return None
    ti = tool_input or {}
    out: dict[str, Any] = {"tool": tool_name}
    fp = ti.get("file_path")
    if isinstance(fp, str) and fp:
        out["files"] = [fp]
    if tool_name in {"Read", "Edit", "Write", "Update", "NotebookEdit"}:
        name = _basename(fp) if isinstance(fp, str) and fp else "file"
        verb = {"Read": "Read", "Write": "Created"}.get(tool_name, "Edited")
        out["summary"] = f"{verb} {name}"
    elif tool_name == "Bash":
        cmd = ti.get("command")
        short = str(cmd).splitlines()[0][:60] if cmd else "command"
        if cmd:
            out["cmd"] = str(cmd)[:200]
        text = str(tool_response).lower()
        failed = "error" in text or "failed" in text
        out["summary"] = f"Command failed: {short}" if failed else f"Ran: {short}"
    elif tool_name in {"Grep", "Glob"}:
        out["summary"] = f"{tool_name} {str(ti.get('pattern', ''))[:40]}"
    elif tool_name in {"WebFetch", "WebSearch"}:
        target = ti.get("url") or ti.get("query") or ""
        out["summary"] = f"Fetched: {str(target)[:60]}"
    else:  # Task
        out["summary"] = f"{tool_name} completed"
    return out


def _git_changes(cwd: Path) -> tuple[list[str], str]:
    """Return (changed_files, diff_stat). Empty on any failure / non-repo."""
    try:
        names = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], ""
    files = [f for f in names.stdout.splitlines() if f.strip()]
    if not files:
        return [], ""
    try:
        stat = subprocess.run(
            ["git", "diff", "HEAD", "--stat"],
            cwd=cwd, capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        stat = ""
    return files, stat


_EDIT_TOOLS = frozenset({"Edit", "Update", "NotebookEdit"})


def _uniq_add(seq: list[str], name: str) -> None:
    if name and name not in seq:
        seq.append(name)


def _files_word(n: int) -> str:
    return "file" if n == 1 else "files"


def _name_list(names: list[str], limit: int = 2) -> str:
    head = ", ".join(names[:limit])
    extra = len(names) - limit
    return f"{head} +{extra} more" if extra > 0 else head


def _title_gist(
    edited: list[str],
    created: list[str],
    read: list[str],
    cmd_tokens: dict[str, int],
) -> str:
    """One mechanical phrase saying what the session did, for the page title.

    The recall digest injects page titles only, so this phrase is all a
    reviewer (and a later session) sees before deciding to open the body.
    """
    touched = edited + [n for n in created if n not in edited]
    if touched:
        return f"edited {_name_list(touched)}"
    n_cmds = sum(cmd_tokens.values())
    if n_cmds:
        top = sorted(cmd_tokens, key=lambda t: (-cmd_tokens[t], t))[:3]
        return f"ran {n_cmds} commands ({', '.join(top)})"
    if read:
        return f"read {_name_list(read)}"
    return ""


def build_summary_body(
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    project: str | None = None,
    generated_at: str | None = None,
) -> tuple[str, str]:
    prompts = [o for o in observations if str(o.get("tool", "")) == PROMPT_TOOL]
    observations = [o for o in observations if str(o.get("tool", "")) != PROMPT_TOOL]
    tool_counts: dict[str, int] = {}
    files: set[str] = set(changed_files)
    commands: list[str] = []
    edited: list[str] = []
    created: list[str] = []
    read: list[str] = []
    cmd_tokens: dict[str, int] = {}
    for obs in observations:
        tool = str(obs.get("tool", ""))
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        names = [_basename(str(f)) for f in obs.get("files") or []]
        if tool in _EDIT_TOOLS:
            for n in names:
                _uniq_add(edited, n)
        elif tool == "Write":
            for n in names:
                _uniq_add(created, n)
        elif tool == "Read":
            for n in names:
                _uniq_add(read, n)
        for f in obs.get("files") or []:
            files.add(str(f))
        cmd = obs.get("cmd")
        if cmd:
            commands.append(str(cmd))
            parts = str(cmd).split()
            tok = _basename(parts[0]) if parts else "command"
            cmd_tokens[tok] = cmd_tokens.get(tok, 0) + 1
    for f in changed_files:
        name = _basename(str(f))
        if name not in created:
            _uniq_add(edited, name)
    gist = _title_gist(edited, created, read, cmd_tokens)
    if prompts:
        # the user's own words, whole — never a mid-sentence "…" cut
        first_prompt = str(prompts[0].get("prompt") or prompts[0].get("summary") or "")
        title = f"session: {_sentence_clip(first_prompt)}"
    else:
        base = f"session summary: {project or 'workspace'}"
        sid8 = session_id[:8]
        title = f"{base} - {gist} ({sid8})" if gist else f"{base} ({sid8})"
    lines: list[str] = [f"# {title}", ""]
    if generated_at:
        lines.append(f"- generated: {generated_at}")
    lines += [f"- session: `{session_id}`", f"- observations: {len(observations)}", ""]
    if prompts:
        lines += ["## prompt", ""]
        first_text = str(prompts[0].get("prompt") or prompts[0].get("summary") or "")
        lines += [f"> {ln}" if ln else ">" for ln in first_text.strip().splitlines()]
        lines.append("")
        if len(prompts) > 1:
            lines += ["## follow-up prompts", ""]
            for p in prompts[1:11]:
                lines.append(f"- {_sentence_clip(str(p.get('prompt') or p.get('summary') or ''))}")
            if len(prompts) > 11:
                lines.append(f"- +{len(prompts) - 11} more")
            lines.append("")
    happened: list[str] = []
    if edited:
        happened.append(
            f"- edited {len(edited)} {_files_word(len(edited))}: {_name_list(edited, 5)}"
        )
    if created:
        happened.append(
            f"- created {len(created)} {_files_word(len(created))}: {_name_list(created, 5)}"
        )
    if cmd_tokens:
        n_cmds = sum(cmd_tokens.values())
        top = sorted(cmd_tokens, key=lambda t: (-cmd_tokens[t], t))[:3]
        counts = ", ".join(f"{t} x{cmd_tokens[t]}" for t in top)
        word = "command" if n_cmds == 1 else "commands"
        happened.append(f"- ran {n_cmds} {word} ({counts})")
    if read:
        happened.append(f"- read {len(read)} {_files_word(len(read))}")
    if happened:
        lines += ["## what happened", "", *happened, ""]
    if files:
        lines += ["## files modified this session", ""]
        lines += [f"- {f}" for f in sorted(files)[:20]]
        lines.append("")
    if git_stat:
        lines += ["## git changes", "", "```", git_stat, "```", ""]
    if tool_counts:
        lines += ["## activity", ""]
        lines += [f"- {t}: {tool_counts[t]}" for t in sorted(tool_counts)]
        lines.append("")
    if commands:
        lines += ["## notable commands", ""]
        lines += [f"- `{c}`" for c in commands[:10]]
        lines.append("")
    if observations:
        lines += ["## observations", ""]
        lines += [f"- {o.get('summary', '')}" for o in observations[:30]]
        lines.append("")
    return title, "\n".join(lines).rstrip() + "\n"


def finalize(
    store: KBStore,
    session_id: str,
    *,
    cwd: Path | None = None,
    project: str | None = None,
    generated_at: str | None = None,
    config: CaptureConfig | None = None,
    transcript_path: Path | None = None,
    allow_llm: bool = True,
) -> dict[str, Any]:
    """Roll a session buffer into one PENDING summary proposal. No approve().

    If cwd is None (e.g., when finalizing orphaned buffers with unknown origin),
    git changes are not included. Otherwise, git changes from cwd are included.

    When `capture.summary_mode` is `auto` and `summary_llm_cmd` is configured,
    an LLM narrative is generated post-session from the mechanical record (plus
    the transcript excerpt when the host provided `transcript_path`) and folded
    into the pending body. Any LLM failure degrades to mechanical-only — the
    proposal is always filed. `allow_llm=False` skips the attempt (bulk sweeps
    of orphaned buffers must not serialize LLM calls); `vouch summarize`
    enriches those later.
    """
    cfg = config or load_config(store)
    path = buffer_path(store, session_id)
    observations = _read_observations(path)
    if not cfg.enabled:
        return {"captured": len(observations), "summary_proposal_id": None,
                "skipped": "disabled"}
    # Only include git context if cwd is explicitly provided (known origin)
    # For cleanup of orphaned buffers, cwd=None, so skip git context
    if cwd is not None:
        changed_files, git_stat = _git_changes(cwd)
    else:
        changed_files, git_stat = [], ""
    substantive = [
        o for o in observations if str(o.get("tool", "")) != PROMPT_TOOL
    ]
    total = len(substantive) + len(changed_files)
    if total < cfg.min_observations:
        if path.exists():
            path.unlink()
        return {"captured": total, "summary_proposal_id": None,
                "skipped": "below-min"}
    title, body = build_summary_body(
        session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at,
    )
    # The pre-LLM mechanical record is the claim's evidence: register it as
    # an immutable transcript source before any narrative gets folded in, so
    # what the claim cites is exactly what the hooks observed.
    source = store.put_source(
        body.encode("utf-8"),
        title=title,
        locator=(
            str(transcript_path) if transcript_path is not None
            else f"claude-code-session:{session_id}"
        ),
        source_type="transcript",
        tags=["session-capture"],
        metadata={"session_id": session_id},
    )
    ai_summary = False
    if allow_llm:
        try:
            # lazy import: observe's per-tool-call fast path must not pay for it
            from . import summarize as summarize_mod

            scfg = summarize_mod.load_summary_config(store)
            if scfg.mode == "auto" and scfg.configured:
                excerpt = summarize_mod.read_transcript_excerpt(
                    transcript_path, scfg.max_transcript_chars
                )
                narrative = summarize_mod.generate(
                    summarize_mod.build_record(body, excerpt), scfg
                )
                if narrative:
                    body = summarize_mod.insert_summary_section(body, narrative)
                    ai_summary = True
        except Exception:
            # the narrative is best-effort; the mechanical summary always files.
            ai_summary = False
    result = propose_claim(
        store,
        text=body,
        evidence=[source.id],
        proposed_by=CAPTURE_ACTOR,
        claim_type=CAPTURE_CLAIM_TYPE,
        tags=["session"],
        session_id=session_id,
        rationale="auto-captured session summary",
        # keep the claim id keyed by session, not by the gist in the title
        slug_hint=_slugify(f"session summary {project or 'workspace'} {session_id}"),
    )
    if path.exists():
        path.unlink()
    return {
        "captured": total,
        "summary_proposal_id": result.proposal.id,
        "ai_summary": ai_summary,
    }


def pending_count(store: KBStore) -> int:
    return sum(
        1 for p in store.list_proposals(ProposalStatus.PENDING)
        if p.proposed_by == CAPTURE_ACTOR
    )


def is_capture_proposal(proposal: Proposal) -> bool:
    """True for a captured-session summary proposal, whichever era filed it.

    New captures are CLAIM proposals; pre-claim captures were PAGE proposals.
    Both carry ``payload.type == "session"`` and the capture actor, so every
    review surface treats them uniformly.
    """
    if proposal.proposed_by != CAPTURE_ACTOR:
        return False
    ptype = str(proposal.payload.get("type") or "")
    if proposal.kind == ProposalKind.CLAIM:
        return ptype == CAPTURE_CLAIM_TYPE
    if proposal.kind == ProposalKind.PAGE:
        return ptype == CAPTURE_PAGE_TYPE
    return False


def proposal_body(proposal: Proposal) -> str:
    """The summary markdown of a capture proposal — claims keep it in
    ``text``, legacy pages in ``body``."""
    key = "text" if proposal.kind == ProposalKind.CLAIM else "body"
    return str(proposal.payload.get(key) or "")


def set_proposal_body(proposal: Proposal, body: str) -> None:
    key = "text" if proposal.kind == ProposalKind.CLAIM else "body"
    proposal.payload[key] = body


def list_sessions(store: KBStore) -> list[dict[str, Any]]:
    """Captured sessions across both stages, for review surfaces.

    stage "buffer": a still-open capture buffer — the session is either
    running or its tab closed without a SessionEnd. stage "pending": a filed
    summary proposal awaiting review. ``summarized`` marks whether the LLM
    narrative is present; unsummarized entries are what a review page lists
    next to its Summarize button, summarized ones belong in a pending queue.
    """
    from . import summarize as summarize_mod

    sessions: list[dict[str, Any]] = []
    cdir = captures_dir(store)
    if cdir.exists():
        for path in sorted(
            cdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            observations = _read_observations(path)
            title = next(
                (
                    str(o.get("summary") or "")
                    for o in observations
                    if str(o.get("tool", "")) == PROMPT_TOOL and o.get("summary")
                ),
                None,
            )
            sessions.append({
                "session_id": path.stem,
                "stage": "buffer",
                "proposal_id": None,
                "kind": None,
                "title": title,
                "summarized": False,
                "observations": len(observations),
                "last_activity": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=UTC
                ).isoformat(),
            })
    pending = [
        p for p in store.list_proposals(ProposalStatus.PENDING)
        if is_capture_proposal(p)
    ]
    pending.sort(key=lambda p: p.proposed_at, reverse=True)
    for p in pending:
        body = proposal_body(p)
        title = str(p.payload.get("title") or "").strip() or None
        if title is None and body.strip():
            title = body.lstrip().splitlines()[0].lstrip("# ").strip() or None
        sessions.append({
            "session_id": p.session_id,
            "stage": "pending",
            "proposal_id": p.id,
            "kind": p.kind.value,
            "title": title,
            "summarized": summarize_mod.SUMMARY_SECTION_HEADER in body,
            "observations": None,
            "last_activity": p.proposed_at.isoformat(),
        })
    return sessions


def is_stale_buffer(
    path: Path,
    *,
    max_age_seconds: float = 3600.0,
    now_timestamp: float | None = None,
) -> bool:
    """Check if a buffer file's mtime is older than max_age_seconds."""
    if not path.exists():
        return False
    now = now_timestamp if now_timestamp is not None else time.time()
    mtime = path.stat().st_mtime
    age = now - mtime
    return age > max_age_seconds


def finalize_all_except(
    store: KBStore,
    current_session_id: str,
    *,
    max_age_seconds: float = 3600.0,
    cwd: Path | None = None,
    now_timestamp: float | None = None,
) -> dict[str, Any]:
    """Finalize all buffers except current_session_id, if they're older than max_age.

    Returns dict with keys:
      - finalized: [session_id1, session_id2, ...]  session IDs that were finalized
      - finalized_proposals: [proposal_id, ...]  summary proposals that were filed
        (lets `vouch capture sweep` enrich exactly what it just created,
        without touching the pre-existing backlog)
      - skipped_recent: [id3, id4, ...]  sessions too recent to finalize
      - skipped_current: [id5]  the current session (always skipped)
    """
    finalized: list[str] = []
    finalized_proposals: list[str] = []
    skipped_recent: list[str] = []
    skipped_current: list[str] = []
    now = now_timestamp if now_timestamp is not None else time.time()

    caps_dir = captures_dir(store)
    if not caps_dir.exists():
        return {
            "finalized": finalized,
            "finalized_proposals": finalized_proposals,
            "skipped_recent": skipped_recent,
            "skipped_current": skipped_current,
        }

    for path in sorted(caps_dir.glob("*.jsonl")):
        # Extract session ID from filename (e.g., "session-id.jsonl" -> "session-id")
        session_id = path.stem

        if session_id == current_session_id:
            skipped_current.append(session_id)
            continue

        if is_stale_buffer(path, max_age_seconds=max_age_seconds, now_timestamp=now):
            try:
                result = finalize(
                    store, session_id, cwd=cwd,
                    generated_at=datetime.now(UTC).isoformat(),
                    # sweeping N orphans must not serialize N llm calls inline;
                    # `vouch capture sweep` enriches its own finalized_proposals
                    # afterwards, and `vouch summarize --all` covers the rest.
                    allow_llm=False,
                )
                finalized.append(session_id)
                if result.get("summary_proposal_id"):
                    finalized_proposals.append(str(result["summary_proposal_id"]))
            except Exception:
                # Never let a finalize failure break the scan
                pass
        else:
            skipped_recent.append(session_id)

    return {
        "finalized": finalized,
        "finalized_proposals": finalized_proposals,
        "skipped_recent": skipped_recent,
        "skipped_current": skipped_current,
    }
