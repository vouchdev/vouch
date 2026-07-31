"""Auto-capture Claude Code sessions into review-gated summaries.

Passive harvest -> mechanical rollup -> one PENDING page proposal. No LLM.
`observe` appends compact observations to an ephemeral, gitignored scratch
buffer (`.vouch/captures/<session>.jsonl`); `finalize` rolls the buffer plus a
git-diff backstop into a single session-summary page proposal that a human
approves like any other write. The tool-activity path never calls approve().

Answer memory is the one path that files claims, and it stays inside the gate:
a session's answers are ingested as a source, receipt-backed claims are filed,
and self-approval clears only what `proposals.approve` already allows
(trusted-agent or the receipt gate). With neither opt-in set the claims stay
pending. `capture.answer_mode` picks the extraction unit: "session" (default)
extracts once at SessionEnd from the full transcript via
`capture_session_answers`, so spans are cut with every turn in view and a
single per-session claim budget; "turn" restores the legacy per-Stop-hook
`capture_answer`, which sees one answer at a time. See
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

from .config_coerce import coerce_bool, coerce_numeric
from .enrich import Enrichment
from .models import ProposalStatus
from .secrets import mask_secrets
from .storage import KBStore

DEFAULT_ENABLED = True
DEFAULT_REALTIME = False
DEFAULT_MIN_OBSERVATIONS = 3
DEFAULT_DEDUP_WINDOW_SECONDS = 60.0
# "session": claims are extracted once at SessionEnd from the full transcript.
# "turn": legacy behaviour — claims filed from each answer on every Stop hook.
DEFAULT_ANSWER_MODE = "session"
_ANSWER_MODES = frozenset({"session", "turn"})
CAPTURE_ACTOR = "vouch-capture"
CAPTURE_PAGE_TYPE = "session"


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = DEFAULT_ENABLED
    realtime: bool = DEFAULT_REALTIME
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS
    answer_mode: str = DEFAULT_ANSWER_MODE


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
    answer_mode = str(raw.get("answer_mode", DEFAULT_ANSWER_MODE)).strip().lower()
    if answer_mode not in _ANSWER_MODES:
        answer_mode = DEFAULT_ANSWER_MODE
    return CaptureConfig(
        enabled=coerce_bool(raw.get("enabled", DEFAULT_ENABLED), DEFAULT_ENABLED),
        realtime=coerce_bool(raw.get("realtime", DEFAULT_REALTIME), DEFAULT_REALTIME),
        min_observations=coerce_numeric(
            raw.get("min_observations", DEFAULT_MIN_OBSERVATIONS),
            DEFAULT_MIN_OBSERVATIONS,
            int,
        ),
        dedup_window_seconds=coerce_numeric(
            raw.get("dedup_window_seconds", DEFAULT_DEDUP_WINDOW_SECONDS),
            DEFAULT_DEDUP_WINDOW_SECONDS,
            float,
        ),
        answer_mode=answer_mode,
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
    tool_use_id: str | None = None,
) -> bool:
    """Append one observation to the session buffer. Returns True if written."""
    cfg = config or load_config(store)
    if not cfg.enabled:
        return False
    # Opt-in: per-tool PostToolUse harvest is off by default (#602). Finalize
    # rebuilds the same observation shape from the transcript instead.
    if not cfg.realtime:
        return False
    # Mask credentials before anything is persisted: the buffer rolls into a
    # committed session page and the append-only audit log, so a secret that
    # reaches it is permanent. Masked first, so dedup compares masked text too.
    summary = mask_secrets(summary)
    if cmd is not None:
        cmd = mask_secrets(cmd)
    ts = time.time() if now is None else now
    path = buffer_path(store, session_id)
    observations = _read_observations(path)
    # Event-identity dedup: exact and window-free. With user-level AND
    # legacy project-level hooks both wired (global install coexisting
    # with a per-project one), the same PostToolUse event can reach us
    # twice; the (session, tool_use_id) pair identifies it regardless of
    # which wiring delivered it or how the command strings drifted.
    if tool_use_id and any(
        obs.get("tool_use_id") == tool_use_id for obs in observations
    ):
        return False
    key = _dedup_key(tool, summary)
    for obs in reversed(observations):
        if ts - float(obs.get("ts", 0.0)) > cfg.dedup_window_seconds:
            break
        if _dedup_key(str(obs.get("tool", "")), str(obs.get("summary", ""))) == key:
            return False
    record: dict[str, Any] = {"ts": ts, "tool": tool, "summary": summary}
    if tool_use_id:
        record["tool_use_id"] = tool_use_id
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


def observations_from_transcript(transcript_path: Path) -> list[dict[str, Any]]:
    """Rebuild PostToolUse-shaped observations from a host transcript.

    Used when ``capture.realtime`` is off so SessionEnd finalize can still feed
    ``session_split.summarize``'s ``min_observations`` gate without the
    per-tool buffer. Tries the Claude parser first, then Codex — same
    host-neutral detection ``transcript.load_transcript`` uses when the
    caller already has a path (#602 / #645 review).

    Bound: inherits each parser's ``max_messages=2000`` default, so very
    long sessions may drop their oldest tool calls (the realtime buffer
    had no such cap).
    """
    from .transcript import parse_claude_transcript, parse_codex_transcript

    parsed: dict[str, Any] | None = None
    for parse in (parse_claude_transcript, parse_codex_transcript):
        try:
            candidate = parse(transcript_path)
        except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
            continue
        if not isinstance(candidate, dict):
            continue
        obs = _observations_from_parsed(candidate)
        if obs:
            return obs
        # keep the first successful parse as a fallback (empty session)
        if parsed is None:
            parsed = candidate
    return _observations_from_parsed(parsed) if parsed is not None else []


def _observations_from_parsed(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn normalized transcript ``messages``/``blocks`` into buffer rows."""
    from .codex_rollout import _observation_from_call

    out: list[dict[str, Any]] = []
    for msg in parsed.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for block in msg.get("blocks") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            tip = block.get("input") if isinstance(block.get("input"), dict) else {}
            result = block.get("result")
            response: object = ""
            if isinstance(result, dict):
                response = result.get("content") or ""
                if result.get("is_error"):
                    response = f"error: {response}"
            obs = summarize_tool(
                str(name) if name is not None else None,
                tip,
                response,
            )
            # Codex tool names (exec_command, apply_patch, …) are not in
            # _OBSERVED_TOOLS; map them the same way codex_rollout ingest does.
            if obs is None and name is not None:
                # parse_codex_transcript wraps args as {"arguments": ...} /
                # {"input": ...}; unwrap so _observation_from_call sees the
                # same shape parse_rollout feeds it.
                call_args: object = tip
                if isinstance(tip, dict) and "arguments" in tip:
                    call_args = tip.get("arguments")
                obs = _observation_from_call(str(name), call_args)
                if obs is not None and isinstance(response, str) and response:
                    text = response.lower()
                    if "error" in text or "failed" in text:
                        short = str(obs.get("summary", "")).removeprefix("Ran: ")
                        obs = {**obs, "summary": f"Command failed: {short}"}
            if obs is None:
                continue
            record: dict[str, Any] = {
                "ts": 0.0,
                "tool": obs["tool"],
                "summary": mask_secrets(str(obs["summary"])),
            }
            tid = block.get("id")
            if isinstance(tid, str) and tid:
                record["tool_use_id"] = tid
            if obs.get("files"):
                record["files"] = list(obs["files"])
            if obs.get("cmd"):
                record["cmd"] = mask_secrets(str(obs["cmd"]))
            out.append(record)
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


DEFAULT_MAX_SESSION_CHARS = 100_000


def session_history(
    transcript_path: Path,
    *,
    max_question_chars: int = 240,
    max_answer_chars: int = 20000,
    max_session_chars: int = DEFAULT_MAX_SESSION_CHARS,
) -> tuple[list[str], str] | None:
    """Every (question, answer) exchange of a transcript, as one document.

    Pure extraction — no model. Each exchange keeps only the turn's final
    assistant text (the same "the last text row is the answer" rule as
    ``last_exchange``, applied per turn). Returns ``(questions, document)``
    where the document is the answers joined by blank lines; questions stay
    out of the document so a receipt-backed claim can only ever quote
    assistant prose. Over ``max_session_chars`` the oldest exchanges are
    dropped first — the tail of a session is where its conclusions live.
    None when the transcript has no assistant answer at all.
    """
    try:
        fh = transcript_path.open(encoding="utf-8")
    except OSError:
        return None
    exchanges: list[tuple[str, str]] = []
    question: str | None = None
    answer: str | None = None
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
                if answer is not None:
                    exchanges.append((question or "", answer))
                question, answer = user, None
                continue
            text = _assistant_text(obj)
            if text is not None:
                answer = text
    if answer is not None:
        exchanges.append((question or "", answer))
    if not exchanges:
        return None
    kept: list[tuple[str, str]] = []
    total = 0
    for q, a in reversed(exchanges):
        a = a[:max_answer_chars].rstrip()
        if kept and total + len(a) > max_session_chars:
            break
        kept.append((_excerpt(q, max_chars=max_question_chars), a))
        total += len(a)
    kept.reverse()
    questions = [q for q, _ in kept if q]
    document = "\n\n".join(a for _, a in kept)
    return questions, document


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
    enrichment: Enrichment | None = None,
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
    # own words when the transcript offers them, then the enrichment summary
    # (what the session accomplished), else with what changed.
    # The session uuid stays in the body for traceability.
    if first_prompt:
        title = f"session: {_excerpt(first_prompt)}"
    elif enrichment is not None and enrichment.summary:
        title = f"session: {_excerpt(enrichment.summary)}"
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
    if enrichment is not None and enrichment.summary:
        lines += ["## summary", "", enrichment.summary, ""]
    if enrichment is not None and enrichment.subjects:
        lines += ["## subjects", ""]
        for s in enrichment.subjects:
            desc = f": {s.description}" if s.description else ""
            lines.append(f"- **{s.name}** ({s.type}){desc}")
        lines.append("")
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
    origin: Path | None = None,
) -> dict[str, Any]:
    """Roll a session buffer into PENDING summary proposal(s). No approve().

    Claude-Code-facing wrapper: resolves the transcript's first user prompt
    (its one host-specific enricher) and delegates to the host-blind
    `session_split.summarize`. `mode` forwards "auto" | "split" | "mechanical".
    If cwd is None (e.g., finalizing orphaned buffers of unknown origin), git
    changes are not included; transcript_path (from the SessionEnd hook payload)
    supplies the human's first prompt for the summary title when present.

    ``origin`` marks a personal-KB fallback rollup (see ``capture_answer``):
    the filed summary records the folder the session ran in, so a shared
    personal KB's review queue says which folder each summary is about.

    Under ``capture.answer_mode: session`` (the default) this is also where
    answer memory happens: the full transcript is handed to
    ``capture_session_answers`` once, instead of a Stop hook filing claims
    on every turn. A claim-extraction failure never loses the summary.
    """
    from . import session_split  # deferred: breaks the capture<->session_split cycle
    cfg = config or load_config(store)
    intent = (
        first_user_prompt(transcript_path) if transcript_path is not None else None
    )
    # Answers run FIRST so the summary page can cite the session source: an
    # uncited session page is a diary the admission gate auto-rejects, while
    # one citing the receipts-bearing source is reviewable knowledge. A
    # claim-extraction failure still never loses the summary.
    answers: dict[str, Any] | None = None
    sources: list[str] = []
    if transcript_path is not None and cfg.answer_mode == "session":
        try:
            answers = capture_session_answers(
                store, session_id, transcript_path, config=cfg, origin=origin,
            )
        except Exception:
            answers = _answer_skip(session_id, "error")
        source_id = answers.get("source")
        if source_id:
            sources = [str(source_id)]
    result = session_split.summarize(
        store, session_id, intent=intent, cwd=cwd, project=project,
        generated_at=generated_at, mode=mode, config=cfg, origin=origin,
        sources=sources, transcript_path=transcript_path,
    )
    if answers is not None:
        result["answers"] = answers
    updates = result.get("updates")
    if updates:
        result["superseded"] = apply_enrich_updates(store, updates)
    return result


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
    origin: Path | None = None,
) -> dict[str, Any]:
    """Turn a session's latest Q&A into durable, recallable knowledge.

    The ``capture.answer_mode: turn`` path only. Fires from a host Stop hook
    (the turn just finished). Under the default ``session`` mode it defers —
    ``finalize`` extracts once from the full transcript instead
    (``capture_session_answers``) — so a Stop hook can stay wired regardless
    of mode. In turn mode it extracts the last exchange, ingests the *answer*
    as a content-addressed source, files a receipt-backed claim per quotable
    span (``extract.extract_receipt_claims``), and approves each one the
    review gate allows — self-approval clears under ``review.approver_role:
    trusted-agent`` or, for these verbatim-quoting claims,
    ``review.auto_approve_on_receipt`` (the starter-config default). With
    neither gate on the claims stay pending: the review gate is honoured,
    never bypassed.

    Idempotent and quiet by design: an answer already ingested (same bytes) is
    skipped, and answers shorter than ``min_answer_chars`` (acknowledgements)
    are ignored, so a Stop hook firing every turn does not fill the KB with
    noise or duplicates.

    ``origin`` marks a personal-KB *fallback* capture: the session ran in a
    folder with no project KB and ``store`` is the machine's personal
    catch-all. The folder is recorded on the source
    (``metadata.origin_path``, tag ``personal-fallback``) so `vouch adopt`
    can later drain this knowledge into that folder's own KB.
    """
    import os

    from . import extract as extract_mod
    from . import proposals as proposals_mod
    from .storage import ArtifactNotFoundError, sha256_hex

    # vouch's own LLM subprocesses set this so the agent session they spawn does
    # not capture itself back into the KB (mirrors load_config's contract).
    if os.environ.get("VOUCH_CAPTURE_DISABLE") == "1":
        return _answer_skip(session_id, "disabled-env")
    cfg = config or load_config(store)
    if not cfg.enabled:
        return _answer_skip(session_id, "disabled")
    if cfg.answer_mode != "turn":
        # session mode: finalize extracts once from the whole transcript
        # (capture_session_answers). Per-turn extraction would cut claims with
        # single-answer context — the "noted at the end" class of fragment —
        # and no cross-turn dedup or budget.
        return _answer_skip(session_id, "deferred-to-session-end")

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

    tags = ["session-answer"]
    metadata: dict[str, Any] = {"session_id": session_id, "question": question}
    if origin is not None:
        tags.append("personal-fallback")
        metadata["origin_path"] = str(origin)
    source = store.put_source(
        content,
        title=question or f"session {session_id} answer",
        source_type="message",
        tags=tags,
        metadata=metadata,
        # same stamp the extracted claims get at the propose gate: captured
        # knowledge records its project at write time (unretrofittable later)
        scope=proposals_mod.default_scope(store),
    )
    filed = extract_mod.extract_receipt_claims(
        store, source.id, proposed_by=ANSWER_ACTOR, limit=max_claims,
    )
    approved = 0
    for result in filed:
        # approves under the gate, rejects duplicates of durable claims,
        # leaves everything else pending — see resolve_pending_receipt_claim.
        claim = proposals_mod.resolve_pending_receipt_claim(
            store, result.proposal, actor=ANSWER_ACTOR,
            reason="auto-captured session answer (receipt verified)",
        )
        if claim is not None:
            approved += 1
    return {
        "captured": True, "skipped": None, "session_id": session_id,
        "source": source.id, "filed": len(filed), "approved": approved,
    }


def capture_session_answers(
    store: KBStore,
    session_id: str,
    transcript_path: Path,
    *,
    min_answer_chars: int = DEFAULT_MIN_ANSWER_CHARS,
    max_claims: int = DEFAULT_MAX_ANSWER_CLAIMS,
    config: CaptureConfig | None = None,
    origin: Path | None = None,
) -> dict[str, Any]:
    """Turn a whole session's answers into durable, recallable knowledge.

    The session-mode counterpart of ``capture_answer``, run once from
    ``finalize`` (SessionEnd) instead of on every Stop hook. The full
    transcript is the extraction unit: spans are selected with every turn in
    view, identical spans collapse across turns, and ``max_claims`` bounds
    the session rather than each turn. The gate story is unchanged —
    receipt-verified claims self-approve only where ``proposals.approve``
    already allows it (trusted-agent or ``review.auto_approve_on_receipt``),
    everything else stays pending.

    Idempotent the same way ``capture_answer`` is: the assembled history is
    content-addressed, so re-finalizing an unchanged transcript skips.
    ``origin`` marks a personal-KB fallback capture, recorded on the source
    for `vouch adopt`.
    """
    import os

    from . import extract as extract_mod
    from . import proposals as proposals_mod
    from .storage import ArtifactNotFoundError, sha256_hex

    if os.environ.get("VOUCH_CAPTURE_DISABLE") == "1":
        return _answer_skip(session_id, "disabled-env")
    cfg = config or load_config(store)
    if not cfg.enabled:
        return _answer_skip(session_id, "disabled")

    history = session_history(transcript_path)
    if history is None:
        return _answer_skip(session_id, "no-answer")
    questions, document = history
    if len(document) < min_answer_chars:
        return _answer_skip(session_id, "answer-too-short")

    content = document.encode("utf-8")
    sid = sha256_hex(content)
    try:
        store.get_source(sid)
        # The source id is returned even on skip: a re-finalize (e.g. crash
        # between answers and summary) still lets the summary page cite it.
        skip = _answer_skip(session_id, "already-captured")
        skip["source"] = sid
        return skip
    except ArtifactNotFoundError:
        pass

    tags = ["session-answer", "session-history"]
    metadata: dict[str, Any] = {"session_id": session_id, "questions": questions}
    if questions:
        # same key the turn path writes, so downstream readers keep working.
        metadata["question"] = questions[0]
    if origin is not None:
        tags.append("personal-fallback")
        metadata["origin_path"] = str(origin)
    source = store.put_source(
        content,
        title=questions[0] if questions else f"session {session_id} answers",
        source_type="message",
        tags=tags,
        metadata=metadata,
        scope=proposals_mod.default_scope(store),
    )
    filed = extract_mod.extract_receipt_claims(
        store, source.id, proposed_by=ANSWER_ACTOR, limit=max_claims,
    )
    approved = 0
    for result in filed:
        claim = proposals_mod.resolve_pending_receipt_claim(
            store, result.proposal, actor=ANSWER_ACTOR,
            reason="auto-captured session history (receipt verified)",
        )
        if claim is not None:
            approved += 1
    return {
        "captured": True, "skipped": None, "session_id": session_id,
        "source": source.id, "filed": len(filed), "approved": approved,
    }


def apply_enrich_updates(
    store: KBStore, updates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Turn enrichment-detected value changes into supersessions, gate-honouring.

    This is the knowledge-update path: the enrich LLM flags "X changed from
    old to new"; this resolver maps both verbatim values onto durable claims
    and links them with ``lifecycle.supersede``, after which the superseded
    claim leaves every context pack. Precision over recall throughout:

    * runs only under the same self-approval conditions capture's claims use
      (``review.approver_role: trusted-agent`` or
      ``review.auto_approve_on_receipt``) — with the gate closed nothing is
      touched, the human at ``vouch review`` stays the decider;
    * a value must identify exactly ONE approved claim; zero or several
      matches skip the update (a hallucinated value matches nothing);
    * the new claim must be strictly newer than the old one, and distinct.

    Returns one record per attempted update with what happened, for the
    finalize result and tests.
    """
    from . import lifecycle
    from . import proposals as proposals_mod
    from .context import _RETRACTED_CLAIM_STATUSES

    review_cfg = proposals_mod._review_config(store)
    gate_open = review_cfg.get("approver_role") == "trusted-agent" or bool(
        review_cfg.get("auto_approve_on_receipt")
    )
    outcomes: list[dict[str, Any]] = []
    if not gate_open:
        return [
            {**u, "applied": False, "reason": "gate-closed"} for u in updates
        ]
    # Durable-and-live claims only: the same statuses retrieval serves.
    approved = [
        c for c in store.list_claims()
        if c.status not in _RETRACTED_CLAIM_STATUSES
    ]

    def match(value: str) -> Any | None:
        needle = value.lower()
        hits = [c for c in approved if needle in c.text.lower()]
        return hits[0] if len(hits) == 1 else None

    for update in updates[:DEFAULT_MAX_ANSWER_CLAIMS]:
        old_value = str(update.get("old", ""))
        new_value = str(update.get("new", ""))
        record: dict[str, Any] = {**update, "applied": False}
        old_claim = match(old_value)
        new_claim = match(new_value)
        if old_claim is None or new_claim is None:
            record["reason"] = "no-unique-claim-match"
        elif old_claim.id == new_claim.id:
            record["reason"] = "same-claim"
        elif new_claim.created_at <= old_claim.created_at:
            record["reason"] = "new-not-newer"
        else:
            try:
                lifecycle.supersede(
                    store, old_claim_id=old_claim.id,
                    new_claim_id=new_claim.id, actor=CAPTURE_ACTOR,
                )
            except lifecycle.LifecycleError as e:
                record["reason"] = f"lifecycle: {e}"
            else:
                record.update(
                    applied=True, old_claim=old_claim.id, new_claim=new_claim.id,
                )
        outcomes.append(record)
    return outcomes


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


def _drain_approval_backlog(store: KBStore) -> int:
    """Auto-approve whatever the configured gate allows; count it.

    Under ``review.approver_role: trusted-agent`` (the starter-config
    default) this drains every pending proposal — claims, pages, relations —
    left behind while the gate was stricter; under
    ``review.auto_approve_on_receipt`` alone it drains receipt-verified
    claims only. No-op when no gate is open, and never fatal: it runs from a
    SessionStart hook that must not break the session.
    """
    from . import proposals as proposals_mod

    try:
        return len(proposals_mod.auto_approve_pending(store))
    except Exception:
        return 0


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
      - auto_approved: count of pending receipt-verified claims drained on the way
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
            "auto_approved": _drain_approval_backlog(store),
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
        "auto_approved": _drain_approval_backlog(store),
    }
