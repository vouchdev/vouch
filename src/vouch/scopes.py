"""Scoped credentials — kb:read / kb:propose / kb:approve / kb:admin (#608).

A bearer token used to be all-or-nothing: hold it and you could call every
``kb.*`` method, ``kb.approve`` included. That is fine for a solo human and
wrong for anything else — a CI job that should only read, a triage bot that
should only propose, an agent that should never approve its own work.

Withholding ``kb:approve`` *is* the review gate, expressed as a credential.
The config-level ``trusted-agent`` flag can only widen the gate; this is the
first thing in vouch that can narrow it.

Four coarse scopes over the method list, not a per-method allowlist. A
per-method grammar is more flexible and makes every new ``kb.*`` method a
config migration for every deployment; four buckets keep that cost at zero.

* ``kb:read``    — search, context, read_*, list_*, and every other analysis
                   that cannot change durable state.
* ``kb:propose`` — register_source, propose_*, cite, session_*. Everything
                   that files work *into* the review queue.
* ``kb:approve`` — approve/reject and the lifecycle verbs. Deciding what the
                   KB believes.
* ``kb:admin``   — destructive or index-wide maintenance: clear_claims,
                   wipe_dead_refs, index_rebuild, provenance_rebuild.

Two rules make this safe to ship into existing deployments:

**An unscoped credential means all scopes.** Every token issued before this
existed keeps working exactly as it did. Scoping is opt-in, and an empty scope
set is "unrestricted", never "denied" — the alternative would break every
deployment on upgrade.

**Every method must be classified.** ``METHOD_SCOPES`` is exhaustive over
``capabilities.METHODS`` and ``test_every_method_is_classified`` enforces it,
so a newly-added method cannot silently land in no scope (unreachable for
scoped callers) or in all of them (a hole). Unknown methods deny by default —
fails closed, because a deny-list in a trust-centric system fails open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

READ = "kb:read"
PROPOSE = "kb:propose"
APPROVE = "kb:approve"
ADMIN = "kb:admin"

ALL_SCOPES: tuple[str, ...] = (READ, PROPOSE, APPROVE, ADMIN)

# The useful agent default, and the one where the gate holds: an agent may
# read the KB and file proposals, but cannot decide what the KB believes.
DEFAULT_SCOPES: tuple[str, ...] = (READ, PROPOSE)


class ScopeError(ValueError):
    """An unknown or malformed scope."""


# Exhaustive over capabilities.METHODS — pinned by a test.
METHOD_SCOPES: dict[str, str] = {
    # --- kb:read — analysis that cannot change durable state ---------------
    "kb.capabilities": READ,
    "kb.status": READ,
    "kb.stats": READ,
    "kb.activity": READ,
    "kb.digest": READ,
    "kb.search": READ,
    "kb.explain_ranking": READ,
    "kb.neighbors": READ,
    "kb.experts": READ,
    "kb.context": READ,
    "kb.synthesize": READ,
    "kb.read_page": READ,
    "kb.read_claim": READ,
    "kb.read_entity": READ,
    "kb.read_relation": READ,
    "kb.read_evidence": READ,
    "kb.read_source": READ,
    "kb.diff": READ,
    "kb.list_pages": READ,
    "kb.list_claims": READ,
    "kb.list_entities": READ,
    "kb.list_relations": READ,
    "kb.list_sources": READ,
    "kb.list_pending": READ,
    "kb.triage_pending": READ,
    "kb.list_sessions": READ,
    "kb.list_goals": READ,
    "kb.session_transcript": READ,
    "kb.volunteer_context": READ,
    "kb.lint": READ,
    "kb.doctor": READ,
    "kb.export": READ,
    "kb.export_check": READ,
    "kb.import_check": READ,
    "kb.audit": READ,
    "kb.dedup_scan": READ,
    "kb.eval_embeddings": READ,
    "kb.effectiveness": READ,
    "kb.embeddings_stats": READ,
    "kb.why": READ,
    "kb.trace": READ,
    "kb.impact": READ,
    "kb.graph_export": READ,
    "kb.detect_themes": READ,
    "kb.list_skills": READ,
    "kb.get_skill": READ,
    # --- kb:propose — files work into the review queue ---------------------
    "kb.register_source": PROPOSE,
    "kb.register_source_from_path": PROPOSE,
    "kb.propose_claim": PROPOSE,
    "kb.propose_page": PROPOSE,
    "kb.propose_entity": PROPOSE,
    "kb.propose_relation": PROPOSE,
    "kb.propose_delete": PROPOSE,
    "kb.propose_theme": PROPOSE,
    "kb.propose_goal": PROPOSE,
    # capture_correction routes exclusively through propose_quoted_claim and
    # has no import of approve — it files a PENDING claim like any other
    # proposal, so it belongs here and not with the deciding verbs.
    "kb.capture_correction": PROPOSE,
    "kb.cite": PROPOSE,
    "kb.source_verify": PROPOSE,
    "kb.session_start": PROPOSE,
    "kb.session_end": PROPOSE,
    # crystallize/summarize_session/compile all file page proposals and
    # nothing else — they are propose-path, not approve-path.
    "kb.crystallize": PROPOSE,
    "kb.summarize_session": PROPOSE,
    "kb.compile": PROPOSE,
    # --- kb:approve — deciding what the KB believes ------------------------
    "kb.approve": APPROVE,
    "kb.reject": APPROVE,
    "kb.reject_extracted": APPROVE,
    "kb.expire": APPROVE,
    "kb.supersede": APPROVE,
    "kb.contradict": APPROVE,
    "kb.archive": APPROVE,
    "kb.confirm": APPROVE,
    # set_goal_status is a lifecycle op, not a proposal: it mutates an already
    # approved goal in place (life.set_goal_status, beside supersede/archive/
    # confirm) and is documented as "the only write path for goal status" —
    # status moves never go through a second proposal. Filing it under PROPOSE
    # would let a propose-only credential change durable state with no review,
    # which is the boundary this module exists to hold.
    "kb.set_goal_status": APPROVE,
    # --- kb:admin — destructive or index-wide maintenance ------------------
    "kb.clear_claims": ADMIN,
    "kb.wipe_dead_refs": ADMIN,
    "kb.index_rebuild": ADMIN,
    "kb.reindex_embeddings": ADMIN,
    "kb.provenance_rebuild": ADMIN,
}


def parse_scopes(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalise a scope spec. ``None``/empty means unscoped (all scopes).

    Accepts a comma-separated string or any iterable. Raises
    :class:`ScopeError` on an unknown scope rather than dropping it — a typo
    in ``--scopes`` must not silently produce a credential with fewer (or
    more) powers than the operator asked for.
    """
    if raw is None:
        return ()
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        scope = str(item).strip()
        if not scope:
            continue
        if scope not in ALL_SCOPES:
            raise ScopeError(
                f"unknown scope {scope!r}; valid scopes are {', '.join(ALL_SCOPES)}"
            )
        if scope not in out:
            out.append(scope)
    return tuple(out)


def scope_for_method(method: str) -> str | None:
    """The scope a method needs, or ``None`` when it is not classified."""
    return METHOD_SCOPES.get(method)


def permits(granted: Iterable[str] | None, method: str) -> bool:
    """Whether a credential holding ``granted`` may call ``method``.

    Unscoped (empty/None) grants everything — the back-compat rule. A
    classified method needs its scope; an *unclassified* one is denied for a
    scoped caller, so forgetting the table entry costs a scoped agent a method
    rather than silently handing it out.
    """
    scopes = tuple(granted or ())
    if not scopes:
        return True
    required = scope_for_method(method)
    if required is None:
        return False
    return required in scopes


def methods_for(granted: Iterable[str] | None) -> list[str]:
    """Every method a credential holding ``granted`` may call, sorted.

    Backs the ``kb.capabilities`` scope report so an agent can discover what
    it may do up front instead of failing method by method.
    """
    return sorted(m for m in METHOD_SCOPES if permits(granted, m))


def describe(granted: Iterable[str] | None) -> dict[str, object]:
    """The ``kb.capabilities`` scope block for the calling credential."""
    scopes = tuple(granted or ())
    return {
        "available": list(ALL_SCOPES),
        "default": list(DEFAULT_SCOPES),
        "granted": list(scopes),
        "unscoped": not scopes,
        "allowed_methods": methods_for(scopes),
    }
