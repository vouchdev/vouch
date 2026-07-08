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
from pathlib import Path
from typing import Any

import yaml

from . import capture
from .proposals import propose_page
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
                "summary_proposal_ids": [], "mode": "skipped", "skipped": "disabled"}
    if cwd is not None:
        changed_files, git_stat = capture._git_changes(cwd)
    else:
        changed_files, git_stat = [], ""
    total = len(observations) + len(changed_files)
    if total < cfg.min_observations:
        if path.exists():
            path.unlink()
        return {"captured": total, "summary_proposal_id": None,
                "summary_proposal_ids": [], "mode": "skipped", "skipped": "below-min"}

    # Task 4 inserts the LLM split branch here.

    pid = _propose_mechanical(
        store, session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at, intent=intent,
    )
    if path.exists():
        path.unlink()
    return {"captured": total, "summary_proposal_id": pid,
            "summary_proposal_ids": [pid], "mode": "mechanical"}


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
