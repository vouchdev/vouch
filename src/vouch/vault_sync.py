"""Bidirectional sync between vouch's KB and an Obsidian-style vault (#181).

Why this exists: ``.vouch/pages/*.md`` is already plain markdown with YAML
frontmatter -- it *is* an Obsidian/Logseq-compatible vault. Today the loop
is one-way: agents propose, humans approve. This module closes the loop so
a human can edit a page in Obsidian and have those edits come back through
the review gate as a normal page-edit proposal.

The vault has a single managed subdirectory:

* ``<vault>/vouch/pages/<id>.md``   - mirrored copies of approved pages
* ``<vault>/vouch/claims/<id>.md``  - stub markdown for each approved claim,
                                      with backlinks to citing pages
* ``<vault>/vouch/.sync-state.json`` - per-mirrored-file content hash so the
                                      next forward sync can tell whether the
                                      user has edited anything

Forward sync (vault -> KB) walks ``<vault>/vouch/pages/``, detects edits by
comparing the on-disk hash against the state file, and for each detected
edit:

1. Registers a vault-origin source via ``store.put_source(...)`` with
   ``locator="vault:<relpath>"``; the bytes are exactly what the user has on
   disk so a reviewer can audit the precise edit.
2. Files a ``page-edit`` proposal via ``proposals.propose_page(...)`` with
   ``proposed_by`` set to the configured actor (default ``vault-sync``).

Backward sync (KB -> vault) lists approved pages and approved claims, writes
the page mirrors verbatim, and renders a small markdown stub per claim with
Obsidian ``[[wikilinks]]`` to citing pages and source ids. After both
directions, the state file is rewritten to the new content hashes so the
next forward pass only flags real user edits.

The watch loop is intentionally a stdlib polling loop (no ``watchdog`` dep):
~2 s intervals are fine for an interactive Obsidian edit-then-review flow,
and zero new runtime deps matches vouch's small-surface convention.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .models import (
    ClaimStatus,
    PageStatus,
)
from .proposals import propose_page
from .storage import (
    ArtifactNotFoundError,
    KBStore,
    _deserialize_page,
    _serialize_page,
)

log = logging.getLogger(__name__)

# The "house" subdirectory inside the user's vault. Everything we read or
# write lives under here -- the user's own notes are untouchable.
VAULT_DIR = "vouch"
STATE_FILENAME = ".sync-state.json"

_VALID_DIRECTIONS: frozenset[str] = frozenset({"forward", "backward", "both"})


class VaultSyncError(RuntimeError):
    """Raised on misconfiguration (bad vault dir, unknown direction)."""


@dataclass
class VaultSyncResult:
    """Outcome of one ``sync_vault`` call, partitioned by what happened.

    All page ids are reported relative -- the caller can format them however.
    """
    pages_mirrored: list[str] = field(default_factory=list)
    claims_mirrored: list[str] = field(default_factory=list)
    pages_proposed: list[str] = field(default_factory=list)
    pages_skipped_unchanged: list[str] = field(default_factory=list)
    pages_skipped_unknown_id: list[str] = field(default_factory=list)
    claim_stubs_edited: list[str] = field(default_factory=list)


# --- state file -----------------------------------------------------------


def _state_path(vault_dir: Path) -> Path:
    return vault_dir / VAULT_DIR / STATE_FILENAME


def _load_state(vault_dir: Path) -> dict[str, str]:
    p = _state_path(vault_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # A corrupted state file shouldn't wedge the sync forever. Log it,
        # treat as empty -- the worst case is one batch of "is this an edit?"
        # false positives that the user can re-approve or reject in the
        # review queue.
        log.warning("vault sync state file %s is unreadable: %s", p, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save_state(vault_dir: Path, state: dict[str, str]) -> None:
    p = _state_path(vault_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- page <-> file helpers -----------------------------------------------


_PAGE_ID_RE = re.compile(r"^id:\s*([\w.-]+)\s*$", re.MULTILINE)


def _page_id_from_frontmatter(text: str) -> str | None:
    """Pull the ``id:`` from a markdown file's frontmatter.

    Uses a regex (not the full YAML parser) so a file the user is mid-editing
    -- with broken YAML below the id line -- still gets its id extracted.
    The full validation happens later when we feed it through
    ``_deserialize_page``.
    """
    m = _PAGE_ID_RE.search(text[:1024])  # frontmatter is at file start
    return m.group(1) if m else None


def _mirror_dir(vault_dir: Path) -> Path:
    return vault_dir / VAULT_DIR / "pages"


def _claims_dir(vault_dir: Path) -> Path:
    return vault_dir / VAULT_DIR / "claims"


# --- backward direction: KB -> vault --------------------------------------


def _approved_pages(store: KBStore) -> Iterable:
    for page in store.list_pages():
        if page.status != PageStatus.DRAFT:
            yield page


def _approved_claims(store: KBStore) -> Iterable:
    for claim in store.list_claims():
        # Working claims have not been through the review gate; archived /
        # superseded / redacted claims are intentionally not surfaced into the
        # vault (Obsidian backlinks would otherwise resurrect dead knowledge).
        if claim.status in {
            ClaimStatus.ACTIONABLE,
            ClaimStatus.STABLE,
            ClaimStatus.CONTESTED,
        }:
            yield claim


def _render_claim_stub(
    claim_id: str,
    claim_text: str,
    status: str,
    citing_pages: list[str],
    sources: list[str],
) -> str:
    """Markdown stub for a single approved claim.

    Obsidian renders ``[[pages/<id>]]`` as a backlink in the graph view; the
    inverse direction (claim -> page) gets rendered as a normal wikilink in
    the body so the user can navigate either way from inside Obsidian.
    """
    lines = [
        "---",
        f"id: {claim_id}",
        f"status: {status}",
        "type: claim",
        "---",
        "",
        f"# {claim_id}",
        "",
        claim_text.rstrip(),
        "",
    ]
    if citing_pages:
        lines.append("## Cited by")
        lines.append("")
        for pid in citing_pages:
            lines.append(f"- [[pages/{pid}]]")
        lines.append("")
    if sources:
        lines.append("## Sources")
        lines.append("")
        for sid in sources:
            # Sources are content-addressed sha256; truncate for human eyes.
            short = sid[:16] + ("…" if len(sid) > 16 else "")
            lines.append(f"- `{short}`")
        lines.append("")
    return "\n".join(lines)


def kb_to_vault(store: KBStore, vault_dir: Path) -> VaultSyncResult:
    """Mirror approved KB artifacts into the vault under ``<vault>/vouch/``.

    Overwrites the mirror each call: the vault subdirectory is vouch's house,
    and only the KB writes there. User edits to mirrored files are picked
    up by :func:`vault_to_kb` on the next forward pass *before* this function
    overwrites them.
    """
    result = VaultSyncResult()
    mirror = _mirror_dir(vault_dir)
    claims_out = _claims_dir(vault_dir)
    mirror.mkdir(parents=True, exist_ok=True)
    claims_out.mkdir(parents=True, exist_ok=True)

    # Build a citing-pages index up front so claim stubs can backlink in O(1).
    citers: dict[str, list[str]] = {}
    for page in _approved_pages(store):
        for cid in page.claims:
            citers.setdefault(cid, []).append(page.id)

    # Pages
    for page in _approved_pages(store):
        text = _serialize_page(page)
        dst = mirror / f"{page.id}.md"
        dst.write_text(text, encoding="utf-8")
        result.pages_mirrored.append(page.id)

    # Claim stubs
    for claim in _approved_claims(store):
        body = _render_claim_stub(
            claim_id=claim.id,
            claim_text=claim.text,
            status=str(claim.status.value if hasattr(claim.status, "value") else claim.status),
            citing_pages=sorted(set(citers.get(claim.id, []))),
            sources=list(claim.evidence),
        )
        dst = claims_out / f"{claim.id}.md"
        dst.write_text(body, encoding="utf-8")
        result.claims_mirrored.append(claim.id)

    # Refresh state file: record the hash of every mirrored file so the next
    # forward pass can detect user edits as "current content != recorded hash".
    new_state: dict[str, str] = {}
    for f in mirror.glob("*.md"):
        rel = f"pages/{f.name}"
        new_state[rel] = _sha256_text(f.read_text(encoding="utf-8"))
    for f in claims_out.glob("*.md"):
        rel = f"claims/{f.name}"
        new_state[rel] = _sha256_text(f.read_text(encoding="utf-8"))
    _save_state(vault_dir, new_state)

    return result


# --- forward direction: vault -> KB ---------------------------------------


def _has_pending_page_proposal(
    store: KBStore, page_id: str, *, body: str | None = None
) -> bool:
    """Return True if a pending proposal already targets ``page_id``.

    When ``body`` is supplied, only returns True if the pending proposal
    also carries the same body — allowing a second different vault edit
    to file a new proposal even while the first is still pending.
    Prevents duplicate proposals when vault_to_kb runs multiple times
    before the reviewer approves the first proposal for a given page edit.
    """
    from .models import ProposalKind, ProposalStatus
    for proposal in store.list_proposals(ProposalStatus.PENDING):
        if proposal.kind != ProposalKind.PAGE:
            continue
        payload = proposal.payload
        if not isinstance(payload, dict) or payload.get("id") != page_id:
            continue
        if body is None or payload.get("body") == body:
            return True
    return False


def vault_to_kb(
    store: KBStore,
    vault_dir: Path,
    *,
    actor: str = "vault-sync",
) -> VaultSyncResult:
    """Scan ``<vault>/vouch/pages/`` for user edits and file proposals."""
    result = VaultSyncResult()
    mirror = _mirror_dir(vault_dir)
    if not mirror.is_dir():
        return result

    state = _load_state(vault_dir)

    for path in sorted(mirror.glob("*.md")):
        rel = f"pages/{path.name}"
        text = path.read_text(encoding="utf-8")
        current_hash = _sha256_text(text)
        recorded = state.get(rel)
        if recorded is None:
            # Never mirrored on this side -- skip silently. We only file
            # proposals for *edits* to KB-managed pages, not arbitrary new
            # files the user dropped into the mirror dir.
            continue
        if current_hash == recorded:
            result.pages_skipped_unchanged.append(rel)
            continue

        page_id = _page_id_from_frontmatter(text)
        if not page_id:
            result.pages_skipped_unknown_id.append(rel)
            continue

        # Validate the edit roundtrips through the Page model -- if the user
        # broke the YAML, surface that as a skip with a log line rather than
        # filing a malformed proposal.
        try:
            edited = _deserialize_page(text)
        except (ValueError, KeyError) as e:
            log.warning(
                "vault sync: vault edit at %s does not deserialise as a Page (%s); "
                "skipping rather than filing a malformed proposal",
                rel, e,
            )
            result.pages_skipped_unknown_id.append(rel)
            continue

        # Fix 1 (#219): guard against proposing edits for pages that no
        # longer exist in the KB. The mirror file may outlive the KB page if
        # the page was deleted after the last backward sync; filing a proposal
        # for a ghost page would produce an unresolvable slug on approve.
        try:
            store.get_page(page_id)
        except ArtifactNotFoundError:
            log.warning(
                "vault sync: mirror file %s references page %r which no longer "
                "exists in the KB; skipping to avoid a ghost proposal",
                rel, page_id,
            )
            result.pages_skipped_unknown_id.append(rel)
            continue

        # Fix 2 (#219): skip if a pending proposal already targets this page
        # id. Without this guard, running vault_to_kb twice before the first
        # proposal is approved files duplicate proposals for the same edit,
        # cluttering the review queue and causing the second approve to fail
        # with "page already exists".
        if _has_pending_page_proposal(store, page_id):
            log.debug(
                "vault sync: pending proposal already exists for page %r; "
                "skipping to avoid duplicate",
                page_id,
            )
            result.pages_skipped_unchanged.append(rel)
            continue

        # Record the user's edit as a vault-origin source so reviewers can
        # see the exact bytes that triggered the proposal. Content-addressed
        # via sha256, so re-runs against the same bytes coalesce on the same
        # source id rather than spawning duplicates.
        source = store.put_source(
            text.encode("utf-8"),
            title=f"Vault edit: {path.name}",
            locator=f"vault:{rel}",
            source_type="file",
            media_type="text/markdown",
            tags=["vault", "vault-sync"],
        )

        # Fix 3 (#219): pass slug_hint=page_id so the proposal targets the
        # existing page rather than a slugified copy of the title. Without
        # this, a page with id "auth-decision-001" and title "Auth Decision"
        # would produce a proposal for a new page "auth-decision", silently
        # duplicating the KB entry on approve instead of updating it.
        propose_page(
            store,
            title=edited.title,
            body=edited.body,
            page_type=str(edited.type.value if hasattr(edited.type, "value") else edited.type),
            claim_ids=list(edited.claims),
            entity_ids=list(edited.entities),
            source_ids=list({*edited.sources, source.id}),
            proposed_by=actor,
            tags=list(edited.tags),
            slug_hint=page_id,
        )
        result.pages_proposed.append(page_id)

        # Update state so we don't re-propose the same edit on the next tick.
        state[rel] = current_hash

    # Fix 4 (#219): walk claims/ and warn on any user edit. Claim stubs are
    # read-only mirrors written by kb_to_vault; edits there are silently
    # dropped without this guard. Warn so the user knows to edit the citing
    # page instead.
    claims_mirror = _claims_dir(vault_dir)
    if claims_mirror.is_dir():
        for path in sorted(claims_mirror.glob("*.md")):
            rel = f"claims/{path.name}"
            recorded = state.get(rel)
            if recorded is None:
                continue
            current_hash = _sha256_text(path.read_text(encoding="utf-8"))
            if current_hash != recorded:
                log.warning(
                    "vault sync: edit detected in claim stub %s; claim stubs "
                    "are read-only mirrors — edit the citing page instead",
                    rel,
                )
                result.claim_stubs_edited.append(rel)

    _save_state(vault_dir, state)
    return result


# --- orchestrator ---------------------------------------------------------


def sync_vault(
    store: KBStore,
    vault_dir: Path,
    *,
    direction: str = "both",
    actor: str = "vault-sync",
) -> VaultSyncResult:
    """Run vault sync; the default direction is bidirectional.

    Order matters: **forward runs first** so user edits land as proposals
    before the backward mirror overwrites them. Once the forward pass has
    captured the edit, the backward pass restores the vault to the KB's
    canonical state for any *unchanged* artifacts (no-op for the edited ones
    until the proposal lands).
    """
    if direction not in _VALID_DIRECTIONS:
        raise VaultSyncError(
            f"unknown direction {direction!r} "
            f"(valid: {', '.join(sorted(_VALID_DIRECTIONS))})"
        )
    if not vault_dir.is_dir():
        raise VaultSyncError(
            f"vault directory {vault_dir} does not exist or is not a directory"
        )

    combined = VaultSyncResult()
    if direction in {"forward", "both"}:
        try:
            r = vault_to_kb(store, vault_dir, actor=actor)
        except ArtifactNotFoundError as e:
            # A vault edit referenced a claim/entity/source that no longer
            # exists in the KB. That's a *real* conflict the user has to
            # resolve in Obsidian, not a vouch bug; surface it cleanly.
            raise VaultSyncError(f"vault edit references unknown artifact: {e}") from e
        combined.pages_proposed.extend(r.pages_proposed)
        combined.pages_skipped_unchanged.extend(r.pages_skipped_unchanged)
        combined.pages_skipped_unknown_id.extend(r.pages_skipped_unknown_id)
    if direction in {"backward", "both"}:
        r = kb_to_vault(store, vault_dir)
        combined.pages_mirrored.extend(r.pages_mirrored)
        combined.claims_mirrored.extend(r.claims_mirrored)
    return combined


def watch_vault(
    store: KBStore,
    vault_dir: Path,
    *,
    direction: str = "both",
    actor: str = "vault-sync",
    poll_interval: float = 2.0,
    iterations: int | None = None,
) -> int:
    """Poll the vault on a fixed interval, syncing each tick.

    Returns the number of ticks executed. ``iterations`` lets tests bound
    the loop; the CLI calls with ``iterations=None`` (forever, exits on
    ``KeyboardInterrupt``).
    """
    if direction not in _VALID_DIRECTIONS:
        raise VaultSyncError(
            f"unknown direction {direction!r} "
            f"(valid: {', '.join(sorted(_VALID_DIRECTIONS))})"
        )
    if not vault_dir.is_dir():
        raise VaultSyncError(
            f"vault directory {vault_dir} does not exist or is not a directory"
        )

    ticks = 0
    try:
        while True:
            sync_vault(store, vault_dir, direction=direction, actor=actor)
            ticks += 1
            if iterations is not None and ticks >= iterations:
                return ticks
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        return ticks
