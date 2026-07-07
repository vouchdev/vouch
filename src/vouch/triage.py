"""Advisory triage scoring for the pending-review queue (issue #322).

Read-only. Scores each pending proposal on four signals — fit, citation
quality, duplication risk, and contradiction risk — and folds them into a
composite ``score`` plus an advisory ``recommendation``. The result is
attached as ``_meta.vouch_triage`` on the proposal's own ``model_dump``.

This never decides anything: no call here ever reaches
``proposals.approve``, ``proposals.reject``, ``store.put_*``, or
``store.move_proposal_to_decided``. A human still calls ``kb.approve`` /
``kb.reject``; ``recommendation`` is a hint the reviewer may ignore.

Opt-in: disabled unless ``triage.enabled: true`` is set in
``.vouch/config.yaml`` (mirrors the defensive yaml-read pattern in
``salience.reflex_cfg`` / ``embeddings.similarity.similarity_threshold`` —
no pydantic Config model yet, see issue #243).

Duplication risk reuses the embedding path already built for propose-time
warnings (``embeddings.similarity.find_similar_on_propose``); fit uses the
same underlying primitive (``index_db.search_embedding``) at a lower
threshold band so a near-duplicate hit doesn't also inflate fit and cancel
out its own duplication penalty (see ``_topical_fit_scores``). When no
embedder is registered (base install, no ``[embeddings]`` extra), both
signals fall back to a ``difflib`` text-similarity heuristic so the method
still returns a full block.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

import yaml

from .models import Proposal, ProposalKind, ProposalStatus
from .proposals import _payload_block_reason
from .storage import ArtifactNotFoundError, KBStore

DEFAULT_WEIGHTS: dict[str, float] = {
    "fit": 0.3,
    "citation_quality": 0.3,
    "duplication_risk": 0.2,
    "contradiction_risk": 0.2,
}

_APPROVE_THRESHOLD = 0.7
_REJECT_THRESHOLD = 0.35
_FUZZY_MATCH_FLOOR = 0.3
_CONTRADICTION_CANDIDATE_FLOOR = 0.35

_NEGATION_MARKERS = frozenset({
    "not", "no", "never", "cannot", "isnt", "doesnt", "wont", "wasnt",
    "arent", "dont", "didnt", "hasnt", "havent", "without", "neither", "nor",
})


class TriageError(ValueError):
    """Raised when `kb.triage_pending` is invoked while disabled, or misused."""


@dataclass(frozen=True)
class TriageConfig:
    enabled: bool
    backend: str
    weights: dict[str, float]


def triage_cfg(store: KBStore) -> TriageConfig:
    """Read `triage.*` from config.yaml defensively. Default: disabled."""
    cfg: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        pass

    triage = cfg.get("triage")
    triage = triage if isinstance(triage, dict) else {}

    enabled = triage.get("enabled", False)
    enabled = bool(enabled) if isinstance(enabled, bool) else False

    backend = triage.get("backend", "embeddings")
    backend = backend if isinstance(backend, str) else "embeddings"

    weights = dict(DEFAULT_WEIGHTS)
    weights_cfg = triage.get("weights")
    if isinstance(weights_cfg, dict):
        for key in weights:
            value = weights_cfg.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                weights[key] = float(value)

    return TriageConfig(enabled=enabled, backend=backend, weights=weights)


# --- shared helpers ---------------------------------------------------------


def _referenced_entity_ids(proposal: Proposal) -> list[str]:
    if proposal.kind in (ProposalKind.CLAIM, ProposalKind.PAGE):
        return list(proposal.payload.get("entities") or [])
    return []


def _has_negation(text: str) -> bool:
    tokens = set(re.findall(r"[a-z']+", text.casefold()))
    tokens = {t.replace("'", "") for t in tokens}
    return bool(tokens & _NEGATION_MARKERS)


def _safe_embedder() -> Any | None:
    try:
        from .embeddings import get_embedder

        return get_embedder()
    except Exception:
        return None


def _best_fuzzy_match(
    text: str, pool: list[tuple[str, str]],
) -> tuple[str | None, float]:
    needle = text.casefold()
    best_id: str | None = None
    best_ratio = 0.0
    for cid, candidate in pool:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        ratio = difflib.SequenceMatcher(None, needle, candidate.casefold()).ratio()
        if ratio > best_ratio:
            best_ratio, best_id = ratio, cid
    if best_id is None or best_ratio < _FUZZY_MATCH_FLOOR:
        return None, 0.0
    return best_id, best_ratio


def _claim_text_pool(
    store: KBStore, *, exclude_proposal_id: str, exclude_claim_id: str | None,
) -> list[tuple[str, str]]:
    pool = [
        (c.id, c.text) for c in store.list_claims() if c.id != exclude_claim_id
    ]
    pool += [
        (p.id, str(p.payload.get("text", "")))
        for p in store.list_proposals(ProposalStatus.PENDING)
        if p.kind == ProposalKind.CLAIM and p.id != exclude_proposal_id
    ]
    return pool


def _embedding_hits_for_claim(
    store: KBStore, proposal: Proposal, *, use_embeddings: bool,
) -> list[dict[str, Any]] | None:
    """`find_similar_on_propose` hits, or None when the embedding path can't run.

    None means "no embedder available (or backend forced to heuristic)" —
    callers fall back to a difflib heuristic. `[]` means the embedder ran
    and genuinely found nothing similar. Every hit returned is, by that
    function's own contract, at or above the near-duplicate threshold —
    it's a duplicate detector, not a general similarity search. Used by
    `duplication_risk` and `contradiction_risk`; deliberately NOT reused
    for `fit` (see `_topical_fit_scores`).
    """
    if not use_embeddings or proposal.kind != ProposalKind.CLAIM:
        return None
    text = str(proposal.payload.get("text", "")).strip()
    if not text or _safe_embedder() is None:
        return None
    try:
        from .embeddings.similarity import find_similar_on_propose
    except ImportError:
        return None
    return find_similar_on_propose(
        store, text, exclude_claim_id=proposal.payload.get("id"),
    )


def _topical_fit_scores(store: KBStore, proposal: Proposal, embedder: Any) -> list[float]:
    """Cosine scores against the approved corpus, below the duplicate band.

    A near-duplicate hit is already penalized by `duplication_risk`; letting
    it also inflate `fit` would let the two signals cancel each other out
    for the exact-duplicate case. So this looks at a lower, wider band
    (`min_score=0.3`) and excludes anything at or above the near-duplicate
    threshold (`review.similarity_threshold`, default 0.95).
    """
    text = str(proposal.payload.get("text", "")).strip()
    if not text:
        return []
    try:
        from . import index_db
        from .embeddings.similarity import similarity_threshold

        vec = embedder.encode(text)
        dup_threshold = similarity_threshold(store)
        hits = index_db.search_embedding(
            store.kb_dir, query_vec=vec, kinds=("claim", "page"), limit=5, min_score=0.3,
        )
    except Exception:
        return []
    exclude_id = proposal.payload.get("id")
    return [
        float(cos) for _kind, cid, _snip, cos in hits
        if cid != exclude_id and cos < dup_threshold
    ]


# --- signals -----------------------------------------------------------------


def _signal_citation_quality(store: KBStore, proposal: Proposal) -> dict[str, Any]:
    block = _payload_block_reason(store, proposal)
    if block:
        return {"score": 0.0, "reason": block}
    if proposal.kind in (ProposalKind.CLAIM, ProposalKind.RELATION):
        n = len(proposal.payload.get("evidence") or [])
        if n == 0:
            # Relations may legitimately have no evidence; claims can't reach
            # here with n == 0 (Claim._at_least_one_citation already blocked).
            return {
                "score": 0.6,
                "reason": "relation has no evidence citation (allowed, but weaker)",
            }
        score = min(1.0, 0.7 + 0.15 * (n - 1))
        return {"score": round(score, 4), "reason": f"{n} evidence citation(s) resolve cleanly"}
    if proposal.kind == ProposalKind.PAGE:
        sources = proposal.payload.get("sources") or []
        claims = proposal.payload.get("claims") or []
        if not sources and not claims:
            return {"score": 0.5, "reason": "page has no source/claim citations"}
        return {
            "score": 1.0,
            "reason": f"{len(sources)} source(s), {len(claims)} claim(s) resolve cleanly",
        }
    return {"score": 1.0, "reason": "entity payload resolves cleanly"}


def _signal_fit(
    store: KBStore, proposal: Proposal, embedder: Any | None,
) -> dict[str, Any]:
    entity_ids = _referenced_entity_ids(proposal)
    known = {e.id for e in store.list_entities()}
    overlap: float | None = None
    if entity_ids:
        overlap = sum(1 for e in entity_ids if e in known) / len(entity_ids)

    topical: float | None = None
    if embedder is not None and proposal.kind == ProposalKind.CLAIM:
        scores = _topical_fit_scores(store, proposal, embedder)
        if scores:
            topical = sum(scores) / len(scores)

    parts = [v for v in (overlap, topical) if v is not None]
    if not parts:
        return {
            "score": 0.5,
            "reason": "no referenced entities or approved-corpus signal; neutral fit",
        }
    bits = []
    if overlap is not None:
        bits.append(f"{overlap:.0%} of referenced entities already known")
    if topical is not None:
        bits.append(f"mean topical similarity to approved corpus {topical:.2f}")
    return {"score": round(sum(parts) / len(parts), 4), "reason": "; ".join(bits)}


def _duplication_risk_structural(store: KBStore, proposal: Proposal) -> dict[str, Any]:
    if proposal.kind == ProposalKind.RELATION:
        triple = (
            proposal.payload.get("source"),
            proposal.payload.get("relation"),
            proposal.payload.get("target"),
        )
        for r in store.list_relations():
            if (r.source, r.relation.value, r.target) == triple:
                return {"score": 1.0, "reason": f"identical relation already approved: {r.id}"}
        for p in store.list_proposals(ProposalStatus.PENDING):
            if p.kind != ProposalKind.RELATION or p.id == proposal.id:
                continue
            other = (p.payload.get("source"), p.payload.get("relation"), p.payload.get("target"))
            if other == triple:
                return {"score": 1.0, "reason": f"identical relation already pending: {p.id}"}
        return {"score": 0.0, "reason": "no identical relation found"}

    if proposal.kind == ProposalKind.ENTITY:
        name = str(proposal.payload.get("name", "")).strip()
        pool = [(e.id, e.name) for e in store.list_entities()]
        pool += [
            (p.id, str(p.payload.get("name", "")))
            for p in store.list_proposals(ProposalStatus.PENDING)
            if p.kind == ProposalKind.ENTITY and p.id != proposal.id
        ]
    else:  # PAGE
        name = str(proposal.payload.get("title", "")).strip()
        pool = [(pg.id, pg.title) for pg in store.list_pages()]
        pool += [
            (p.id, str(p.payload.get("title", "")))
            for p in store.list_proposals(ProposalStatus.PENDING)
            if p.kind == ProposalKind.PAGE and p.id != proposal.id
        ]

    if not name:
        return {"score": 0.0, "reason": "no name/title to compare"}
    best_id, best_ratio = _best_fuzzy_match(name, pool)
    if best_id is None:
        return {"score": 0.0, "reason": "no similarly-named artifact found (heuristic backend)"}
    return {
        "score": round(best_ratio, 4),
        "reason": f"name similarity {best_ratio:.2f} vs {best_id} (heuristic backend)",
    }


def _signal_duplication_risk(
    store: KBStore, proposal: Proposal, hits: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if proposal.kind != ProposalKind.CLAIM:
        return _duplication_risk_structural(store, proposal)

    text = str(proposal.payload.get("text", "")).strip()
    if not text:
        return {"score": 0.0, "reason": "no claim text to compare"}

    if hits is None:
        pool = _claim_text_pool(
            store, exclude_proposal_id=proposal.id,
            exclude_claim_id=proposal.payload.get("id"),
        )
        best_id, best_ratio = _best_fuzzy_match(text, pool)
        if best_id is None:
            return {"score": 0.0, "reason": "no near-duplicate claims found (heuristic backend)"}
        return {
            "score": round(best_ratio, 4),
            "reason": f"text similarity {best_ratio:.2f} vs {best_id} (heuristic backend)",
        }

    if not hits:
        return {"score": 0.0, "reason": "no near-duplicate claims found (embedding backend)"}
    top = max(hits, key=lambda w: w["cosine"])
    return {
        "score": round(float(top["cosine"]), 4),
        "reason": (
            f"cosine {top['cosine']:.2f} vs {top['artifact_kind']} "
            f"{top['artifact_id']} (embedding backend)"
        ),
    }


def _signal_contradiction_risk(
    store: KBStore, proposal: Proposal, hits: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if proposal.kind != ProposalKind.CLAIM:
        return {"score": 0.0, "reason": "contradiction risk is only assessed for claim proposals"}

    text = str(proposal.payload.get("text", "")).strip()
    if not text:
        return {"score": 0.0, "reason": "no claim text to compare"}

    entity_ids = set(proposal.payload.get("entities") or [])
    neg = _has_negation(text)

    if hits is not None:
        backend = "embedding"
        candidates = [
            (h["artifact_id"], float(h["cosine"]))
            for h in hits
            if h.get("artifact_kind") == "claim"
        ]
    else:
        backend = "heuristic"
        pool = _claim_text_pool(
            store, exclude_proposal_id=proposal.id,
            exclude_claim_id=proposal.payload.get("id"),
        )
        candidates = [
            (cid, ratio)
            for cid, ratio in (
                (cid, difflib.SequenceMatcher(None, text.casefold(), ctext.casefold()).ratio())
                for cid, ctext in pool
            )
            if ratio >= _CONTRADICTION_CANDIDATE_FLOOR
        ]

    if not candidates:
        return {"score": 0.0, "reason": f"no topically related claims found ({backend} backend)"}

    conflicts: list[tuple[str, float]] = []
    for cid, sim in candidates:
        try:
            claim = store.get_claim(cid)
        except ArtifactNotFoundError:
            continue  # candidate is a pending proposal, not yet an approved claim
        if entity_ids & set(claim.entities) and _has_negation(claim.text) != neg:
            conflicts.append((cid, sim))

    if not conflicts:
        return {
            "score": 0.0,
            "reason": (
                f"{len(candidates)} related claim(s), "
                f"no polarity conflict ({backend} backend)"
            ),
        }
    top_id, top_sim = max(conflicts, key=lambda c: c[1])
    score = round(min(1.0, 0.5 + top_sim / 2), 4)
    return {
        "score": score,
        "reason": (
            f"possible polarity conflict with {top_id} "
            f"(similarity {top_sim:.2f}, {backend} backend)"
        ),
    }


# --- composite ---------------------------------------------------------------


def _composite_score(signals: dict[str, dict[str, Any]], weights: dict[str, float]) -> float:
    goodness = {
        "fit": signals["fit"]["score"],
        "citation_quality": signals["citation_quality"]["score"],
        "duplication_risk": 1.0 - signals["duplication_risk"]["score"],
        "contradiction_risk": 1.0 - signals["contradiction_risk"]["score"],
    }
    total_weight = sum(weights.get(k, 0.0) for k in goodness) or 1.0
    raw = sum(goodness[k] * weights.get(k, 0.0) for k in goodness) / total_weight
    return round(min(1.0, max(0.0, raw)), 4)


def _recommendation(score: float, signals: dict[str, dict[str, Any]]) -> str:
    if signals["citation_quality"]["score"] == 0.0:
        # A blocked payload can't be approved as-is (approve() would raise) —
        # no composite score should be able to override that.
        return "reject"
    if score >= _APPROVE_THRESHOLD:
        return "approve"
    if score <= _REJECT_THRESHOLD:
        return "reject"
    return "needs-human"


def _rationale(recommendation: str, score: float, signals: dict[str, dict[str, Any]]) -> str:
    parts = "; ".join(f"{name}: {sig['reason']}" for name, sig in signals.items())
    return f"{recommendation} (score {score:.2f}) — {parts}"


def score_proposal(
    store: KBStore, proposal: Proposal, *,
    weights: dict[str, float] | None = None,
    use_embeddings: bool = True,
) -> dict[str, Any]:
    """Compute the `_meta.vouch_triage` block for one pending proposal."""
    weights = weights or DEFAULT_WEIGHTS
    hits = _embedding_hits_for_claim(store, proposal, use_embeddings=use_embeddings)
    embedder = _safe_embedder() if use_embeddings else None
    signals = {
        "fit": _signal_fit(store, proposal, embedder),
        "citation_quality": _signal_citation_quality(store, proposal),
        "duplication_risk": _signal_duplication_risk(store, proposal, hits),
        "contradiction_risk": _signal_contradiction_risk(store, proposal, hits),
    }
    score = _composite_score(signals, weights)
    recommendation = _recommendation(score, signals)
    return {
        "recommendation": recommendation,
        "score": score,
        "signals": signals,
        "rationale": _rationale(recommendation, score, signals),
    }


def triage_pending(
    store: KBStore, proposal_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Score pending proposals (default: all of them) — read-only, advisory.

    Raises TriageError if `triage.enabled` isn't `true` in config.yaml.
    """
    cfg = triage_cfg(store)
    if not cfg.enabled:
        raise TriageError(
            "triage is disabled; set triage.enabled: true in .vouch/config.yaml to opt in"
        )
    use_embeddings = cfg.backend != "heuristic"

    if proposal_ids:
        proposals = [store.get_proposal(pid) for pid in proposal_ids]
        proposals = [p for p in proposals if p.status == ProposalStatus.PENDING]
    else:
        proposals = store.list_proposals(ProposalStatus.PENDING)

    out: list[dict[str, Any]] = []
    for p in proposals:
        result = p.model_dump(mode="json")
        result.setdefault("_meta", {})["vouch_triage"] = score_proposal(
            store, p, weights=cfg.weights, use_embeddings=use_embeddings,
        )
        out.append(result)
    return out
