"""Deterministic cited synthesis for the vouch-context engine.

Turns a ``kb.context`` ContextPack plus salience reflex and hot-memory sidebars
into a single markdown block suitable for OpenClaw's ``systemPromptAddition``
slot. Zero LLM calls — the same posture as gbrain-context's deterministic
temporal injection, but for review-gated KB retrieval (#228).
"""

from __future__ import annotations

import re
from typing import Any

_HEADER = "## Vouch knowledge context"
_SALIENCE_HEADER = "### Entity salience (retrieval reflex)"
_HOT_HEADER = "### Session hot memory"
_QUALITY_HEADER = "### Retrieval quality"
_CITATIONS_HEADER = "### Cited retrieval hits"

# Strip control chars / newlines so external KB text cannot forge prompt directives.
_CTRL_RE = re.compile(r"[\n\r\t\x00-\x1F\x7F]+")


def sanitize_for_prompt(text: str, *, max_len: int = 400) -> str:
    """Flatten and clamp untrusted KB text before prompt injection."""
    cleaned = _CTRL_RE.sub(" ", text or "").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _format_citations(cites: list[str]) -> str:
    if not cites:
        return "uncited"
    return ", ".join(f"`{sanitize_for_prompt(c, max_len=80)}`" for c in cites[:6])


def format_context_item(item: dict[str, Any]) -> str:
    """Render one ContextPack item as a single markdown bullet."""
    kind = sanitize_for_prompt(str(item.get("type", "artifact")), max_len=32)
    artifact_id = sanitize_for_prompt(str(item.get("id", "?")), max_len=120)
    summary = sanitize_for_prompt(str(item.get("summary", "")), max_len=320)
    score = item.get("score")
    backend = sanitize_for_prompt(str(item.get("backend", "unknown")), max_len=32)
    cites = item.get("citations") or []
    if isinstance(score, (int, float)):
        score_bit = f" (score={score:.3f}, {backend})"
    else:
        score_bit = f" ({backend})"
    cite_bit = f" — evidence: {_format_citations(list(cites))}"
    return f"- **[{kind}] `{artifact_id}`**{score_bit}: {summary}{cite_bit}"


def format_salience_section(
    salience: list[dict[str, Any]],
    *,
    store_names: dict[str, str] | None = None,
) -> str:
    """Render ``_meta.vouch_salience`` records as compact entity pointers."""
    if not salience:
        return ""
    names = store_names or {}
    lines = [_SALIENCE_HEADER, ""]
    for rec in salience:
        eid = sanitize_for_prompt(str(rec.get("entity_id", "?")), max_len=120)
        label = names.get(eid, eid)
        count = int(rec.get("claim_count") or 0)
        top = rec.get("top_claim_id")
        top_bit = f", top claim `{sanitize_for_prompt(str(top), max_len=80)}`" if top else ""
        lines.append(
            f"- entity `{eid}` ({sanitize_for_prompt(label, max_len=80)}): "
            f"{count} linked claim(s){top_bit}"
        )
    lines.append("")
    return "\n".join(lines)


def format_hot_memory_section(mem: dict[str, Any]) -> str:
    """Render active session hot-memory state for the model."""
    if not mem:
        return ""
    lines = [_HOT_HEADER, ""]
    query = sanitize_for_prompt(str(mem.get("query", "")), max_len=200)
    if query:
        lines.append(f"- active task query: {query!r}")
    agent = mem.get("agent")
    if agent:
        lines.append(f"- session agent: `{sanitize_for_prompt(str(agent), max_len=64)}`")
    project = mem.get("project")
    if project:
        lines.append(f"- session project: `{sanitize_for_prompt(str(project), max_len=64)}`")
    push_count = mem.get("push_count")
    if isinstance(push_count, int) and push_count:
        lines.append(f"- volunteer pushes this session: {push_count}")
    volunteered = mem.get("volunteered") or []
    if volunteered:
        ids = ", ".join(
            f"`{sanitize_for_prompt(str(v), max_len=80)}`" for v in list(volunteered)[:8]
        )
        lines.append(f"- already volunteered claims: {ids}")
    scores = mem.get("last_scores") or {}
    if scores:
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:5]
        score_line = ", ".join(
            f"`{sanitize_for_prompt(cid, max_len=80)}`={rel:.2f}" for cid, rel in top
        )
        lines.append(f"- last salience snapshot: {score_line}")
    lines.append("")
    return "\n".join(lines)


def format_quality_section(pack: dict[str, Any]) -> str:
    """Surface ContextPack quality metadata when warnings exist."""
    quality = pack.get("quality") or {}
    warnings = pack.get("warnings") or []
    if not warnings and quality.get("ok", True):
        return ""
    lines = [_QUALITY_HEADER, ""]
    if warnings:
        for w in warnings:
            lines.append(f"- warning: {sanitize_for_prompt(str(w), max_len=200)}")
    failed = quality.get("failed") or []
    if failed:
        lines.append(f"- failed gates: {', '.join(str(f) for f in failed)}")
    if quality.get("budget_truncated"):
        omitted = quality.get("budget_omitted_items", 0)
        clipped = quality.get("budget_clipped_items", 0)
        lines.append(
            f"- budget truncated (omitted={omitted}, clipped={clipped})"
        )
    uncited = quality.get("uncited_items") or []
    if uncited:
        lines.append(f"- uncited claims: {len(uncited)}")
    lines.append("")
    return "\n".join(lines)


def synthesize_context_block(
    *,
    pack: dict[str, Any],
    salience: list[dict[str, Any]] | None = None,
    hot_memory: dict[str, Any] | None = None,
    entity_names: dict[str, str] | None = None,
    citations_mode: str | None = None,
) -> str:
    """Weave retrieval, salience, and hot memory into one cited synthesis block."""
    query = sanitize_for_prompt(str(pack.get("query", "")), max_len=240)
    items: list[dict[str, Any]] = list(pack.get("items") or [])
    backend = sanitize_for_prompt(str(pack.get("backend", "none")), max_len=32)
    viewer = pack.get("viewer") or {}

    sections: list[str] = [
        _HEADER,
        "",
        f"Task: {query!r}",
        f"Retrieval backend: `{backend}`",
    ]
    if viewer:
        proj = viewer.get("project")
        agent = viewer.get("agent")
        if proj or agent:
            sections.append(
                "Viewer scope: "
                + ", ".join(
                    bit
                    for bit in (
                        f"project={proj!r}" if proj else "",
                        f"agent={agent!r}" if agent else "",
                    )
                    if bit
                )
            )
    if citations_mode:
        sections.append(f"Citations mode: `{sanitize_for_prompt(citations_mode, max_len=32)}`")
    sections.append("")

    if items:
        sections.append(_CITATIONS_HEADER)
        sections.append("")
        for item in items:
            sections.append(format_context_item(item))
        sections.append("")
    else:
        sections.append("_No matching approved knowledge found for this task._")
        sections.append("")

    salience_block = format_salience_section(salience or [], store_names=entity_names)
    if salience_block:
        sections.append(salience_block)

    hot_block = format_hot_memory_section(hot_memory or {})
    if hot_block:
        sections.append(hot_block)

    quality_block = format_quality_section(pack)
    if quality_block:
        sections.append(quality_block)

    sections.append(
        "_Sources are review-gated YAML claims under `.vouch/`. "
        "Prefer citing claim ids when asserting facts._"
    )
    return "\n".join(sections).strip() + "\n"


def estimate_tokens(*messages: Any, extra_text: str = "") -> int:
    """Rough token estimate: chars/4 heuristic (matches gbrain-context tests)."""
    from .types import AgentMessage

    total = len(extra_text)
    for msg in messages:
        if isinstance(msg, AgentMessage):
            content = msg.content
        elif isinstance(msg, dict):
            content = msg.get("content", "")
        else:
            content = str(msg)
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    total += len(block["text"])
                else:
                    total += len(str(block))
        else:
            total += len(str(content))
    return max(1, total // 4)
