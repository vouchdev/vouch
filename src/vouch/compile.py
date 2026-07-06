"""Compile approved knowledge into reviewed wiki topic pages.

The llm-wiki ingest pass: hand a deployment-configured LLM command the live
approved claims plus the current page list, receive drafted topic pages as
structured JSON, validate every citation mechanically, and file the survivors
as PENDING page proposals. Never calls ``approve()`` — the review gate stays
intact, and the human decides page by page.

Division of labor mirrors Karpathy's llm-wiki: the LLM drafts and cross-links
the articles; code verifies that every ``[claim: id]`` marker and ``[[link]]``
resolves; the human review is the ingest gate. A draft with an unverifiable
citation is dropped and reported, not repaired — the compiler must not invent
provenance.

The LLM command is deployment config (``compile.llm_cmd`` in
``.vouch/config.yaml``), same pattern as capture/recall: vouch ships no model
dependency and the KB never records which model drafted a page — the audit
trail cares who *approved* it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import audit as audit_mod
from .context import _RETRACTED_CLAIM_STATUSES
from .models import ProposalStatus
from .proposals import ProposalError, _slugify, propose_page
from .storage import ArtifactNotFoundError, KBStore

DEFAULT_MAX_PAGES = 5
DEFAULT_TIMEOUT_SECONDS = 180.0

# The proposer identity for compiled drafts. Deliberately NOT the human
# running the command: the default review gate refuses self-approval, so the
# reviewer who approves a compiled page must be a different actor than the
# proposer. VOUCH_AGENT still wins when set (multi-agent attribution).
COMPILE_ACTOR = "wiki-compiler"

# Raw-material page kinds the compiler must not draft: sessions and logs are
# feedstock for compilation, never its output.
_FORBIDDEN_TYPES = frozenset({"session", "log"})

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_CLAIM_MARKER_RE = re.compile(r"\[claim:\s*([^\]]+)\]")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


class CompileError(Exception):
    """Compile could not run at all (config, LLM, or output-shape failure)."""


@dataclass(frozen=True)
class CompileConfig:
    llm_cmd: str | None = None
    max_pages: int = DEFAULT_MAX_PAGES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _coerce(value: Any, default: Any, cast: Any) -> Any:
    # A config typo (max_pages: five) must degrade to the default, not take
    # down every caller — the web queue reads this config on each render.
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def load_config(store: KBStore) -> CompileConfig:
    """Read ``compile:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return CompileConfig()
    if not isinstance(loaded, dict):
        return CompileConfig()
    raw = loaded.get("compile")
    if not isinstance(raw, dict):
        return CompileConfig()
    cmd = raw.get("llm_cmd")
    return CompileConfig(
        llm_cmd=str(cmd) if cmd else None,
        max_pages=_coerce(
            raw.get("max_pages", DEFAULT_MAX_PAGES), DEFAULT_MAX_PAGES, int,
        ),
        timeout_seconds=_coerce(
            raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS, float,
        ),
    )


@dataclass
class CompileReport:
    proposed: list[dict[str, str]] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "dropped": self.dropped,
            "draft_count": len(self.drafts),
            "dry_run": self.dry_run,
        }


def _pending_page_names(store: KBStore) -> set[str]:
    """Lowercased titles + ids of page proposals already awaiting review."""
    names: set[str] = set()
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind.value != "page":
            continue
        for key in ("title", "id"):
            value = str(prop.payload.get(key) or "").strip().lower()
            if value:
                names.add(value)
    return names


def build_prompt(store: KBStore, *, max_pages: int) -> str:
    """Assemble the self-contained maintainer prompt.

    The whole working set (live claims + page inventory) is inlined rather
    than retrieved: compile is an ingest pass over the KB, and a KB small
    enough to review by hand is small enough to hand to the compiler whole.
    """
    claims = [
        c for c in store.list_claims()
        if c.status not in _RETRACTED_CLAIM_STATUSES
    ]
    if not claims:
        raise CompileError("nothing to compile: the KB has no live approved claims")
    pages = store.list_pages()
    pending = _pending_page_names(store)

    lines = [
        "You are the wiki maintainer for this project's knowledge base. You",
        "compile approved, cited claims into a small set of durable topic",
        "pages (concepts, workflows, decisions) that a future agent or human",
        "reads first. Humans rarely write pages; you do.",
        "",
        "APPROVED CLAIMS (id: text):",
    ]
    for c in claims:
        lines.append(f"- {c.id}: {c.text}")
    lines += ["", "TAKEN TOPICS (existing pages or drafts already awaiting "
                  "review) — do NOT redraft any of these:"]
    taken_lines = [f"- {p.id}: {p.title} [{p.type}]" for p in pages]
    taken_lines += [f"- {name} [pending review]" for name in sorted(pending)]
    lines += taken_lines or ["- (none)"]
    lines += [
        "",
        "RULES",
        f"- Draft at most {max_pages} genuinely NEW topic pages. Skip topics",
        "  already taken; page updates are not supported yet.",
        "- Prefer durable topics over chronology. Never draft a page about a",
        "  session itself; session records are raw material.",
        "- Body: 80-200 words of synthesized markdown prose. After each",
        "  load-bearing statement add an inline citation marker",
        "  [claim: <claim-id>] using ONLY ids from the list above.",
        "- Cross-reference other pages with [[<page title>]] wikilinks, only",
        "  when genuinely related, and only to existing pages or pages in",
        "  this same batch.",
        "- Allowed \"type\" values: concept, workflow, decision, report, index.",
        "",
        "OUTPUT: print ONLY a JSON array, no code fences, no commentary.",
        "Each element: {\"title\": str, \"type\": str, \"body\": str,",
        " \"claims\": [claim-id, ...]}",
    ]
    return "\n".join(lines)


def run_llm(llm_cmd: str, prompt: str, *, timeout_seconds: float) -> str:
    """Run the configured LLM command with the prompt on stdin.

    Runs in a throwaway temp directory: an LLM CLI that discovers per-project
    hooks or MCP servers from its cwd (claude -p does) must not fire this
    project's capture pipeline or connect back to this KB while compiling it.

    Explicit UTF-8 on both pipe directions — the default follows the locale
    (Latin-1 on some hosts, see storage.py), which would crash on the first
    em-dash in a claim or silently mojibake the drafted bodies. ``replace``
    on decode so a stray invalid byte from the LLM surfaces as a visible
    replacement char in review rather than an exception.
    """
    with tempfile.TemporaryDirectory(prefix="vouch-compile-") as tmp:
        try:
            proc = subprocess.run(
                llm_cmd, shell=True, cwd=tmp,
                input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise CompileError(
                f"compile.llm_cmd timed out after {timeout_seconds:.0f}s"
            ) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise CompileError(f"compile.llm_cmd failed ({proc.returncode}): {detail}")
    return proc.stdout


def parse_drafts(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CompileError(f"compiler output is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise CompileError("compiler output must be a JSON array of pages")
    for item in data:
        if not isinstance(item, dict):
            # a list of strings is a common LLM shape failure; surfacing it
            # beats reporting an empty-but-successful compile.
            raise CompileError(
                "compiler output must be a JSON array of page objects, "
                f"got element of type {type(item).__name__}"
            )
    return list(data)


def _draft_problem(
    store: KBStore,
    draft: dict[str, Any],
    *,
    taken_names: set[str],
) -> str | None:
    """Mechanical validation minus wikilinks (those need the surviving batch).

    Returns a drop reason, or None when clean.
    """
    title = str(draft.get("title") or "").strip()
    body = str(draft.get("body") or "").strip()
    page_type = str(draft.get("type") or "").strip().lower()
    if not title:
        return "draft has no title"
    if not body:
        return "draft has no body"
    if page_type in _FORBIDDEN_TYPES:
        return f"type {page_type!r} is raw material, not a topic page"

    # collision guard: approve() routes an existing page id through
    # update_page (the vault-edit path), so a colliding "new" draft would
    # silently overwrite the page on approval. drop it here instead;
    # compiled updates are a future feature, not an accident.
    if title.lower() in taken_names or _slugify(title) in taken_names:
        return f"page for {title!r} already exists or is pending review"

    listed = [str(c) for c in draft.get("claims") or []]
    if not listed:
        return "draft cites no claims"
    live_ids: set[str] = set()
    for cid in listed:
        try:
            claim = store.get_claim(cid)
        except ArtifactNotFoundError:
            return f"unknown claim id: {cid}"
        if claim.status in _RETRACTED_CLAIM_STATUSES:
            return f"claim {cid} is retracted"
        live_ids.add(cid)

    # every inline [claim: …] marker must be backed by a listed, live claim —
    # a body citing a claim the page doesn't link is invented provenance.
    for marker in _CLAIM_MARKER_RE.findall(body):
        cid = marker.strip()
        if cid not in live_ids:
            return f"body cites unlisted claim: {cid}"
    return None


def _first_dangling_link(body: str, known: set[str]) -> str | None:
    for target in _WIKILINK_RE.findall(body):
        name = target.strip()
        if name and name.lower() not in known:
            return name
    return None


def compile_kb(
    store: KBStore,
    *,
    actor: str = COMPILE_ACTOR,
    triggered_by: str | None = None,
    llm_cmd: str | None = None,
    max_pages: int | None = None,
    dry_run: bool = False,
    session_id: str | None = None,
    config: CompileConfig | None = None,
) -> CompileReport:
    """One ingest pass: draft topic pages from live claims, file as proposals.

    ``dry_run`` parses and validates but files nothing. Raises
    :class:`CompileError` when the pass cannot run at all; per-draft failures
    land in ``report.dropped`` instead so one bad draft never sinks the batch.
    ``triggered_by`` is the human (or token label) who initiated the run —
    recorded in the audit log so web-triggered compiles stay attributable.
    """
    cfg = config or load_config(store)
    cmd = llm_cmd or cfg.llm_cmd
    if not cmd:
        raise CompileError(
            "compile.llm_cmd is not configured — set it in .vouch/config.yaml, "
            "e.g.\ncompile:\n  llm_cmd: \"claude -p --model sonnet\""
        )
    cap = max_pages if max_pages is not None else cfg.max_pages
    if cap < 1:
        # a zero/negative cap would drop every draft after spending the LLM
        # run; refuse up front instead of silently producing nothing.
        raise CompileError(f"max_pages must be >= 1, got {cap}")

    prompt = build_prompt(store, max_pages=cap)
    drafts = parse_drafts(run_llm(cmd, prompt, timeout_seconds=cfg.timeout_seconds))

    report = CompileReport(drafts=drafts, dry_run=dry_run)

    existing = store.list_pages()
    taken_names = {p.title.strip().lower() for p in existing}
    taken_names |= {p.id.strip().lower() for p in existing}
    taken_names |= _pending_page_names(store)

    # phase 1: per-draft validation + the cap. cap first-come: a draft past
    # the cap is dropped even if an earlier one falls later, so the outcome
    # doesn't depend on drop order.
    survivors: list[tuple[dict[str, Any], str]] = []
    for i, draft in enumerate(drafts):
        title = str(draft.get("title") or f"draft {i}").strip()
        if len(survivors) >= cap:
            report.dropped.append({"title": title, "reason": f"over max_pages={cap}"})
            continue
        problem = _draft_problem(store, draft, taken_names=taken_names)
        if problem:
            report.dropped.append({"title": title, "reason": problem})
            continue
        survivors.append((draft, title))

    # phase 2: wikilinks resolve against existing pages + the *surviving*
    # batch, to a fixpoint — dropping a draft may dangle a link in another,
    # so iterate until stable. filing a draft whose [[link]] points at a
    # sibling that was just dropped would ship the dangling link the
    # validator exists to prevent.
    known_static = {p.title.strip().lower() for p in existing}
    known_static |= {p.id.strip().lower() for p in existing}
    changed = True
    while changed:
        changed = False
        known = known_static | {t.lower() for _, t in survivors}
        for entry in list(survivors):
            draft, title = entry
            dangling = _first_dangling_link(str(draft.get("body") or ""), known)
            if dangling is not None:
                survivors.remove(entry)
                report.dropped.append({
                    "title": title,
                    "reason": f"unresolved wikilink: [[{dangling}]]",
                })
                changed = True

    for draft, title in survivors:
        if dry_run:
            report.proposed.append({"title": title, "proposal_id": "(dry-run)"})
            continue
        try:
            proposal = propose_page(
                store,
                title=title,
                body=str(draft["body"]).strip(),
                page_type=str(draft.get("type") or "concept").strip().lower(),
                claim_ids=[str(c) for c in draft.get("claims") or []],
                proposed_by=actor,
                tags=["wiki", "compiled"],
                session_id=session_id,
                rationale="compiled from approved claims; every inline "
                          "citation was verified against the store",
            )
        except ProposalError as e:
            report.dropped.append({"title": title, "reason": str(e)})
            continue
        report.proposed.append({
            "title": title,
            "proposal_id": proposal.id,
            "page_id": str(proposal.payload.get("id", "")),
        })

    if not dry_run:
        audit_mod.log_event(
            store.kb_dir,
            event="compile.run",
            actor=triggered_by or actor,
            object_ids=[row["proposal_id"] for row in report.proposed],
            data={
                "proposed": len(report.proposed),
                "dropped": len(report.dropped),
                "proposer": actor,
            },
        )
    return report
