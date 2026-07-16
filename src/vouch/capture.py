"""Auto-capture Claude Code sessions into review-gated summaries.

Passive harvest -> mechanical rollup -> one PENDING page proposal. No LLM.
`observe` appends compact observations to an ephemeral, gitignored scratch
buffer (`.vouch/captures/<session>.jsonl`); `finalize` rolls the buffer plus a
git-diff backstop into a single session-summary page proposal that a human
approves like any other write. The tool-activity path never calls approve().

`capture_answer` is the one exception, and it stays inside the gate: it ingests
a session's answer as a source, files receipt-backed claims, and self-approves
only what `proposals.approve` already allows (trusted-agent or the receipt
gate). With neither opt-in set it too leaves the claims pending. See
docs/superpowers/specs/2026-07-01-vouch-session-autocapture-design.md and
docs/superpowers/specs/2026-07-16-passive-answer-memory-design.md
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import ProposalStatus
from .secrets import mask_secrets
from .storage import KBStore

DEFAULT_ENABLED = True
DEFAULT_MIN_OBSERVATIONS = 3
DEFAULT_DEDUP_WINDOW_SECONDS = 60.0
CAPTURE_ACTOR = "vouch-capture"
CAPTURE_PAGE_TYPE = "session"


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = DEFAULT_ENABLED
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS


def load_config(store: KBStore) -> CaptureConfig:
    """Read ``capture:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
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
    # Mask credentials before anything is persisted: the buffer rolls into a
    # committed session page and the append-only audit log, so a secret that
    # reaches it is permanent. Masked first, so dedup compares masked text too.
    summary = mask_secrets(summary)
    if cmd is not None:
        cmd = mask_secrets(cmd)
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


def first_user_prompt(transcript_path: Path, *, max_chars: int = 240) -> str | None:
    """Mechanically extract the session's first genuine user prompt.

    The SessionEnd hook payload carries the transcript path; the first thing
    the human actually typed is the best available one-line description of
    what the session was about. Pure extraction — host wrapper messages
    (`<command-name>…`, `<task-notification>…`, caveats) and meta lines are
    skipped, no model is involved, so capture's no-LLM rule holds.
    """
    try:
        fh = transcript_path.open(encoding="utf-8")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user" or obj.get("isMeta"):
                continue
            msg = obj.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            texts: list[str] = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [
                    str(c.get("text", ""))
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
            for raw in texts:
                text = raw.strip()
                if not text or text.startswith("<"):
                    continue
                if text.lower().startswith("caveat:"):
                    continue
                collapsed = " ".join(text.split())
                if len(collapsed) > max_chars:
                    collapsed = collapsed[: max_chars - 1].rstrip() + "…"
                return collapsed
    return None


def _genuine_user_text(obj: dict[str, Any]) -> str | None:
    """The human-typed text of a user turn, or None for meta/wrapper/tool turns.

    Same filtering as ``first_user_prompt``: skips ``isMeta`` rows, host wrapper
    messages (``<command-name>…``, ``<task-notification>…``) and caveats, and
    tool_result turns (which carry no ``text`` block). Whitespace is collapsed.
    """
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
    for raw in texts:
        text = raw.strip()
        if not text or text.startswith("<"):
            continue
        if text.lower().startswith("caveat:"):
            continue
        return " ".join(text.split())
    return None


def _assistant_text(obj: dict[str, Any]) -> str | None:
    """Concatenated text blocks of an assistant turn, or None (tool-only turns).

    Keeps internal newlines between blocks — the answer becomes a source whose
    byte-offset receipts must match its stored bytes verbatim, so it is not
    re-wrapped or whitespace-collapsed the way a one-line prompt is.
    """
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    parts: list[str] = []
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
    joined = "\n".join(p.strip() for p in parts if p.strip()).strip()
    return joined or None


def last_exchange(
    transcript_path: Path,
    *,
    max_question_chars: int = 240,
    max_answer_chars: int = 20000,
) -> tuple[str, str] | None:
    """Extract the most recent (user question, assistant answer) from a transcript.

    Pure extraction — no model. Pairs the last assistant text turn with the most
    recent genuine user prompt at or before it (at Stop-hook time the transcript
    ends on the assistant turn, so that is the triggering question). Returns None
    when there is no assistant answer at all. The answer keeps its internal
    newlines; only the outer length is bounded to ``max_answer_chars``.
    """
    try:
        fh = transcript_path.open(encoding="utf-8")
    except OSError:
        return None
    question: str | None = None
    pair: tuple[str, str] | None = None
    with fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            user = _genuine_user_text(obj)
            if user is not None:
                question = user
                continue
            answer = _assistant_text(obj)
            if answer is not None:
                pair = (question or "", answer)
    if pair is None:
        return None
    q, a = pair
    if len(q) > max_question_chars:
        q = q[: max_question_chars - 1].rstrip() + "…"
    if len(a) > max_answer_chars:
        a = a[:max_answer_chars].rstrip()
    return q, a


def _excerpt(prompt: str, *, max_chars: int = 64) -> str:
    if len(prompt) <= max_chars:
        return prompt
    return prompt[: max_chars - 1].rstrip() + "…"


def _fallback_title(
    files: set[str], observations_count: int, generated_at: str | None
) -> str:
    """Describe the session by what it touched — never by its uuid."""
    date = f" {generated_at[:10]}" if generated_at else ""
    if not files:
        return f"session{date}: {observations_count} observation(s), no file changes"
    segments: dict[str, int] = {}
    for f in sorted(files):
        seg = f.split("/", 1)[0] if "/" in f else _basename(f)
        segments[seg] = segments.get(seg, 0) + 1
    top = sorted(segments, key=lambda s: (-segments[s], s))[:3]
    return f"session{date}: {', '.join(top)} — {len(files)} file(s)"


def build_summary_body(
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    project: str | None = None,
    generated_at: str | None = None,
    first_prompt: str | None = None,
) -> tuple[str, str]:
    tool_counts: dict[str, int] = {}
    files: set[str] = set(changed_files)
    commands: list[str] = []
    for obs in observations:
        tool = str(obs.get("tool", ""))
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        for f in obs.get("files") or []:
            files.add(str(f))
        cmd = obs.get("cmd")
        if cmd:
            commands.append(str(cmd))
    # The title is what a reviewer scans in the queue: lead with the human's
    # own words when the transcript offers them, else with what changed.
    # The session uuid stays in the body for traceability.
    if first_prompt:
        title = f"session: {_excerpt(first_prompt)}"
    else:
        title = _fallback_title(files, len(observations), generated_at)
    if project:
        title = f"{title} [{project}]"
    lines: list[str] = [f"# {title}", ""]
    if generated_at:
        lines.append(f"- generated: {generated_at}")
    lines += [f"- session: `{session_id}`", f"- observations: {len(observations)}", ""]
    if first_prompt:
        lines += ["## prompt", "", f"> {first_prompt}", ""]
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
    transcript_path: Path | None = None,
    mode: str = "auto",
    config: CaptureConfig | None = None,
) -> dict[str, Any]:
    """Roll a session buffer into PENDING summary proposal(s). No approve().

    Claude-Code-facing wrapper: resolves the transcript's first user prompt
    (its one host-specific enricher) and delegates to the host-blind
    `session_split.summarize`. `mode` forwards "auto" | "split" | "mechanical".
    If cwd is None (e.g., finalizing orphaned buffers of unknown origin), git
    changes are not included; transcript_path (from the SessionEnd hook payload)
    supplies the human's first prompt for the summary title when present.
    """
    from . import session_split  # deferred: breaks the capture<->session_split cycle
    intent = (
        first_user_prompt(transcript_path) if transcript_path is not None else None
    )
    return session_split.summarize(
        store, session_id, intent=intent, cwd=cwd, project=project,
        generated_at=generated_at, mode=mode, config=config,
    )


ANSWER_ACTOR = CAPTURE_ACTOR
DEFAULT_MIN_ANSWER_CHARS = 160
DEFAULT_MAX_ANSWER_CLAIMS = 12


def _answer_skip(session_id: str, reason: str) -> dict[str, Any]:
    return {
        "captured": False, "skipped": reason, "session_id": session_id,
        "source": None, "filed": 0, "approved": 0,
    }


def capture_answer(
    store: KBStore,
    session_id: str,
    transcript_path: Path,
    *,
    min_answer_chars: int = DEFAULT_MIN_ANSWER_CHARS,
    max_claims: int = DEFAULT_MAX_ANSWER_CLAIMS,
    config: CaptureConfig | None = None,
) -> dict[str, Any]:
    """Turn a session's latest Q&A into durable, recallable knowledge.

    Fires from a host Stop hook (the turn just finished). Extracts the last
    exchange, ingests the *answer* as a content-addressed source, files a
    receipt-backed claim per quotable span (``extract.extract_receipt_claims``),
    and approves each one the review gate allows — self-approval clears under
    ``review.approver_role: trusted-agent`` or, for these verbatim-quoting
    claims, ``review.auto_approve_on_receipt``. With neither gate on the claims
    stay pending: the review gate is honoured, never bypassed.

    Idempotent and quiet by design: an answer already ingested (same bytes) is
    skipped, and answers shorter than ``min_answer_chars`` (acknowledgements)
    are ignored, so a Stop hook firing every turn does not fill the KB with
    noise or duplicates.
    """
    import os

    from . import extract as extract_mod
    from . import proposals as proposals_mod
    from .proposals import ProposalError
    from .storage import ArtifactNotFoundError, sha256_hex

    # vouch's own LLM subprocesses set this so the agent session they spawn does
    # not capture itself back into the KB (mirrors load_config's contract).
    if os.environ.get("VOUCH_CAPTURE_DISABLE") == "1":
        return _answer_skip(session_id, "disabled-env")
    cfg = config or load_config(store)
    if not cfg.enabled:
        return _answer_skip(session_id, "disabled")

    exchange = last_exchange(transcript_path)
    if exchange is None:
        return _answer_skip(session_id, "no-answer")
    question, answer = exchange
    if len(answer) < min_answer_chars:
        return _answer_skip(session_id, "answer-too-short")

    content = answer.encode("utf-8")
    sid = sha256_hex(content)
    try:
        store.get_source(sid)
        return _answer_skip(session_id, "already-captured")
    except ArtifactNotFoundError:
        pass

    source = store.put_source(
        content,
        title=question or f"session {session_id} answer",
        source_type="message",
        tags=["session-answer"],
        metadata={"session_id": session_id, "question": question},
    )
    filed = extract_mod.extract_receipt_claims(
        store, source.id, proposed_by=ANSWER_ACTOR, limit=max_claims,
    )
    approved = 0
    for result in filed:
        try:
            proposals_mod.approve(
                store, result.proposal.id, approved_by=ANSWER_ACTOR,
                reason="auto-captured session answer (receipt verified)",
            )
            approved += 1
        except ProposalError:
            # gate closed (no trusted-agent, no receipt opt-in): leave pending.
            pass
    return {
        "captured": True, "skipped": None, "session_id": session_id,
        "source": source.id, "filed": len(filed), "approved": approved,
    }


def pending_count(store: KBStore) -> int:
    return sum(
        1 for p in store.list_proposals(ProposalStatus.PENDING)
        if p.proposed_by == CAPTURE_ACTOR
    )


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
      - skipped_recent: [id3, id4, ...]  sessions too recent to finalize
      - skipped_current: [id5]  the current session (always skipped)
    """
    finalized: list[str] = []
    skipped_recent: list[str] = []
    skipped_current: list[str] = []
    now = now_timestamp if now_timestamp is not None else time.time()

    caps_dir = captures_dir(store)
    if not caps_dir.exists():
        return {
            "finalized": finalized,
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
                finalize(
                    store, session_id, cwd=cwd,
                    generated_at=datetime.now(UTC).isoformat(),
                )
                finalized.append(session_id)
            except Exception:
                # Never let a finalize failure break the scan
                pass
        else:
            skipped_recent.append(session_id)

    return {
        "finalized": finalized,
        "skipped_recent": skipped_recent,
        "skipped_current": skipped_current,
    }
