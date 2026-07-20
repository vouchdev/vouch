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
import re
from typing import Any

import yaml

from . import salience as salience_mod
from .context import build_context_pack
from .storage import KBStore

_log = logging.getLogger("vouch")

_MAX_ITEMS = 8
_MAX_CHARS = 2000

# A "do work" prompt gets a smaller pack: recall should inform the work, not
# crowd the turn it is trying to help.
_WORK_MAX_ITEMS = 3
_WORK_MAX_CHARS = 700

_TOKEN_RE = re.compile(r"\w+")

# Retrieval ORs every query token (see index_db._quote_match), so a prompt
# like "which one is better?" matched claims on "one" and injected noise on
# every conversational turn. Search only on informative tokens; a prompt
# with none is not a retrieval request at all.
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
        # filler interjections — a turn that is only these is not a query
        "hmm", "hm", "huh", "um", "umm", "uh", "ah", "oh", "eh", "wow", "cool",
        "right", "yep", "yup", "nope", "nah", "wait", "hold", "done", "great",
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
    "proceed", "resume", "retry", "finish", "ship", "land", "publish", "tag",
    "continue", "go", "keep", "carry",
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


def prompt_gate_cfg(cfg: dict) -> bool:
    """Read ``retrieval.prompt_gate.enabled`` defensively.

    Absent (the shape every pre-1.6 KB has on disk) means today's behaviour:
    every prompt gets an injected block. The starter config ships it on, so
    fresh KBs are quiet on prompts that are not asking the KB anything.
    """
    retrieval = cfg.get("retrieval") if isinstance(cfg, dict) else None
    gate = retrieval.get("prompt_gate") if isinstance(retrieval, dict) else None
    if not isinstance(gate, dict):
        return False
    return bool(gate.get("enabled", False))


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


# Politeness and discourse lead-ins sit in front of the real imperative
# ("please fix …", "ok now refactor …", "can you add …"). Skipping them is
# what makes the first-word test survive how people actually type.
_LEAD_INS = frozenset({
    "please", "pls", "now", "then", "also", "next", "ok", "okay", "so", "and",
    "hey", "hi", "quick", "quickly", "just", "first", "finally", "lets", "let",
    "can", "could", "would", "will", "you", "we", "i", "im", "id", "maybe",
    "actually", "again", "still", "ahead", "on", "it", "that", "this",
})


def _looks_like_action(prompt: str) -> bool:
    """True when the prompt reads as an imperative 'do work' request.

    Scans past politeness / discourse lead-ins to the first substantive word
    and tests it against the imperative verb list. A question word reached
    that way ("can you tell me what the cadence is") stops the scan: it is a
    lookup wearing an imperative's clothes, and lookups keep full recall.
    """
    saw_lead_in = False
    for tok in prompt.strip().lower().split()[:6]:
        word = "".join(ch for ch in tok if ch.isalpha())
        if not word:
            continue  # skip leading punctuation / emoji tokens
        if word in _ACTION_VERBS:
            return True
        if word in _LEAD_INS:
            saw_lead_in = True
            continue
        return False
    # Nothing but discourse tokens ("continue", "ok now", "go ahead"): a
    # continuation of the work in progress, not a question for the KB.
    return saw_lead_in


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


def _whose_kb(personal: bool) -> str:
    """How the injected block names the KB it just searched."""
    if personal:
        return (
            "your machine-wide personal vouch knowledge base (this folder has "
            "no project KB of its own, so items may come from other folders)"
        )
    return "the project's vouch knowledge base"


def build_claude_prompt_hook(
    store: KBStore, stdin_text: str, *, personal: bool = False
) -> str:
    """Return the stdout a host should inject for this prompt, or "" for none.

    ``personal`` marks a read served by the machine-wide personal catch-all
    KB (this folder has no project KB): the injected text must not call it
    "the project's knowledge base", because the items may have been captured
    while working in a different folder.
    """
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
    # The prompt gate decides how much of a turn vouch is entitled to. Two
    # kinds of prompt are not asking the KB anything:
    #
    #   * chatter with no informative tokens ("ok thanks", "which one?") —
    #     retrieval ORs every token, so these used to match on "one" and
    #     inject noise on turns that wanted none;
    #   * "do work" imperatives ("fix the failing test", "run the suite") —
    #     recall can still HELP these (conventions, architecture), but the
    #     turn belongs to the work, so a miss stays silent and a hit arrives
    #     as advice rather than as a reply contract.
    #
    # Lookups — questions, "what/why/how", anything not imperative — keep the
    # full visible-recall behaviour, banner and opener included.
    gate_on = prompt_gate_cfg(cfg)
    tokens = _informative_tokens(prompt)
    if gate_on and not tokens:
        return ""
    work_mode = gate_on and _looks_like_action(prompt)
    query = " ".join(tokens) if (gate_on and tokens) else prompt
    try:
        pack = build_context_pack(
            store,
            query=query,
            limit=_WORK_MAX_ITEMS if work_mode else _MAX_ITEMS,
            max_chars=_WORK_MAX_CHARS if work_mode else _MAX_CHARS,
        )
    except Exception:
        _log.warning("context-hook: build_context_pack failed", exc_info=True)
        return ""
    body = _render(pack) if isinstance(pack, dict) else ""
    if not body:
        if work_mode:
            # Nothing to offer a work request: say nothing at all rather than
            # spend the turn's opener announcing an empty search.
            return ""
        # Say so explicitly even when empty — the user wants to see that vouch
        # was consulted, not silence that looks like vouch did nothing.
        return _envelope(
            f"[vouch memory] I searched {_whose_kb(personal)} for this "
            "prompt and found nothing relevant. Your final reply MUST open with "
            'the exact words "Nothing in vouch on this." — even if you use tools '
            "or explore the codebase first — then answer from your own knowledge."
        )
    if work_mode:
        # Advisory, not a reply contract: the user asked for work, so the
        # answer must read as the work — no forced opener, no blockquote
        # ritual. Cite only what is actually used.
        return _envelope(
            f"[vouch memory] This is a work request, so I checked "
            f"{_whose_kb(personal)} for background only. Use the items below "
            "if they are relevant to the change; ignore them if they are not. "
            "Do NOT open your reply with a memory banner — answer as you "
            "normally would, and cite an item's [id] inline only where you "
            "actually relied on it.\n\n" + body
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
            "item(s) verbatim, each as a markdown blockquote line ending in its "
            "[ev-...] id, and STOP -- no extra reasoning, caveats, or tool "
            "calls. If they do NOT fully answer it, continue normally: ground "
            'in them, and your final reply MUST still open with "From vouch '
            'memory:" even after tool use.'
            "\n\n" + body
        )
    else:
        # The opener is a hard output contract, not a suggestion: models that
        # explore with tools before answering routinely drop soft "open your
        # reply with" phrasing from their final message (observed in the field
        # on tool-heavy prompts), so state it as a MUST that survives tool use.
        # Likewise the blockquote rule: recalled facts must be visually
        # distinguishable from the model's own words in the rendered reply.
        block = (
            f"[vouch memory] I searched {_whose_kb(personal)} for this "
            "prompt. Approved, cited items are below — check them BEFORE reasoning "
            "or exploring on your own, and ground your answer in the relevant "
            "item(s). Your final reply MUST open with the exact words "
            '"From vouch memory:" — even if you use tools or explore the codebase '
            "first, that opener comes before everything else. Render every fact "
            "you take from memory as its own markdown blockquote line ending in "
            'its id, formatted "> fact — [id]", so recalled knowledge is '
            "visually distinct; anything you add beyond memory goes as normal "
            "prose after the blockquotes. If none of the items are actually "
            'relevant, open with "Nothing relevant in vouch on this" instead and '
            "answer from your own knowledge.\n\n" + body
        )
    return _envelope(block)
