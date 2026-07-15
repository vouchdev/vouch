"""LLM review-relevance grading for session transcripts.

The Review console shows a captured session's full dialog; even after the
deterministic noise tagging in ``transcript.py`` hides injected scaffolding,
a long session buries the few exchanges a reviewer actually needs. This
module asks the deployment-configured LLM (``compile.llm_cmd`` — the same
command the wiki compiler and synthesize use) to grade the surviving dialog
messages as ``key`` or ``low``; ungraded messages are implicitly normal.

Grades are presentation metadata only: they attach to messages, never
reorder or remove them, and the raw session file stays the evidence of
record. Every LLM reply is mechanically validated — out-of-range indices,
unknown grades, and non-dialog targets are dropped; an unusable reply
degrades to an error note on the transcript, never an exception to the
caller. Results cache in state.db keyed by the raw file's content hash.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import index_db
from .compile import load_config
from .llm_draft import LLMDraftError, parse_object, run_llm
from .storage import KBStore

# Cap what one grading call sends: enough dialog for judgment, bounded cost.
MAX_EXCERPT_CHARS = 400
MAX_PROMPT_MESSAGES = 400
MAX_NOTE_CHARS = 200

_VALID_GRADES = ("key", "low")

_PROMPT_HEADER = """\
You grade a coding-agent session dialog for a human reviewer who must decide
whether the session's knowledge-base proposals are trustworthy. The reviewer
skims: mark only the messages that change what they should look at.

Grade "key": a message that states the user's actual goal, a decision, a
constraint, a discovered fact, or an outcome the reviewer must see.
Grade "low": a message that carries no review value (acknowledgements,
pleasantries, progress chatter, repetition).
Everything you do not list is treated as normal.

Reply with ONLY a JSON object, no prose:
{"grades": [{"i": <message index>, "grade": "key"|"low", "note": "<=120 chars, only for key"}]}

Dialog (index. role: text):
"""


def grading_available(store: KBStore) -> bool:
    """True when the deployment configured an LLM command to grade with."""
    return load_config(store).llm_cmd is not None


def _dialog_lines(transcript: dict[str, Any]) -> tuple[list[str], list[int]]:
    """Numbered prompt lines for gradable messages + their message indices.

    Gradable = a dialog message with visible (non-noise) text. Tool payloads
    and thinking are excluded — the reviewer's transcript collapses those
    already, and they would dwarf the dialog in the prompt.
    """
    lines: list[str] = []
    indices: list[int] = []
    for i, message in enumerate(transcript.get("messages", [])):
        if message.get("noise"):
            continue
        texts = [
            b.get("text", "")
            for b in message.get("blocks", [])
            if b.get("type") == "text" and not b.get("noise")
        ]
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            continue
        excerpt = text[:MAX_EXCERPT_CHARS]
        lines.append(f"{i}. {message.get('role', '?')}: {excerpt}")
        indices.append(i)
        if len(lines) >= MAX_PROMPT_MESSAGES:
            break
    return lines, indices


def _validate_grades(raw: dict[str, Any], gradable: set[int]) -> dict[str, dict[str, Any]]:
    """Keep only well-formed grades for messages that were actually offered."""
    out: dict[str, dict[str, Any]] = {}
    entries = raw.get("grades")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("i")
        grade = entry.get("grade")
        if not isinstance(idx, int) or idx not in gradable:
            continue
        if grade not in _VALID_GRADES:
            continue
        note = entry.get("note")
        note = note.strip()[:MAX_NOTE_CHARS] if isinstance(note, str) else None
        out[str(idx)] = {"grade": grade, "note": note if grade == "key" else None}
    return out


def _attach(transcript: dict[str, Any], grades: dict[str, Any], *, graded_at: str,
            cached: bool) -> None:
    messages = transcript.get("messages", [])
    for key, value in grades.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict) or value.get("grade") not in _VALID_GRADES:
            continue
        if 0 <= idx < len(messages):
            note = value.get("note")
            messages[idx]["relevance"] = {
                "grade": value["grade"],
                "note": note if isinstance(note, str) else None,
            }
    transcript["grading"] = {
        "graded_at": graded_at,
        "cached": cached,
        "graded_messages": len(grades),
    }


def apply_grades(
    store: KBStore,
    session_id: str,
    transcript: dict[str, Any],
    *,
    raw_path: Path,
    regrade: bool = False,
) -> None:
    """Annotate ``transcript`` in place with cached-or-fresh LLM grades.

    Failure shape: ``transcript["grading"] = {"error": …}`` — the transcript
    itself always renders.
    """
    config = load_config(store)
    if config.llm_cmd is None:
        transcript["grading"] = {"error": "compile.llm_cmd is not configured"}
        return

    try:
        content_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    except OSError as e:
        transcript["grading"] = {"error": f"cannot hash transcript: {e}"}
        return

    if not regrade:
        hit = index_db.get_transcript_grades(store.kb_dir, session_id, content_hash)
        if hit is not None:
            grades = hit["grades"]
            assert isinstance(grades, dict)
            _attach(
                transcript, grades,
                graded_at=str(hit["graded_at"]), cached=True,
            )
            return

    lines, indices = _dialog_lines(transcript)
    if not lines:
        transcript["grading"] = {"error": "no dialog messages to grade"}
        return

    prompt = _PROMPT_HEADER + "\n".join(lines) + "\n"
    try:
        raw = run_llm(
            config.llm_cmd, prompt,
            timeout_seconds=config.timeout_seconds,
            label="compile.llm_cmd",
        )
        parsed = parse_object(raw, noun="transcript grades")
    except LLMDraftError as e:
        transcript["grading"] = {"error": str(e)}
        return

    grades = _validate_grades(parsed, set(indices))
    graded_at = datetime.now(UTC).isoformat()
    index_db.put_transcript_grades(
        store.kb_dir, session_id,
        content_hash=content_hash, grades=grades, graded_at=graded_at,
    )
    _attach(transcript, grades, graded_at=graded_at, cached=False)
