"""Viewer-context scoping for retrieval and audit reads (VEP-0005).

Retrieval surfaces treat scope as a relevance filter. The audit read path
also applies scope so multi-project KBs do not leak events across project
boundaries in ``kb.audit`` / ``vouch audit``. Artifacts remain readable as
plaintext YAML on disk regardless of scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .models import ArtifactScope, ProposalKind, Visibility

if TYPE_CHECKING:
    from .models import AuditEvent
    from .storage import KBStore

_SCOPED_KINDS = frozenset({"claim", "source"})


@dataclass(frozen=True)
class ViewerContext:
    """Who is asking — used to filter retrieval hits and audit timelines."""

    project: str | None = None
    agent: str | None = None


# Transport auth layers may pass a structured viewer scope (issue #232).
ScopeSpec = ViewerContext


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def viewer_from(
    *,
    config_path: Path | None = None,
    project: str | None = None,
    agent: str | None = None,
) -> ViewerContext:
    """Resolve viewer context: explicit param > env > ``config.yaml``.

    Precedence matches VEP-0005: request param, then ``VOUCH_PROJECT`` /
    ``VOUCH_AGENT``, then ``retrieval.scope`` in config.
    """
    resolved_project = _norm(project)
    resolved_agent = _norm(agent)

    if resolved_project is None:
        resolved_project = _norm(os.environ.get("VOUCH_PROJECT"))
    if resolved_agent is None:
        resolved_agent = _norm(os.environ.get("VOUCH_AGENT"))

    if config_path is not None and (resolved_project is None or resolved_agent is None):
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            loaded = None
        if isinstance(loaded, dict):
            retrieval = loaded.get("retrieval")
            if isinstance(retrieval, dict):
                scope_cfg = retrieval.get("scope")
                if isinstance(scope_cfg, dict):
                    if resolved_project is None:
                        raw = scope_cfg.get("project")
                        if isinstance(raw, str):
                            resolved_project = _norm(raw)
                    if resolved_agent is None:
                        raw = scope_cfg.get("agent")
                        if isinstance(raw, str):
                            resolved_agent = _norm(raw)

    return ViewerContext(project=resolved_project, agent=resolved_agent)


def viewer_from_params(store: KBStore, params: dict[str, Any]) -> ViewerContext:
    """Resolve viewer context from kb.* method params (flat or nested)."""
    nested = params.get("viewer_scope")
    if isinstance(nested, dict):
        return viewer_from(
            config_path=store.config_path,
            project=nested.get("project"),
            agent=nested.get("agent"),
        )
    return viewer_from(
        config_path=store.config_path,
        project=params.get("project"),
        agent=params.get("agent"),
    )


def is_visible(scope: ArtifactScope, viewer: ViewerContext) -> bool:
    """Return whether *scope* is visible to *viewer* in retrieval surfaces."""
    vis = scope.visibility
    if vis in (Visibility.PUBLIC, Visibility.TEAM):
        return True
    if vis == Visibility.PROJECT:
        if scope.project is None:
            return True
        if viewer.project is None:
            return False
        return scope.project == viewer.project
    if vis == Visibility.PRIVATE:
        # Fail closed: private artifacts require an explicit agent match.
        if scope.agent is None or viewer.agent is None:
            return False
        return scope.agent == viewer.agent
    return False


def scoped_fetch_limit(limit: int, viewer: ViewerContext) -> int:
    """Over-fetch when a viewer context may filter many hits away."""
    if viewer.project is not None or viewer.agent is not None:
        return max(limit * 5, limit)
    return limit


def artifact_scope_for_hit(store: KBStore, kind: str, artifact_id: str) -> ArtifactScope | None:
    """Return scope for scoped artifact kinds; ``None`` if kind is unscoped."""
    if kind not in _SCOPED_KINDS:
        return None
    try:
        if kind == "claim":
            return store.get_claim(artifact_id).scope
        if kind == "source":
            return store.get_source(artifact_id).scope
    except Exception:
        return None
    return None


def filter_hits(
    store: KBStore,
    hits: list[tuple[str, str, str, float]],
    viewer: ViewerContext,
    *,
    limit: int | None = None,
) -> list[tuple[str, str, str, float]]:
    """Drop retrieval hits invisible to *viewer*; optionally truncate to *limit*."""
    kept: list[tuple[str, str, str, float]] = []
    for kind, artifact_id, summary, score in hits:
        scope = artifact_scope_for_hit(store, kind, artifact_id)
        if scope is not None and not is_visible(scope, viewer):
            continue
        kept.append((kind, artifact_id, summary, score))
        if limit is not None and len(kept) >= limit:
            break
    return kept


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _parse_scope(raw: object) -> ArtifactScope:
    if raw is None:
        return ArtifactScope()
    if isinstance(raw, str):
        return ArtifactScope(visibility=Visibility(raw))
    if isinstance(raw, dict):
        return ArtifactScope.model_validate(raw)
    return ArtifactScope()


def artifact_scope_for_object_id(store: KBStore, object_id: str) -> ArtifactScope | None:
    """Return scope for an audit ``object_id``; ``None`` if unscoped or unknown."""
    if _is_sha256_hex(object_id):
        try:
            return store.get_source(object_id).scope
        except Exception:
            pass

    try:
        return store.get_claim(object_id).scope
    except Exception:
        pass

    try:
        proposal = store.get_proposal(object_id)
    except Exception:
        return None

    if proposal.kind != ProposalKind.CLAIM:
        return None

    raw = proposal.payload.get("scope")
    return _parse_scope(raw)


def event_visible_to_viewer(
    store: KBStore,
    event: AuditEvent,
    viewer: ViewerContext,
) -> bool:
    """Return whether *event* may be shown to *viewer*.

    Events with no ``object_ids`` (e.g. ``kb.init``) are visible to everyone.
    Otherwise every referenced scoped artifact must pass ``is_visible``.
    """
    if not event.object_ids:
        return True
    for oid in event.object_ids:
        scope = artifact_scope_for_object_id(store, oid)
        if scope is not None and not is_visible(scope, viewer):
            return False
    return True


def filter_audit_events(
    store: KBStore,
    events: list[AuditEvent],
    viewer: ViewerContext,
) -> list[AuditEvent]:
    """Drop audit events whose ``object_ids`` reference artifacts outside *viewer*."""
    return [e for e in events if event_visible_to_viewer(store, e, viewer)]
