"""Summarize a session's observation buffer into review-gated pages.

Host-blind: reads only the normalized observation buffer
(`.vouch/captures/<id>.jsonl`) that every host adapter writes via
`capture.observe`, never a host transcript. Small sessions get one mechanical
rollup page (reusing `capture.build_summary_body`); large sessions get an LLM
topical split into several `type: session` pages. Every page is a PENDING
proposal — `approve()` is never called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import audit as audit_mod
from . import capture, llm_draft
from . import compile as compile_mod
from .llm_draft import LLMDraftError
from .models import ProposalStatus
from .proposals import _slugify, propose_page, reject
from .storage import KBStore

logger = logging.getLogger(__name__)

SPLIT_ACTOR = "session-split"

DEFAULT_THRESHOLD_OBSERVATIONS = 40
DEFAULT_MAX_PAGES = 6
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_INPUT_CHARS = 60000


class SplitConfigError(Exception):
    """The split cannot run (no resolvable llm_cmd)."""


@dataclass(frozen=True)
class SplitConfig:
    enabled: bool = True
    llm_cmd: str | None = None
    threshold_observations: int = DEFAULT_THRESHOLD_OBSERVATIONS
    max_pages: int = DEFAULT_MAX_PAGES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS


def _coerce(value: Any, default: Any, cast: Any) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def load_split_config(store: KBStore) -> SplitConfig:
    """Read `capture.split` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return SplitConfig()
    if not isinstance(loaded, dict):
        return SplitConfig()
    cap = loaded.get("capture")
    raw = cap.get("split") if isinstance(cap, dict) else None
    if not isinstance(raw, dict):
        return SplitConfig()
    llm_cmd = raw.get("llm_cmd")
    return SplitConfig(
        enabled=bool(raw.get("enabled", True)),
        llm_cmd=str(llm_cmd) if llm_cmd else None,
        threshold_observations=_coerce(
            raw.get("threshold_observations", DEFAULT_THRESHOLD_OBSERVATIONS),
            DEFAULT_THRESHOLD_OBSERVATIONS, int),
        max_pages=_coerce(raw.get("max_pages", DEFAULT_MAX_PAGES), DEFAULT_MAX_PAGES, int),
        timeout_seconds=_coerce(
            raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS, float),
        max_input_chars=_coerce(
            raw.get("max_input_chars", DEFAULT_MAX_INPUT_CHARS),
            DEFAULT_MAX_INPUT_CHARS, int),
    )


def summarize(
    store: KBStore,
    session_id: str,
    *,
    intent: str | None = None,
    cwd: Path | None = None,
    project: str | None = None,
    generated_at: str | None = None,
    mode: str = "auto",
    config: capture.CaptureConfig | None = None,
) -> dict[str, Any]:
    """Roll a session buffer into PENDING page proposals. Never approves.

    `mode`: "auto" (size gate decides), "split" (force LLM), or "mechanical"
    (force the single rollup). The buffer is deleted only after a page is
    filed (or an explicit below-min skip), so a crash mid-run leaves it intact
    for the next `finalize-all` sweep to retry.
    """
    cfg = config or capture.load_config(store)
    path = capture.buffer_path(store, session_id)
    observations = capture._read_observations(path)
    if not cfg.enabled:
        return {"captured": len(observations), "summary_proposal_id": None,
                "summary_proposal_ids": [], "mode": "skipped", "skipped": "disabled",
                "session_id": session_id, "summarized": False, "proposal_id": None}
    if cwd is not None:
        changed_files, git_stat = capture._git_changes(cwd)
    else:
        changed_files, git_stat = [], ""
    total = len(observations) + len(changed_files)
    if total < cfg.min_observations:
        # The buffer is empty/gone. If a mechanical summary was already filed
        # for this session, the intent (e.g. the review console's Summarize) is
        # to narrate that filed record with the LLM, not to re-read the buffer.
        if mode != "mechanical":
            renarrated = _try_renarrate(store, session_id, split_cfg=load_split_config(store))
            if renarrated is not None:
                if path.exists():
                    path.unlink()
                return renarrated
        if path.exists():
            path.unlink()
        reason = "below-min" if observations else "no-pending-summary-for-session"
        return {"captured": total, "summary_proposal_id": None,
                "summary_proposal_ids": [], "mode": "skipped", "skipped": reason,
                "session_id": session_id, "summarized": False, "proposal_id": None}

    split_cfg = load_split_config(store)
    want_split = mode == "split" or (
        mode == "auto" and split_cfg.enabled and total >= split_cfg.threshold_observations
    )
    if mode != "mechanical" and want_split:
        try:
            ids, dropped, truncated = _propose_split(
                store, session_id, observations, changed_files, git_stat,
                intent=intent, split_cfg=split_cfg,
            )
            if ids:
                if path.exists():
                    path.unlink()
                return {"captured": total, "summary_proposal_id": ids[0],
                        "summary_proposal_ids": ids, "mode": "split",
                        "dropped": dropped, "truncated": truncated,
                        "session_id": session_id, "summarized": True,
                        "proposal_id": ids[0]}
            logger.warning(
                "session_split: no valid drafts for %s; falling back to mechanical",
                session_id,
            )
        except (LLMDraftError, SplitConfigError) as e:
            logger.warning(
                "session_split: llm split failed for %s (%s); falling back", session_id, e
            )

    pid = _propose_mechanical(
        store, session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at, intent=intent,
    )
    if path.exists():
        path.unlink()
    final_mode = "fallback" if (mode != "mechanical" and want_split) else "mechanical"
    result: dict[str, Any] = {
        "captured": total, "summary_proposal_id": pid,
        "summary_proposal_ids": [pid], "mode": final_mode,
        "session_id": session_id, "summarized": final_mode == "mechanical",
        "proposal_id": pid,
    }
    if final_mode == "fallback":
        # the LLM was attempted and fell back; the mechanical page is a backstop,
        # but surface the failure so the UI can prompt a retry / config fix.
        result["skipped"] = "llm-failed"
    return result


def _propose_mechanical(
    store: KBStore,
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    project: str | None,
    generated_at: str | None,
    intent: str | None,
) -> str:
    """File the single mechanical rollup page, exactly as capture did before."""
    title, body = capture.build_summary_body(
        session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at, first_prompt=intent,
    )
    proposal = propose_page(
        store, title=title, body=body,
        page_type=capture.CAPTURE_PAGE_TYPE,
        proposed_by=capture.CAPTURE_ACTOR,
        session_id=session_id,
        rationale="auto-captured session summary",
    )
    return proposal.id


def _propose_split(
    store: KBStore,
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    intent: str | None,
    split_cfg: SplitConfig,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    cmd = split_cfg.llm_cmd or compile_mod.load_config(store).llm_cmd
    if not cmd:
        raise SplitConfigError(
            "capture.split.llm_cmd is not configured (and compile.llm_cmd is unset)"
        )
    prompt, truncated = build_split_prompt(
        store, observations, changed_files, git_stat,
        intent=intent, max_pages=split_cfg.max_pages,
        max_input_chars=split_cfg.max_input_chars,
    )
    raw = llm_draft.run_llm(
        cmd, prompt, timeout_seconds=split_cfg.timeout_seconds,
        label="capture.split.llm_cmd",
    )
    drafts = llm_draft.parse_drafts(raw, noun="page")
    ids, dropped = _file_drafts(store, session_id, drafts, split_cfg.max_pages)
    _audit_split(store, session_id, ids, dropped, len(observations), truncated)
    return ids, dropped, truncated


def _render_obs(obs: dict[str, Any]) -> str:
    tool = str(obs.get("tool", "")).strip()
    summary = str(obs.get("summary", "")).strip()
    files = obs.get("files") or []
    line = f"[{tool}] {summary}" if tool else summary
    if files:
        line += f" (files: {', '.join(str(f) for f in files[:5])})"
    return line


def build_split_prompt(
    store: KBStore,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    intent: str | None,
    max_pages: int,
    max_input_chars: int,
) -> tuple[str, bool]:
    """Assemble the host-neutral topical-split prompt. Returns (prompt, truncated).

    `tool` labels are opaque — the model clusters on the `summary` prose, so any
    host's tool vocabulary works. If the rendered activity exceeds
    `max_input_chars`, keep the most-recent observations that fit and prepend an
    explicit elision note (no silent cap).
    """
    obs_lines = [_render_obs(o) for o in observations]
    truncated = False
    if sum(len(x) + 1 for x in obs_lines) > max_input_chars:
        truncated = True
        kept: list[str] = []
        size = 0
        for line in reversed(obs_lines):
            if size + len(line) + 1 > max_input_chars:
                break
            kept.append(line)
            size += len(line) + 1
        obs_lines = list(reversed(kept))
        elided = len(observations) - len(obs_lines)
        obs_lines.insert(0, f"(... {elided} older observations elided ...)")

    lines: list[str] = [
        "You are the session historian for this project's knowledge base. You",
        "summarize one work session into a small set of durable, human-readable",
        "session records — one per distinct thread of work.",
        "",
    ]
    if intent:
        lines += ["SESSION INTENT:", f"  {intent}", ""]
    lines += ["SESSION ACTIVITY (one line per observation, oldest first):"]
    lines += [f"- {line}" for line in obs_lines]
    lines += [""]
    if changed_files:
        lines += ["FILES CHANGED:"]
        lines += [f"- {f}" for f in changed_files[:50]]
        lines += [""]
    if git_stat:
        lines += ["GIT STAT:", "```", git_stat, "```", ""]

    pages = store.list_pages()
    pending = compile_mod._pending_page_names(store)
    taken = [f"- {p.title}" for p in pages] + [f"- {n} [pending]" for n in sorted(pending)]
    lines += ["TAKEN TOPICS (do NOT redraft any of these):"]
    lines += taken or ["- (none)"]
    lines += ["", *_rules_lines(max_pages)]
    return "\n".join(lines), truncated


def _rules_lines(max_pages: int) -> list[str]:
    """The shared clustering rules + JSON output contract for both the
    buffer split and the re-narrate-from-record prompts."""
    return [
        "RULES",
        f"- Cluster the activity into at most {max_pages} coherent TOPICS —",
        "  distinct threads of work in this session. Draft one page per topic.",
        "- Each page needs a specific title (\"fixed the audit-log write race\",",
        "  not \"bug fixes\") and an 80-200 word markdown body summarizing that",
        "  thread of work.",
        "- These are session records, NOT wiki topic pages: do NOT add",
        "  [claim: id] markers, and do NOT invent facts beyond the activity shown.",
        "- Skip any topic already listed under TAKEN TOPICS.",
        "",
        "OUTPUT: print ONLY a JSON array, no code fences, no commentary.",
        "Each element: {\"title\": str, \"body\": str}",
    ]


def _skip(session_id: str, reason: str, *, proposal_id: str | None = None) -> dict[str, Any]:
    return {"captured": 0, "summary_proposal_id": proposal_id, "summary_proposal_ids": [],
            "mode": "skipped", "skipped": reason, "session_id": session_id,
            "summarized": False, "proposal_id": proposal_id}


def _eligible_mechanical_proposal(store: KBStore, session_id: str) -> Any | None:
    """The pending, not-yet-narrated session summary for `session_id`, if any.

    Only mechanical rollups (proposed by vouch-capture) are eligible; a
    session-split proposal is already narrated.
    """
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind.value != "page":
            continue
        if str(prop.payload.get("type") or "") != capture.CAPTURE_PAGE_TYPE:
            continue
        if prop.session_id != session_id:
            continue
        if prop.proposed_by != capture.CAPTURE_ACTOR:
            continue
        return prop
    return None


def build_renarrate_prompt(store: KBStore, body: str, *, title: str, max_pages: int) -> str:
    """Prompt to narrate an already-filed mechanical session record into
    topical pages. Source is the filed proposal's markdown body (the buffer is
    gone by the time a filed summary is re-narrated)."""
    lines = [
        "You are the session historian for this project's knowledge base. You are",
        "given a mechanically-generated session record. Rewrite it into coherent,",
        "narrated topical pages — one per distinct thread of work.",
        "",
    ]
    if title:
        lines += [f"SESSION RECORD TITLE: {title}", ""]
    lines += ["SESSION RECORD (markdown):", body, ""]
    pages = store.list_pages()
    pending = compile_mod._pending_page_names(store)
    taken = [f"- {p.title}" for p in pages] + [f"- {n} [pending]" for n in sorted(pending)]
    lines += ["TAKEN TOPICS (do NOT redraft any of these):"]
    lines += taken or ["- (none)"]
    lines += ["", *_rules_lines(max_pages)]
    return "\n".join(lines)


def _try_renarrate(
    store: KBStore, session_id: str, *, split_cfg: SplitConfig
) -> dict[str, Any] | None:
    """Narrate a filed mechanical summary with the LLM, superseding it.

    Returns None when no eligible mechanical proposal exists (caller falls
    through to the below-min skip). On success files narrated page proposals
    and rejects the mechanical one. On LLM failure leaves it intact.
    """
    prop = _eligible_mechanical_proposal(store, session_id)
    if prop is None:
        return None
    cmd = split_cfg.llm_cmd or compile_mod.load_config(store).llm_cmd
    if not cmd:
        return _skip(session_id, "not-configured", proposal_id=prop.id)
    body = str(prop.payload.get("body") or "")
    title = str(prop.payload.get("title") or "")
    try:
        prompt = build_renarrate_prompt(store, body, title=title, max_pages=split_cfg.max_pages)
        raw = llm_draft.run_llm(
            cmd, prompt, timeout_seconds=split_cfg.timeout_seconds,
            label="capture.split.llm_cmd",
        )
        drafts = llm_draft.parse_drafts(raw, noun="page")
        ids, dropped = _file_drafts(store, session_id, drafts, split_cfg.max_pages)
    except LLMDraftError as e:
        logger.warning("session_split: renarrate failed for %s (%s)", session_id, e)
        return _skip(session_id, "llm-failed", proposal_id=prop.id)
    if not ids:
        logger.warning("session_split: renarrate produced no valid drafts for %s", session_id)
        return _skip(session_id, "llm-failed", proposal_id=prop.id)
    reject(
        store, prop.id, rejected_by=SPLIT_ACTOR,
        reason="superseded by llm narrative summary",
    )
    _audit_split(store, session_id, ids, dropped, 0, False)
    return {"captured": 0, "summary_proposal_id": ids[0], "summary_proposal_ids": ids,
            "mode": "renarrated", "dropped": dropped, "truncated": False,
            "session_id": session_id, "summarized": True, "proposal_id": ids[0],
            "superseded": prop.id}


def _file_drafts(
    store: KBStore,
    session_id: str,
    drafts: list[dict[str, Any]],
    max_pages: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    existing = store.list_pages()
    taken = {p.title.strip().lower() for p in existing}
    taken |= {p.id.strip().lower() for p in existing}
    taken |= compile_mod._pending_page_names(store)
    ids: list[str] = []
    dropped: list[dict[str, Any]] = []
    for i, draft in enumerate(drafts):
        title = str(draft.get("title") or "").strip()
        body = str(draft.get("body") or "").strip()
        if not title:
            dropped.append({"title": f"draft {i}", "reason": "draft has no title"})
            continue
        if not body:
            dropped.append({"title": title, "reason": "draft has no body"})
            continue
        if len(ids) >= max_pages:
            dropped.append({"title": title, "reason": f"over max_pages={max_pages}"})
            continue
        if title.lower() in taken or _slugify(title) in taken:
            dropped.append({"title": title, "reason": "title already exists or is pending"})
            continue
        proposal = propose_page(
            store, title=title, body=body,
            page_type=capture.CAPTURE_PAGE_TYPE,  # "session" — forced, ignore any LLM type
            proposed_by=SPLIT_ACTOR,
            tags=["session", "split"],
            session_id=session_id,
            metadata={"session_id": session_id},
            rationale=f"llm topical split of session {session_id}",
        )
        ids.append(proposal.id)
        taken.add(title.lower())
        taken.add(_slugify(title))
    return ids, dropped


def _audit_split(
    store: KBStore,
    session_id: str,
    ids: list[str],
    dropped: list[dict[str, Any]],
    n_observations: int,
    truncated: bool,
) -> None:
    audit_mod.log_event(
        store.kb_dir, event="session.split", actor=SPLIT_ACTOR,
        object_ids=ids,
        data={"proposed": len(ids), "dropped": len(dropped),
              "observations": n_observations, "truncated": truncated},
    )


def build_session_rows(store: KBStore) -> list[dict[str, Any]]:
    """Assemble the session-review pipeline view (`kb.list_sessions`).

    Two stages, host-blind (reads the same buffers + proposals every adapter's
    capture feeds):

    - "pending": a filed session-summary page proposal awaiting review.
      `summarized=True` — it already has a summary (mechanical or LLM-split).
    - "buffer": an open capture buffer that has not been summarized yet.
      `summarized=False` — it still needs a summary.

    A session with a filed proposal is not also listed as a buffer (finalize
    deletes the buffer, but guard against a race). Newest activity first.
    """
    rows: list[dict[str, Any]] = []
    pending_session_ids: set[str] = set()

    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind.value != "page":
            continue
        if str(prop.payload.get("type") or "") != capture.CAPTURE_PAGE_TYPE:
            continue
        if prop.session_id:
            pending_session_ids.add(prop.session_id)
        # a mechanical rollup (vouch-capture) still needs an LLM narrative;
        # only a session-split proposal counts as already summarized.
        tags = prop.payload.get("tags") or []
        summarized = prop.proposed_by == SPLIT_ACTOR or "split" in tags
        rows.append({
            "session_id": prop.session_id,
            "stage": "pending",
            "proposal_id": prop.id,
            "kind": "page",
            "title": prop.payload.get("title"),
            "summarized": summarized,
            "observations": None,
            "last_activity": prop.proposed_at.isoformat(),
        })

    caps = capture.captures_dir(store)
    if caps.exists():
        for path in sorted(caps.glob("*.jsonl")):
            sid = path.stem
            if sid in pending_session_ids:
                continue
            obs = capture._read_observations(path)
            ts_vals = [float(o["ts"]) for o in obs if o.get("ts") is not None]
            last = (
                datetime.fromtimestamp(max(ts_vals), tz=UTC).isoformat()
                if ts_vals else None
            )
            rows.append({
                "session_id": sid,
                "stage": "buffer",
                "proposal_id": None,
                "kind": None,
                "title": None,
                "summarized": False,
                "observations": len(obs),
                "last_activity": last,
            })

    rows.sort(key=lambda r: r["last_activity"] or "", reverse=True)
    return rows
