"""LLM narrative summaries for captured sessions — post-session, review-gated.

The mechanical rollup in capture.py records what a session *did*; this module
asks a configurable LLM command to write what the session *meant* — worked on,
decisions, outcomes, open threads — from that record plus an optional
transcript excerpt. Generation runs after the session (SessionEnd hook, or
`vouch summarize` for the backlog), never inside it, so it works for sessions
that end unexpectedly. The narrative only ever lands inside a PENDING
proposal (a session claim, or a page from the pre-claim capture era);
nothing here calls approve() — the review gate stays intact.

The LLM is deployment config (`capture.summary_llm_cmd` in config.yaml), not a
vendored dependency: any shell command that reads the record on stdin and
prints markdown on stdout works (`claude -p`, an api-key script, a local
model). Unset ⇒ every entry point degrades to a no-op and the mechanical
summary ships alone.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import audit
from .capture import is_capture_proposal, proposal_body, set_proposal_body
from .models import ProposalStatus
from .storage import KBStore

SUMMARY_SECTION_HEADER = "## ai summary"
SUMMARY_ACTOR = "vouch-summarize"
VALID_MODES = ("auto", "manual", "off")
DEFAULT_MODE = "auto"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_TRANSCRIPT_CHARS = 40_000
MAX_SUMMARY_CHARS = 6_000

_SECTION_NOTE = (
    "_ai-generated from the session record — verify against the sections "
    "below before approving._"
)

PROMPT_INSTRUCTIONS = """\
you are summarizing a coding-agent session for a human reviewer deciding
whether to keep this summary as durable project memory.

below is the session record: a mechanical activity rollup (user prompts,
files touched, commands run, git changes) and possibly a transcript excerpt.

write a concise markdown summary with exactly these four parts:

- **worked on** — one or two sentences on what the session was about.
- **decisions** — bullet list of durable choices: tools or approaches picked,
  alternatives ruled out, constraints discovered. only decisions the record
  supports; write "none evident" rather than inventing any.
- **outcomes** — what landed (files changed, tests passing) and what failed.
- **open threads** — unfinished work or questions the record shows.

state only what the record supports. lowercase prose. no preamble, no
sections other than the four above.
"""


@dataclass(frozen=True)
class SummaryConfig:
    mode: str = DEFAULT_MODE
    llm_cmd: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS

    @property
    def configured(self) -> bool:
        return self.mode != "off" and bool(self.llm_cmd.strip())


def load_summary_config(store: KBStore) -> SummaryConfig:
    """Read ``capture.summary_*`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
    except (OSError, yaml.YAMLError):
        return SummaryConfig()
    if not isinstance(loaded, dict):
        return SummaryConfig()
    raw = loaded.get("capture")
    if not isinstance(raw, dict):
        return SummaryConfig()
    mode = str(raw.get("summary_mode", DEFAULT_MODE)).strip().lower()
    if mode not in VALID_MODES:
        mode = DEFAULT_MODE
    return SummaryConfig(
        mode=mode,
        llm_cmd=str(raw.get("summary_llm_cmd", "") or ""),
        timeout_seconds=float(
            raw.get("summary_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        ),
        max_transcript_chars=int(
            raw.get("summary_max_transcript_chars", DEFAULT_MAX_TRANSCRIPT_CHARS)
        ),
    )


def read_transcript_excerpt(path: Path | None, max_chars: int) -> str:
    """Extract user/assistant text from a claude-code transcript (jsonl).

    Tool calls, tool results, and non-text content blocks are skipped — the
    prose conversation is the decision-bearing channel and keeps the record
    inside the LLM budget. Returns the tail when the whole run is too long
    (late turns carry the conclusions).
    """
    if path is None:
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    turns: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = [
                str(part.get("text", "")).strip()
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p)
        else:
            continue
        if text:
            turns.append(f"{role}: {text}")
    joined = "\n\n".join(turns)
    if len(joined) <= max_chars:
        return joined
    return "[…transcript truncated…]\n" + joined[-max_chars:]


def build_record(mechanical_body: str, transcript_excerpt: str = "") -> str:
    """Assemble the stdin payload for the LLM command."""
    parts = [PROMPT_INSTRUCTIONS, "# session record", "", mechanical_body.strip()]
    if transcript_excerpt.strip():
        parts += ["", "# transcript excerpt", "", transcript_excerpt.strip()]
    return "\n".join(parts) + "\n"


def generate(record: str, config: SummaryConfig) -> str | None:
    """Run the configured LLM command; return its markdown or None on any failure.

    `llm_cmd` comes from the KB's own config.yaml — the same local trust
    level as a git hook — and runs through the shell so users can configure
    pipelines. Every failure mode (missing binary, nonzero exit, timeout,
    empty output) degrades to None; callers ship the mechanical summary alone.
    """
    if not config.configured:
        return None
    try:
        proc = subprocess.run(
            config.llm_cmd,
            shell=True,
            input=record,
            text=True,
            capture_output=True,
            timeout=config.timeout_seconds,
            # an agent CLI as summarizer (claude -p) fires this repo's own
            # capture hooks; without this the summarize run captures itself
            # and files a junk session per invocation.
            env={**os.environ, "VOUCH_CAPTURE_OFF": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    if len(out) > MAX_SUMMARY_CHARS:
        out = out[:MAX_SUMMARY_CHARS].rstrip() + "\n\n[…truncated…]"
    return out


def strip_summary_section(body: str) -> str:
    """Remove an existing ai-summary section (idempotent re-runs)."""
    start = body.find(SUMMARY_SECTION_HEADER)
    if start == -1:
        return body
    end = body.find("\n## ", start + len(SUMMARY_SECTION_HEADER))
    if end == -1:
        return body[:start].rstrip() + "\n"
    return body[:start] + body[end + 1 :]


def insert_summary_section(body: str, summary: str) -> str:
    """Place the ai summary before the first `## ` section of the page body.

    Reviewers read the narrative first, then the mechanical sections it must
    be verified against. Re-inserting replaces any previous ai summary.
    """
    body = strip_summary_section(body)
    section = f"{SUMMARY_SECTION_HEADER}\n\n{_SECTION_NOTE}\n\n{summary}\n"
    idx = body.find("\n## ")
    if idx == -1:
        return body.rstrip() + "\n\n" + section
    return body[: idx + 1] + section + "\n" + body[idx + 1 :]


def summarize_pending(
    store: KBStore,
    proposal_id: str,
    *,
    config: SummaryConfig | None = None,
    transcript_path: Path | None = None,
) -> dict[str, Any]:
    """Enrich one PENDING captured-session proposal with an LLM narrative.

    Works on both eras of capture proposal — claims (payload ``text``) and
    legacy pages (payload ``body``). Mutates only the pending proposal's
    summary markdown (pre-review scratch, gitignored); the proposal's status
    is never touched.
    """
    cfg = config or load_summary_config(store)
    result: dict[str, Any] = {"proposal_id": proposal_id, "summarized": False}
    if not cfg.configured:
        result["skipped"] = "not-configured"
        return result
    proposal = store.get_proposal(proposal_id)
    if proposal.status != ProposalStatus.PENDING:
        result["skipped"] = "not-pending"
        return result
    if not is_capture_proposal(proposal):
        result["skipped"] = "not-a-captured-session"
        return result
    body = proposal_body(proposal)
    excerpt = read_transcript_excerpt(transcript_path, cfg.max_transcript_chars)
    record = build_record(strip_summary_section(body), excerpt)
    summary = generate(record, cfg)
    if summary is None:
        result["skipped"] = "llm-failed"
        return result
    set_proposal_body(proposal, insert_summary_section(body, summary))
    store.update_proposal(proposal)
    audit.log_event(
        store.kb_dir,
        event="capture.summarize",
        actor=SUMMARY_ACTOR,
        object_ids=[proposal.id],
        data={"mode": cfg.mode},
    )
    result["summarized"] = True
    return result


def find_transcript(session_id: str) -> Path | None:
    """Locate the claude-code transcript for a session id.

    Claude Code stores one JSONL per session under
    ~/.claude/projects/<project-slug>/<session-id>.jsonl; the slug encodes
    the workspace path, so globbing across projects finds the session no
    matter which workspace it ran in.
    """
    safe = session_id.replace("/", "").replace("..", "")
    if not safe:
        return None
    projects = Path.home() / ".claude" / "projects"
    try:
        hits = sorted(projects.glob(f"*/{safe}.jsonl"))
    except OSError:
        return None
    return hits[0] if hits else None


def summarize_by_session(
    store: KBStore,
    session_id: str,
    *,
    config: SummaryConfig | None = None,
) -> dict[str, Any]:
    """Generate the ai summary for one claude-code session, by its id.

    Resolves in order: a still-open capture buffer (finalized first, so a
    freshly closed tab works immediately), then the session's pending
    summary proposal. The transcript is auto-located under ~/.claude/projects/
    and folded into the record when present. Same gate posture as
    everything else here: only the PENDING body changes.
    """
    cfg = config or load_summary_config(store)
    from . import capture as capture_mod

    buffer = capture_mod.buffer_path(store, session_id)
    if buffer.exists():
        capture_mod.finalize(store, session_id, allow_llm=False)
    proposal_id: str | None = None
    for proposal in store.list_proposals(ProposalStatus.PENDING):
        if proposal.session_id == session_id and is_capture_proposal(proposal):
            proposal_id = proposal.id
            break
    if proposal_id is None:
        return {
            "session_id": session_id,
            "summarized": False,
            "skipped": "no-pending-summary-for-session",
        }
    result = summarize_pending(
        store, proposal_id, config=cfg,
        transcript_path=find_transcript(session_id),
    )
    result["session_id"] = session_id
    return result


def summarize_all_pending(
    store: KBStore,
    *,
    config: SummaryConfig | None = None,
    only_missing: bool = True,
) -> dict[str, Any]:
    """Enrich every PENDING captured-session page (the backlog path)."""
    cfg = config or load_summary_config(store)
    summarized: list[str] = []
    skipped: list[dict[str, Any]] = []
    for proposal in store.list_proposals(ProposalStatus.PENDING):
        if not is_capture_proposal(proposal):
            continue
        if only_missing and SUMMARY_SECTION_HEADER in proposal_body(proposal):
            skipped.append({"proposal_id": proposal.id, "skipped": "already-summarized"})
            continue
        one = summarize_pending(store, proposal.id, config=cfg)
        if one["summarized"]:
            summarized.append(proposal.id)
        else:
            skipped.append(one)
    return {"summarized": summarized, "skipped": skipped}
