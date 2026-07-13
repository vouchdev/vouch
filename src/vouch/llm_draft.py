"""Shared LLM drafting plumbing for the review-gated compilers.

Both `compile.py` (approved claims -> topic pages) and `session_split.py`
(session observations -> topical session pages) hand a deployment-configured
LLM command a prompt on stdin and parse a JSON array of drafts back. This
module is the shared subprocess + parse layer; each caller keeps its own
domain validation (compile verifies claim citations; session_split forces the
session page type).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from typing import Any

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


class LLMDraftError(Exception):
    """The LLM command could not run, or returned unusable output."""


def run_llm(
    llm_cmd: str,
    prompt: str,
    *,
    timeout_seconds: float,
    label: str = "llm_cmd",
) -> str:
    """Run `llm_cmd` with `prompt` on stdin, in a throwaway temp cwd.

    `label` names the command in error messages so callers keep their own
    config-key wording (e.g. "compile.llm_cmd"). Runs in a temp dir so an LLM
    CLI that discovers per-project hooks/MCP from its cwd does not fire this
    project's own pipeline while summarizing it. UTF-8 is forced on both pipe
    directions — the default follows the locale (Latin-1 on some hosts), which
    would crash on the first em-dash; `errors="replace"` surfaces a stray
    invalid byte as a visible replacement char in review, not an exception.
    """
    with tempfile.TemporaryDirectory(prefix="vouch-llm-") as tmp:
        try:
            proc = subprocess.run(
                llm_cmd, shell=True, cwd=tmp,
                input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMDraftError(f"{label} timed out after {timeout_seconds:.0f}s") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise LLMDraftError(f"{label} failed ({proc.returncode}): {detail}")
    return proc.stdout


def parse_drafts(raw: str, *, noun: str = "page") -> list[dict[str, Any]]:
    """Parse LLM stdout into a list of draft dicts.

    Strips a single markdown code fence if present. `noun` tunes error wording
    ("page" -> "JSON array of pages"). Raises LLMDraftError on any shape
    failure so callers can surface it as a clean, caller-visible message.
    """
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMDraftError(f"compiler output is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise LLMDraftError(f"compiler output must be a JSON array of {noun}s")
    for item in data:
        if not isinstance(item, dict):
            raise LLMDraftError(
                f"compiler output must be a JSON array of {noun} objects, "
                f"got element of type {type(item).__name__}"
            )
    return list(data)
