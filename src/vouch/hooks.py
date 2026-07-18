"""Host-hook helpers: translate an agent host's prompt hook into KB context.

Claude Code's UserPromptSubmit hook passes a JSON payload on stdin and injects
whatever the hook prints (as an `additionalContext` envelope) before the model
runs. `build_claude_prompt_hook` turns that payload into a compact, relevant
context block drawn from *approved* KB knowledge — so recall costs the agent
zero tool calls. It never raises: on any problem it returns "" (inject
nothing), so a hook failure can never block the user's turn.

The injected block is instructional, not just informational: when the KB has
relevant approved items the model is told to open its reply with
"From vouch memory:" and ground the answer in the cited items, and when the
KB has nothing relevant it is told to say so — so the user always sees that
vouch was consulted instead of silence that looks like vouch did nothing.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import yaml

from . import salience as salience_mod
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


# --- confidence short-circuit (opt-in) -------------------------------------
#
# Retrieval scores are unbounded (fts/semantic hybrid), so squash them to a
# 0-1 "confidence" so the config knob (retrieval.short_circuit.min_confidence)
# reads the way a user expects: conf = 1 - exp(-score / K). This is a
# heuristic, NOT a calibrated probability — the action gate below, not this
# number, is what keeps the short-circuit safe.
#
# "Do work" prompts must never collapse to a memory pass-through: a high
# retrieval score cannot tell these apart from a lookup — an action prompt
# about a topic the KB knows still scores high — so an action is never
# short-circuited, however confident the match.

_SHORT_CIRCUIT_K = 5.0
_DEFAULT_MIN_CONFIDENCE = 0.8

_ACTION_VERBS = frozenset({
    "implement", "fix", "refactor", "add", "create", "build", "run", "change",
    "update", "edit", "write", "delete", "remove", "make", "install", "deploy",
    "commit", "merge", "rename", "move", "generate", "wire", "patch", "migrate",
    "rewrite", "optimize", "optimise", "debug", "configure", "rebase", "push",
    "revert", "bump", "replace", "integrate", "scaffold", "draft", "compile",
    "set", "apply", "convert", "extract", "split", "rework", "port", "cut",
})


def short_circuit_cfg(cfg: dict) -> tuple[bool, float]:
    """Read ``retrieval.short_circuit`` defensively. Returns (enabled, min_confidence).

    Opt-in (default off). ``min_confidence`` is a 0-1 threshold on the squashed
    top-item confidence; malformed values fall back to the default.
    """
    retrieval = cfg.get("retrieval") if isinstance(cfg, dict) else None
    sc = retrieval.get("short_circuit") if isinstance(retrieval, dict) else None
    if not isinstance(sc, dict):
        return (False, _DEFAULT_MIN_CONFIDENCE)
    enabled = bool(sc.get("enabled", False))
    try:
        threshold = float(sc.get("min_confidence", _DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        threshold = _DEFAULT_MIN_CONFIDENCE
    if not (0.0 <= threshold <= 1.0):
        threshold = _DEFAULT_MIN_CONFIDENCE
    return (enabled, threshold)


def _confidence(score: float) -> float:
    """Squash an unbounded retrieval score into a 0-1 heuristic confidence."""
    if score <= 0:
        return 0.0
    return 1.0 - math.exp(-score / _SHORT_CIRCUIT_K)


def _top_confidence(pack: Any) -> float:
    """Highest item confidence in a context pack (0.0 when empty/malformed)."""
    if not isinstance(pack, dict):
        return 0.0
    best = 0.0
    for item in pack.get("items", []):
        try:
            best = max(best, _confidence(float(item.get("score") or 0)))
        except (TypeError, ValueError):
            continue
    return best


def _looks_like_action(prompt: str) -> bool:
    """True when the prompt's first real word is an imperative 'do work' verb."""
    for tok in prompt.strip().lower().split():
        word = "".join(ch for ch in tok if ch.isalpha())
        if not word:
            continue  # skip leading punctuation / emoji tokens
        return word in _ACTION_VERBS
    return False


def _envelope(block: str) -> str:
    """Wrap an injected context block in the UserPromptSubmit hook envelope."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": block,
            }
        }
    )


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
    session_id = payload.get("session_id")
    session_id = str(session_id) if session_id else None

    # Load config once (defensively): both the salience reflex and the
    # confidence short-circuit read it, and a config failure must never break
    # the hook (module contract — see docstring).
    cfg: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
        cfg = loaded if isinstance(loaded, dict) else {}
    except Exception:
        cfg = {}

    # Feed the entity-salience reflex (#223) so repeated mentions of an
    # entity within a session sharpen ranking on subsequent turns -- this
    # was previously computed but never actually recorded from the hook
    # path, so the reflex sat dormant for claude-code sessions (#425).
    if session_id:
        try:
            _enabled, window, _top_k = salience_mod.reflex_cfg(cfg)
            salience_mod.record_query(session_id, prompt, window=window)
        except Exception:
            # Best-effort reflex feed: recording must never break a
            # working hook (module contract -- see docstring).
            _log.warning("context-hook: salience record_query failed", exc_info=True)
    try:
        pack = build_context_pack(
            store,
            query=prompt,
            limit=_MAX_ITEMS,
            max_chars=_MAX_CHARS,
        )
    except Exception:
        _log.warning("context-hook: build_context_pack failed", exc_info=True)
        return ""
    body = _render(pack) if isinstance(pack, dict) else ""
    if not body:
        # Say so explicitly even when empty — the user wants to see that vouch
        # was consulted, not silence that looks like vouch did nothing.
        return _envelope(
            "[vouch memory] I searched the project's vouch knowledge base for this "
            'prompt and found nothing relevant. Open your reply with "Nothing in '
            'vouch on this." then answer from your own knowledge.'
        )

    sc_enabled, min_conf = short_circuit_cfg(cfg)
    if sc_enabled and not _looks_like_action(prompt) and _top_confidence(pack) >= min_conf:
        # High-confidence lookup: let the model collapse to a near-instant
        # pass-through. The escape hatch ("if they do NOT fully answer") covers
        # the score-is-not-answer gap — a high score means "strongly related",
        # not "definitely the answer". An action prompt never reaches here.
        block = (
            "[vouch memory] A high-confidence match was found in the project's "
            "vouch knowledge base. If the cited item(s) below fully answer the "
            'prompt, reply with ONLY "From vouch memory:" then the relevant '
            "item(s) verbatim and their [ev-...] id(s), and STOP -- no extra "
            "reasoning, caveats, or tool calls. If they do NOT fully answer it, "
            'continue normally: open with "From vouch memory:" and ground in them.'
            "\n\n" + body
        )
    else:
        block = (
            "[vouch memory] I searched the project's vouch knowledge base for this "
            "prompt. Approved, cited items are below. Before you reason on your own: "
            'open your reply with "From vouch memory:" and ground your answer in the '
            "relevant item(s), citing each id in [brackets]. If none are actually "
            'relevant, say "Nothing relevant in vouch on this" and then answer from '
            "your own knowledge.\n\n" + body
        )
    return _envelope(block)
