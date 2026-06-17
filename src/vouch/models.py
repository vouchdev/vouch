"""Domain models for the knowledge base.

Closely follows the AKBP v0.1 draft schemas (Source, Claim, Page, Entity,
Relation, Evidence, AuditEvent, ContextPack) so a `vouch` KB is portable
to any AKBP-conformant tool. Differences from AKBP are flagged with a
`# vouch:` comment near the field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat()


# --- enums (mirror AKBP schemas) ------------------------------------------


class SourceType(StrEnum):
    FILE = "file"
    URL = "url"
    TRANSCRIPT = "transcript"
    MESSAGE = "message"
    COMMIT = "commit"
    ISSUE = "issue"
    SCREENSHOT = "screenshot"
    PDF = "pdf"
    AUDIO = "audio"
    VIDEO = "video"
    FOLDER = "folder"


class ClaimType(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    WORKFLOW = "workflow"
    OBSERVATION = "observation"
    QUESTION = "question"
    WARNING = "warning"


class ClaimStatus(StrEnum):
    WORKING = "working"
    ACTIONABLE = "actionable"
    STABLE = "stable"
    CONTESTED = "contested"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    REDACTED = "redacted"


class Visibility(StrEnum):
    """How widely an artifact is visible within retrieval surfaces."""

    PRIVATE = "private"
    PROJECT = "project"
    TEAM = "team"
    PUBLIC = "public"


# Back-compat alias — external code may still import Scope.
Scope = Visibility


def _coerce_artifact_scope(value: object) -> object:
    """Accept legacy ``scope: project`` strings and structured objects."""
    if value is None:
        return ArtifactScope()
    if isinstance(value, str):
        return {"visibility": value}
    return value


class ArtifactScope(BaseModel):
    """Structured scope: visibility tier plus optional project/agent binding."""

    visibility: Visibility = Visibility.PROJECT
    project: str | None = None
    agent: str | None = None


class EntityType(StrEnum):
    PERSON = "person"
    PROJECT = "project"
    REPO = "repo"
    COMPANY = "company"
    CONCEPT = "concept"
    DECISION = "decision"
    WORKFLOW = "workflow"
    FILE = "file"
    API = "api"
    INCIDENT = "incident"
    SOURCE = "source"
    AGENT = "agent"
    TOOL = "tool"
    TEAM = "team"
    SYSTEM = "system"


class RelationType(StrEnum):
    USES = "uses"
    DEPENDS_ON = "depends_on"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    SUPPORTS = "supports"
    CAUSED_BY = "caused_by"
    OWNED_BY = "owned_by"
    DERIVED_FROM = "derived_from"
    SIMILAR_TO = "similar_to"
    BLOCKS = "blocks"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    MENTIONS = "mentions"
    RELATES_TO = "relates_to"


class PageType(StrEnum):
    ENTITY = "entity"
    CONCEPT = "concept"
    DECISION = "decision"
    WORKFLOW = "workflow"
    SESSION = "session"
    INDEX = "index"
    LOG = "log"
    REPORT = "report"
    SOURCE_SUMMARY = "source-summary"


class PageStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


# --- core artifacts -------------------------------------------------------


class Source(BaseModel):
    """Immutable input material — file, URL, transcript, commit, etc.

    Identified by sha256 of content (when content is captured locally).
    `locator` retains the human-facing path/URL.
    """

    id: str = Field(description="sha256 of content (hex)")
    type: SourceType = SourceType.FILE
    locator: str = Field(description="path, URL, commit hash, etc.")
    title: str | None = None
    hash: str | None = Field(
        default=None,
        description="sha256; mirrors id for content-addressed sources",
    )
    immutable: bool = True
    scope: ArtifactScope = Field(default_factory=ArtifactScope)
    byte_size: int = 0
    media_type: str = "text/plain"
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_is_hex_sha256(cls, v: str) -> str:
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("id must be a lowercase hex sha256 (64 chars)")
        return v

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_scope(cls, v: object) -> object:
        return _coerce_artifact_scope(v)


class Evidence(BaseModel):
    """Pointer into a Source backing a specific Claim.

    Unlike Source (which is the whole document), Evidence specifies *where*
    in the source the supporting material is — line range, timestamp, quote.
    """

    id: str
    source_id: str
    source_type: SourceType | None = None
    locator: str = Field(description="span ref: 'L10-L20', 't=00:14:23', '#section-3'")
    quote: str | None = None
    hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Claim(BaseModel):
    """Atomic durable assertion.

    The smallest thing that can be cited, contradicted, superseded,
    reinforced, decayed, or archived.
    """

    id: str
    text: str
    type: ClaimType = ClaimType.OBSERVATION
    status: ClaimStatus = ClaimStatus.WORKING
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: list[str] = Field(
        default_factory=list,
        description="Source ids OR Evidence ids — both are valid citations",
    )

    @field_validator("evidence")
    @classmethod
    def _at_least_one_citation(cls, v: list[str]) -> list[str]:
        # The "claims must cite sources" guarantee (README §"Why this exists"
        # point 3; CONTRIBUTING §"Things we won't merge") used to live only
        # in proposals.propose_claim, so every other write path —
        # store.put_claim, store.update_claim, and bundle.import_apply via
        # _validate_content — accepted evidence=[] and silently landed an
        # uncited claim. Enforcing on the model closes all paths at once.
        if not v:
            raise ValueError(
                "claim must cite at least one Source or Evidence id "
                "(README §'Object model'; CONTRIBUTING §'Things we won't merge')"
            )
        return v
    entities: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    contradicts: list[str] = Field(
        default_factory=list,
        description="vouch: claim ids this contradicts",
    )
    scope: ArtifactScope = Field(default_factory=ArtifactScope)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_confirmed_at: datetime | None = None
    approved_by: str | None = None  # vouch: review-gate audit

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_scope(cls, v: object) -> object:
        return _coerce_artifact_scope(v)


class Entity(BaseModel):
    """Typed named thing — a person, project, repo, concept, …

    Entities anchor relations and aggregate the claims that mention them.
    """

    id: str
    name: str
    type: EntityType
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    page: str | None = Field(default=None, description="Optional page id for this entity")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Relation(BaseModel):
    """Typed edge between entities / claims / pages."""

    id: str
    source: str = Field(description="id of the source endpoint")
    relation: RelationType
    target: str = Field(description="id of the target endpoint")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Page(BaseModel):
    """Compiled, maintained markdown — entity write-up, decision record,
    session summary, etc. Body is plain markdown; metadata is YAML
    frontmatter on disk.
    """

    id: str
    title: str
    body: str = ""
    type: PageType = PageType.CONCEPT
    status: PageStatus = PageStatus.DRAFT
    claims: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# --- audit + sessions -----------------------------------------------------


class AuditEvent(BaseModel):
    """Append-only record of one mutation. Written to `.vouch/audit.log.jsonl`."""

    id: str
    event: str = Field(description="dotted verb, e.g. 'claim.create', 'proposal.approve'")
    actor: str
    created_at: datetime = Field(default_factory=utcnow)
    object_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False
    reversible: bool = True
    data: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    """An agent's work session — opened on start, closed on end.

    Captures the agent, the task, the proposals/claims it produced, so that
    `crystallize` can later promote the durable parts.
    """

    id: str
    agent: str
    task: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    proposal_ids: list[str] = Field(default_factory=list)
    note: str | None = None


# --- review gate (vouch's addition on top of AKBP) -------------------------


class ProposalKind(StrEnum):
    CLAIM = "claim"
    PAGE = "page"
    ENTITY = "entity"
    RELATION = "relation"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Proposal(BaseModel):
    """An agent's pending write. Lives outside git until reviewed."""

    id: str
    kind: ProposalKind
    proposed_by: str
    session_id: str | None = None
    proposed_at: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any]
    rationale: str | None = None
    status: ProposalStatus = ProposalStatus.PENDING
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


# --- retrieval ------------------------------------------------------------


class ContextItem(BaseModel):
    """One result inside a ContextPack."""

    id: str
    type: Literal["claim", "page", "entity", "relation", "source"]
    summary: str
    score: float = 0.0
    backend: str = "fts5"
    citations: list[str] = Field(default_factory=list)
    freshness: Literal["fresh", "unknown", "stale"] = "unknown"


class ContextQuality(BaseModel):
    """Quality gate metadata attached to every ContextPack."""

    ok: bool = True
    minimum_items: int = 0
    require_citations: bool = False
    fail_on_warnings: bool = False
    budget_truncated: bool = False
    budget_omitted_items: int = 0
    budget_clipped_items: int = 0
    items: int = 0
    uncited_items: list[str] = Field(default_factory=list)
    warnings: int = 0
    failed: list[str] = Field(default_factory=list)


class ContextPack(BaseModel):
    """A bundle of retrieved items ready to drop into an agent prompt."""

    query: str
    generated_at: datetime = Field(default_factory=utcnow)
    items: list[ContextItem] = Field(default_factory=list)
    quality: ContextQuality = Field(default_factory=ContextQuality)
    warnings: list[str] = Field(default_factory=list)


# --- protocol surfaces ----------------------------------------------------


class Capabilities(BaseModel):
    """Returned by the server in response to `kb.capabilities`."""

    name: str = "vouch"
    version: str
    spec: str = "akbp-0.1-compatible"
    level: int = Field(default=3, description="compliance level — see AKBP §11")
    methods: list[str] = Field(default_factory=list)
    retrieval: list[str] = Field(default_factory=lambda: ["fts5", "substring"])
    review_gated: bool = True
    transports: list[str] = Field(default_factory=lambda: ["mcp", "jsonl"])
    knowledge_capability: dict[str, Any] = Field(
        default_factory=lambda: {
            "kind": "local-cited-review-gated-kb",
            "stores_evidence": True,
            "audit_log": True,
        }
    )
    scoping: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": True,
            "viewer_params": ["project", "agent"],
            "scoped_methods": ["kb.search", "kb.context_pack", "kb.audit"],
            "env_vars": ["VOUCH_PROJECT", "VOUCH_AGENT"],
            "config_path": "retrieval.scope",
        }
    )
