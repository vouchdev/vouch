"""Claim lifecycle ops: supersede, contradict, archive, cite.

These are *direct* mutations on durable claims — they don't go through the
proposal queue. The rationale: marking a claim as superseded or contradicted
is metadata about reviewed knowledge, not a new assertion. The audit log
captures who did what.

If you want stricter review on lifecycle changes, gate the CLI commands
behind a config flag rather than refactoring this module.

`reconcile_backlinks` at the bottom is the one exception: it's a
read-then-*propose* pass over the relation graph, not a direct mutation —
every gap it finds lands as a pending `Proposal`, same as `propose_relation`
itself, and requires a human `kb.approve` like any other write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml

from . import audit
from .models import Claim, ClaimStatus, Evidence, Proposal, Relation, RelationType
from .proposals import ProposalError, propose_relation
from .storage import ArtifactNotFoundError, KBStore


class LifecycleError(RuntimeError):
    pass


def supersede(
    store: KBStore,
    *,
    old_claim_id: str,
    new_claim_id: str,
    actor: str,
) -> tuple[Claim, Claim]:
    """Mark `old` as superseded by `new`. Both claims must already exist."""
    if old_claim_id == new_claim_id:
        raise LifecycleError("a claim cannot supersede itself")
    old = store.get_claim(old_claim_id)
    new = store.get_claim(new_claim_id)
    rel = Relation(
        id=f"{new.id}--supersedes--{old.id}",
        source=new.id,
        relation=RelationType.SUPERSEDES,
        target=old.id,
    )
    if old.status == ClaimStatus.SUPERSEDED and old.superseded_by == new.id:
        if old.id not in new.supersedes:
            new.supersedes = sorted({*new.supersedes, old.id})
            new.updated_at = datetime.now(UTC)
            store.update_claim(new)
        store.put_relation_idempotent(rel)
        return old, new  # idempotent + convergent retry
    old.status = ClaimStatus.SUPERSEDED
    old.superseded_by = new.id
    old.updated_at = datetime.now(UTC)
    new.supersedes = sorted({*new.supersedes, old.id})
    new.updated_at = datetime.now(UTC)
    # Atomicity: validate both sides before any write so a legacy dangling
    # ref on `new` can't leave `old.superseded_by` written without the
    # reciprocal `new.supersedes` / relation / audit event.
    store._validate_claim_refs(old)
    store._validate_claim_refs(new)
    store.update_claim(old)
    store.update_claim(new)
    # Mirror the supersedes link into the graph for graph-traversal queries.
    store.put_relation_idempotent(rel)
    audit.log_event(
        store.kb_dir, event="claim.supersede", actor=actor,
        object_ids=[old.id, new.id, rel.id],
    )
    return old, new


def contradict(
    store: KBStore,
    *,
    claim_a: str,
    claim_b: str,
    actor: str,
) -> tuple[Claim, Claim, Relation]:
    """Record that two claims contradict each other (symmetric)."""
    a = store.get_claim(claim_a)
    b = store.get_claim(claim_b)
    a.contradicts = sorted({*a.contradicts, b.id})
    b.contradicts = sorted({*b.contradicts, a.id})
    a.status = ClaimStatus.CONTESTED
    b.status = ClaimStatus.CONTESTED
    a.updated_at = b.updated_at = datetime.now(UTC)
    # Atomicity: mirror of supersede — validate both sides before any write.
    store._validate_claim_refs(a)
    store._validate_claim_refs(b)
    store.update_claim(a)
    store.update_claim(b)
    rel = Relation(
        id=f"{a.id}--contradicts--{b.id}",
        source=a.id,
        relation=RelationType.CONTRADICTS,
        target=b.id,
    )
    store.put_relation_idempotent(rel)
    audit.log_event(
        store.kb_dir, event="claim.contradict", actor=actor,
        object_ids=[a.id, b.id, rel.id],
    )
    return a, b, rel


def archive(store: KBStore, *, claim_id: str, actor: str) -> Claim:
    claim = store.get_claim(claim_id)
    claim.status = ClaimStatus.ARCHIVED
    claim.updated_at = datetime.now(UTC)
    store.update_claim(claim)
    audit.log_event(
        store.kb_dir, event="claim.archive", actor=actor, object_ids=[claim.id],
    )
    return claim


def confirm(store: KBStore, *, claim_id: str, actor: str) -> Claim:
    """Re-confirm a stale claim — bumps `last_confirmed_at`."""
    claim = store.get_claim(claim_id)
    claim.last_confirmed_at = datetime.now(UTC)
    claim.updated_at = claim.last_confirmed_at
    if claim.status == ClaimStatus.WORKING:
        claim.status = ClaimStatus.ACTIONABLE
    store.update_claim(claim)
    audit.log_event(
        store.kb_dir, event="claim.confirm", actor=actor, object_ids=[claim.id],
    )
    return claim


def cite(store: KBStore, claim_id: str) -> list[Evidence | dict]:
    """Return resolved citations for a claim.

    Each entry is either an Evidence record (when the citation is an
    Evidence id) or a minimal dict shaped {kind:'source', source_id, title}
    when the citation is a bare Source id.
    """
    claim = store.get_claim(claim_id)
    out: list[Evidence | dict] = []
    for ref in claim.evidence:
        try:
            out.append(store.get_evidence(ref))
            continue
        except ArtifactNotFoundError:
            pass
        try:
            src = store.get_source(ref)
            out.append({
                "kind": "source",
                "source_id": src.id,
                "title": src.title,
                "locator": src.locator,
                "hash": src.hash,
            })
        except ArtifactNotFoundError:
            out.append({"kind": "missing", "ref": ref})
    return out


# --- backlink reconciliation (#307) ----------------------------------------

# Only pairs with an unambiguous natural inverse are mapped by default.
# `owned_by` and the other purely-directed types (uses, supports, caused_by,
# derived_from, implements, references, mentions, supersedes) have no
# corresponding "reverse" RelationType value today, so — per the "unmapped
# types are skipped rather than guessed" rule — they're left out rather than
# invented. A KB can extend or override this via `.vouch/config.yaml`.

# Mirrors extractors/edges.py's AUTO_EXTRACTOR_ACTOR: an automated pass is
# attributed to a fixed bot identity, not whichever human or agent happened
# to invoke it, so proposals it files can be told apart (and, if a bulk
# reject like `reject_auto_extracted` is ever added for these, filtered on).
RECONCILE_ACTOR = "reconcile"

_DEFAULT_BACKLINK_INVERSE_MAP: dict[str, str] = {
    RelationType.DEPENDS_ON.value: RelationType.BLOCKS.value,
    RelationType.BLOCKS.value: RelationType.DEPENDS_ON.value,
    RelationType.SIMILAR_TO.value: RelationType.SIMILAR_TO.value,
    RelationType.RELATES_TO.value: RelationType.RELATES_TO.value,
    RelationType.CONTRADICTS.value: RelationType.CONTRADICTS.value,
}


def _load_backlink_inverse_map(store: KBStore) -> dict[str, str]:
    """Read the relation-type inverse map from config, with defensive defaults.

    Mirrors the `themes._load_theme_config` pattern: every value is
    type-checked and falls back to the default map rather than crashing on
    malformed input. Declared in `.vouch/config.yaml` as:

        backlinks:
          inverse_map:
            depends_on: blocks
            blocks: depends_on

    A relation type absent from the resulting map has no defined mirror and
    is skipped by `reconcile_backlinks`, not guessed.
    """
    try:
        raw = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
        cfg = raw if isinstance(raw, dict) else {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    backlinks_cfg = cfg.get("backlinks") if isinstance(cfg, dict) else None
    if not isinstance(backlinks_cfg, dict):
        return dict(_DEFAULT_BACKLINK_INVERSE_MAP)
    inverse_map = backlinks_cfg.get("inverse_map")
    if not isinstance(inverse_map, dict) or not inverse_map:
        return dict(_DEFAULT_BACKLINK_INVERSE_MAP)
    cleaned = {
        k: v for k, v in inverse_map.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    return cleaned or dict(_DEFAULT_BACKLINK_INVERSE_MAP)


@dataclass
class ReconcileResult:
    """Outcome of `reconcile_backlinks`."""

    checked: int = 0
    proposed: list[Proposal] = field(default_factory=list)
    skipped_unmapped: int = 0
    skipped_existing: int = 0
    dry_run: bool = False


def reconcile_backlinks(
    store: KBStore,
    *,
    rel_types: list[str] | None = None,
    limit: int = 50,
    dry_run: bool = False,
    proposed_by: str = RECONCILE_ACTOR,
) -> ReconcileResult:
    """Propose missing reverse relations across the graph (#307).

    A read-then-propose pass: for every existing `Relation` whose type has
    a configured inverse (`_load_backlink_inverse_map`), checks whether the
    mirror edge (`target --mirror--> source`) already exists and, if not,
    files one `propose_relation` proposal for it — never writes an approved
    edge directly. Relation types absent from the inverse map are skipped
    rather than guessed. `rel_types`, if given, restricts which *original*
    edges are scanned (by their own type, before mirroring). `limit` bounds
    how many proposals a single run files; `dry_run` is threaded straight
    into `propose_relation`, which still validates and returns a real
    `Proposal` but writes nothing to disk.

    Note on the graph read: the issue that requested this (#307) suggested
    reading via `kb.graph_export`. That method renders the unrelated
    *provenance* DAG (claim citations, supersedes, approvedBy — see
    `provenance.py`) as a dot/mermaid string and never touches `Relation`
    objects. The actual full relation edge set is `store.list_relations()`,
    and `store.relations_from()` is the existence check — the same
    underlying data `kb.neighbors` merges in, minus its extra structural
    (non-Relation) edges that would otherwise risk a false "already exists"
    match. This function reads that store surface directly instead.
    """
    inverse_map = _load_backlink_inverse_map(store)
    relations = store.list_relations()
    if rel_types:
        wanted = set(rel_types)
        relations = [r for r in relations if r.relation.value in wanted]

    result = ReconcileResult(dry_run=dry_run)
    seen: set[tuple[str, str, str]] = set()
    for rel in relations:
        if len(result.proposed) >= limit:
            break
        mirror_type = inverse_map.get(rel.relation.value)
        if mirror_type is None:
            result.skipped_unmapped += 1
            continue
        result.checked += 1
        mirror_src, mirror_target = rel.target, rel.source
        key = (mirror_src, mirror_type, mirror_target)
        if key in seen:
            continue
        already_mirrored = any(
            r.target == mirror_target and r.relation.value == mirror_type
            for r in store.relations_from(mirror_src)
        )
        if already_mirrored:
            result.skipped_existing += 1
            continue
        seen.add(key)
        try:
            pr = propose_relation(
                store,
                src=mirror_src,
                relation=mirror_type,
                target=mirror_target,
                proposed_by=proposed_by,
                rationale=f"backlink for {rel.id}",
                dry_run=dry_run,
            )
        except ProposalError:
            continue
        result.proposed.append(pr)
    return result
