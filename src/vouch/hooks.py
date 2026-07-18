"""Host-hook helpers: translate an agent host's prompt hook into KB context.

Claude Code's UserPromptSubmit hook passes a JSON payload on stdin and injects
whatever the hook prints (as an `additionalContext` envelope) before the model
runs. `build_claude_prompt_hook` turns that payload into a compact, relevant
context block drawn from *approved* KB knowledge — so recall costs the agent
zero tool calls. It never raises: on any problem it returns "" (inject
nothing), so a hook failure can never block the user's turn.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

from . import salience as salience_mod
from .context import build_context_pack
from .storage import KBStore

_log = logging.getLogger("vouch")

_MAX_ITEMS = 8
_MAX_CHARS = 2000

_TOKEN_RE = re.compile(r"\w+")

# Retrieval ORs every query token (see index_db._quote_match), so a prompt
# like "which one is better?" matched claims on "one" and injected noise on
# every conversational turn. Search only on informative tokens; a prompt
# with none injects nothing at all.
_STOPWORDS = frozenset(
    [
        "a", "an", "the", "this", "that", "these", "those", "there", "here", "some",
        "any", "each", "every", "either", "neither", "both", "few", "more", "most",
        "other", "such", "own", "same", "all", "only", "very", "too", "not", "no",
        "nor", "one", "ones", "two", "first", "second", "also", "even", "still",
        "ever", "never", "always", "already", "i", "me", "my", "mine", "we", "us",
        "our", "ours", "you", "your", "yours", "he", "him", "his", "she", "her",
        "hers", "it", "its", "they", "them", "their", "theirs", "myself", "yourself",
        "ourselves", "themselves", "itself", "am", "is", "are", "was", "were", "be",
        "been", "being", "do", "does", "did", "doing", "done", "have", "has", "had",
        "having", "will", "would", "shall", "should", "can", "could", "may", "might",
        "must", "ought", "and", "or", "but", "so", "yet", "if", "then", "else",
        "because", "while", "until", "although", "when", "where", "why", "how",
        "what", "which", "who", "whom", "whose", "than", "as", "of", "to", "in",
        "on", "at", "by", "for", "with", "about", "against", "between", "into",
        "through", "during", "before", "after", "above", "below", "from", "up",
        "down", "out", "off", "over", "under", "again", "further", "once", "think",
        "thinks", "thinking", "thought", "want", "wants", "wanted", "wanna", "need",
        "needs", "needed", "really", "honestly", "actually", "just", "like", "maybe",
        "perhaps", "please", "okay", "ok", "yes", "yeah", "hey", "hi", "hello",
        "thanks", "thank", "sure", "kind", "kinda", "sort", "sorta", "stuff",
        "thing", "things", "good", "better", "best", "great", "nice", "way", "ways",
        "lot", "lots", "bit", "gonna", "gotta", "lets", "let", "make", "makes",
        "made", "making", "get", "gets", "got", "getting", "go", "goes", "going",
        "gone", "went", "come", "comes", "came", "coming", "know", "knows", "knew",
        "knowing", "see", "sees", "saw", "seen", "look", "looks", "looked",
        "looking", "tell", "tells", "told", "say", "says", "said", "much", "many",
        "little", "anything", "something", "everything", "nothing", "anyone",
        "someone", "everyone",
    ]
)


def _informative_tokens(prompt: str) -> list[str]:
    """Order-preserving informative tokens: no stopwords, digits, one-char."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(prompt.lower()):
        if len(tok) < 2 or tok.isdigit() or tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


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
    # Feed the entity-salience reflex (#223) so repeated mentions of an
    # entity within a session sharpen ranking on subsequent turns -- this
    # was previously computed but never actually recorded from the hook
    # path, so the reflex sat dormant for claude-code sessions (#425).
    if session_id:
        try:
            cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(cfg, dict):
                cfg = {}
            _enabled, window, _top_k = salience_mod.reflex_cfg(cfg)
            salience_mod.record_query(session_id, prompt, window=window)
        except Exception:
            # Best-effort reflex feed: recording must never break a
            # working hook (module contract -- see docstring).
            _log.warning("context-hook: salience record_query failed", exc_info=True)
    tokens = _informative_tokens(prompt)
    if not tokens:
        return ""
    try:
        pack = build_context_pack(
            store,
            query=" ".join(tokens),
            limit=_MAX_ITEMS,
            max_chars=_MAX_CHARS,
        )
    except Exception:
        _log.warning("context-hook: build_context_pack failed", exc_info=True)
        return ""
    body = _render(pack) if isinstance(pack, dict) else ""
    if not body:
        return ""
    block = (
        "Relevant knowledge from the project's vouch KB "
        "(approved & cited — consider it before answering):\n" + body
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": block,
            }
        }
    )
