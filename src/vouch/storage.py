"""File-backed storage for the knowledge base.

Layout under <root>/.vouch/:

  config.yaml                  — repo-level KB config
  state.db                     — SQLite FTS5 index (derived from files)
  audit.log.jsonl              — append-only audit log
  .gitignore                   — keeps proposed/ + state.db out of git
  claims/<id>.yaml             — durable approved claims (committed)
  pages/<id>.md                — markdown pages with YAML frontmatter
  sources/<sha>/meta.yaml      — source metadata
  sources/<sha>/content        — raw source bytes
  entities/<id>.yaml           — graph nodes
  relations/<id>.yaml          — graph edges
  evidence/<id>.yaml           — citation pointers into sources
  sessions/<id>.yaml           — session records
  proposed/<id>.yaml           — pending proposals (gitignored — local-only)
  decided/<id>.yaml            — approved/rejected proposals (committed)

The files are the source of truth; the SQLite index is a derived cache
that `vouch index` can rebuild from disk.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .models import (
    Claim,
    Entity,
    Evidence,
    Page,
    Proposal,
    ProposalStatus,
    Relation,
    Session,
    Source,
)

_embed_log = logging.getLogger("vouch.embeddings")

KB_DIRNAME = ".vouch"
CONFIG_FILENAME = "config.yaml"
KB_FORMAT_VERSION = 1

# Semver model-schema version stamped on bootstrap; the migration runner
# (vouch.migrations) advances it. Distinct from KB_FORMAT_VERSION, which is the
# integer directory-layout version in config.yaml.
SCHEMA_VERSION = "0.1.0"
SCHEMA_VERSION_FILENAME = "schema_version"

SUBDIRS = (
    "claims", "pages", "sources", "entities", "relations",
    "evidence", "sessions", "proposed", "decided",
)


class KBNotFoundError(RuntimeError):
    pass


class ArtifactNotFoundError(KeyError):
    pass


def _starter_config() -> dict[str, Any]:
    return {
        "version": KB_FORMAT_VERSION,
        "review": {
            "require_human_approval": True,
            "expire_pending_after_days": 90,
        },
        "capture": {
            # auto-capture claude code sessions into pending summaries.
            "enabled": True,
            "min_observations": 3,
        },
        "recall": {
            # inject a digest of all approved knowledge at session start.
            "enabled": True,
            "max_chars": 12000,
        },
        "retrieval": {
            # auto = embedding -> fts5 -> substring; or pin one of
            # embedding | fts5 | substring. See context._retrieve.
            "backend": "auto",
            "default_limit": 10,
        },
        "agents": {
            "recommended_loop": [
                "kb.search before writing",
                "kb.propose_* with citations",
                "human review via vouch pending/show/approve",
            ],
        },
        # Extra page kinds beyond the built-in PageType enum. Each maps a kind
        # name to {required_fields, frontmatter_schema, required_citations,
        # extends}. See `vouch schema list` / docs for the shape. (issue #234)
        "page_kinds": {},
    }


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover_root(start: Path | None = None) -> Path:
    """Walk up from `start` looking for a `.vouch` directory.

    Mirrors how git locates its repo root. The walk can be skipped entirely
    by setting `VOUCH_KB_PATH=/abs/path/.vouch` (documented in
    `adapters/generic-mcp/README.md`) — useful when the host launches the
    server from a default cwd (e.g. Claude Desktop on macOS / Windows).
    """
    forced = os.environ.get("VOUCH_KB_PATH")
    if forced:
        kb = Path(forced).resolve()
        if not kb.is_dir():
            raise KBNotFoundError(
                f"VOUCH_KB_PATH={forced!r} is not an existing directory"
            )
        if kb.name != KB_DIRNAME:
            raise KBNotFoundError(
                f"VOUCH_KB_PATH must point at a {KB_DIRNAME!r} directory, "
                f"got {forced!r}"
            )
        return kb.parent

    cur = (start or Path.cwd()).resolve()
    while True:
        if (cur / KB_DIRNAME).is_dir():
            return cur
        if cur.parent == cur:
            raise KBNotFoundError(
                f"No {KB_DIRNAME}/ directory found at or above {start or Path.cwd()}"
            )
        cur = cur.parent


def _yaml_dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


def _yaml_load(text: str) -> Any:
    return yaml.safe_load(text)


_log = logging.getLogger("vouch.storage")

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _load_or_skip(path: Path, model: type[_ModelT], kind: str) -> _ModelT | None:
    """Parse one durable artifact file into ``model``.

    On a corrupt or unreadable file — e.g. a hand-edited yaml or mojibake
    carrying a control character that pyyaml's loader rejects — log a warning
    and return ``None`` instead of raising, so a single bad file cannot take
    down a whole bulk listing (``vouch pending`` and friends).
    """
    try:
        return model.model_validate(_yaml_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError, UnicodeDecodeError, OSError) as e:
        _log.warning("skipping unreadable %s %s: %s", kind, path.name, e)
        return None


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _serialize_page(page: Page) -> str:
    meta = page.model_dump(mode="json", exclude={"body"})
    return f"---\n{_yaml_dump(meta)}---\n{page.body}"


def _deserialize_page(text: str) -> Page:
    text = text.replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("page file missing YAML frontmatter")
    meta = _yaml_load(m.group(1)) or {}
    body = m.group(2)
    return Page(body=body, **meta)


class KBStore:
    """File-backed CRUD layer. Pure I/O — no business logic."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.kb_dir = self.root / KB_DIRNAME

    def read_under_root(self, path: str | Path) -> tuple[Path, bytes]:
        # Guard against arbitrary-file-read primitives exposed by the MCP /
        # JSONL `register_source_from_path` entrypoints, and against a TOCTOU
        # race between the containment check and the read: an attacker who
        # can swap the resolved name for a symlink after Path.resolve() has
        # validated it could otherwise still exfiltrate an out-of-root file.
        #
        # Path.resolve() chases any pre-existing symlinks first (so legitimate
        # in-root symlinks still work, then their target is the thing checked
        # for containment). O_NOFOLLOW on the open then rejects a fresh
        # symlink swapped into the resolved name after the containment check.
        # fstat + S_ISREG rejects directories / special files atomically.
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(
                f"path must be inside project root ({self.root}): {resolved}"
            )
        if resolved.is_dir():
            raise ValueError(f"not a regular file: {resolved}")
        flags = os.O_RDONLY
        # POSIX can reject a symlink swapped in after resolve(); Windows has
        # no O_NOFOLLOW, so it falls back to the regular-file check below.
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(resolved, flags)
        except OSError as e:
            raise ValueError(f"cannot read {resolved}: {e}") from e
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise ValueError(f"not a regular file: {resolved}")
        except OSError:
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as f:
            return resolved, f.read()

    # --- bootstrap ---------------------------------------------------------

    @classmethod
    def init(cls, root: Path) -> KBStore:
        kb = cls(root)
        kb.kb_dir.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (kb.kb_dir / sub).mkdir(exist_ok=True)
        if not kb.config_path.exists():
            kb.config_path.write_text(_yaml_dump(_starter_config()), encoding="utf-8")
        schema_version_file = kb.kb_dir / SCHEMA_VERSION_FILENAME
        if not schema_version_file.exists():
            schema_version_file.write_text(SCHEMA_VERSION + "\n", encoding="utf-8")
        gi = kb.kb_dir / ".gitignore"
        if not gi.exists():
            # state.db is derived; proposed/ is the agent's scratch space.
            gi.write_text("proposed/\ncaptures/\nstate.db\nstate.db-*\n", encoding="utf-8")
        return kb

    # --- paths -------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.kb_dir / CONFIG_FILENAME

    def _yaml(self, sub: str, obj_id: str) -> Path:
        return self.kb_dir / sub / f"{obj_id}.yaml"

    def _claim_path(self, claim_id: str) -> Path:
        return self._yaml("claims", claim_id)

    def _page_path(self, page_id: str) -> Path:
        return self.kb_dir / "pages" / f"{page_id}.md"

    def _source_dir(self, source_id: str) -> Path:
        return self.kb_dir / "sources" / source_id

    def _entity_path(self, eid: str) -> Path:
        return self._yaml("entities", eid)

    def _relation_path(self, rid: str) -> Path:
        return self._yaml("relations", rid)

    def _evidence_path(self, eid: str) -> Path:
        return self._yaml("evidence", eid)

    def _session_path(self, sid: str) -> Path:
        return self._yaml("sessions", sid)

    def _proposal_path(self, pid: str) -> Path:
        return self._yaml("proposed", pid)

    def _decided_path(self, pid: str) -> Path:
        return self._yaml("decided", pid)

    # --- sources -----------------------------------------------------------

    def put_source(
        self,
        content: bytes,
        *,
        title: str | None = None,
        url: str | None = None,
        locator: str | None = None,
        source_type: str = "file",
        media_type: str = "text/plain",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Source:
        sid = sha256_hex(content)
        sdir = self._source_dir(sid)
        sdir.mkdir(parents=True, exist_ok=True)
        content_path = sdir / "content"
        if not content_path.exists():
            content_path.write_bytes(content)
        meta_path = sdir / "meta.yaml"
        if meta_path.exists():
            return Source.model_validate(_yaml_load(meta_path.read_text(encoding="utf-8")))
        src = Source(
            id=sid,
            type=source_type,  # type: ignore[arg-type]
            locator=locator or url or title or sid,
            title=title,
            hash=sid,
            byte_size=len(content),
            media_type=media_type,
            tags=tags or [],
            metadata=metadata or {},
        )
        meta_path.write_text(_yaml_dump(src.model_dump(mode="json")), encoding="utf-8")
        self._embed_and_store(kind="source", id=src.id, text=src.title or src.locator or "")
        return src

    def get_source(self, source_id: str) -> Source:
        meta_path = self._source_dir(source_id) / "meta.yaml"
        if not meta_path.exists():
            raise ArtifactNotFoundError(f"source {source_id}")
        return Source.model_validate(_yaml_load(meta_path.read_text(encoding="utf-8")))

    def read_source_content(self, source_id: str) -> bytes:
        p = self._source_dir(source_id) / "content"
        if not p.exists():
            raise ArtifactNotFoundError(f"source content {source_id}")
        return p.read_bytes()

    def list_sources(self) -> list[Source]:
        out: list[Source] = []
        sources_dir = self.kb_dir / "sources"
        if not sources_dir.is_dir():
            return out
        for sdir in sorted(sources_dir.iterdir()):
            meta = sdir / "meta.yaml"
            if meta.exists():
                src = _load_or_skip(meta, Source, "source")
                if src is not None:
                    out.append(src)
        return out

    # --- graph-integrity helpers ------------------------------------------

    # Closes the structural counterpart of the #81 fix: every graph artifact
    # (Relation source/target/evidence, Page entities/sources) must resolve
    # to a known artifact in the KB before it lands on disk. The invariant
    # was already articulated as a `dangling_relation` finding in
    # `health.lint` (`src/vouch/health.py:135-145`) but no write path
    # enforced it, so every approve / lifecycle / bundle / sync surface
    # silently landed graph edges and pages pointing at nothing.

    def _node_exists(self, node_id: str) -> bool:
        """True if `node_id` resolves to a Claim, Page, Entity, or Source."""
        if not node_id:
            return False
        if self._claim_path(node_id).exists():
            return True
        if self._page_path(node_id).exists():
            return True
        if self._entity_path(node_id).exists():
            return True
        return (self._source_dir(node_id) / "meta.yaml").exists()

    def _evidence_ref_exists(self, ref_id: str) -> bool:
        """True if `ref_id` resolves to a Source or Evidence record.

        Matches the citation surface that `put_claim` already accepts:
        either a content-hash Source id or an Evidence id.
        """
        if not ref_id:
            return False
        if (self._source_dir(ref_id) / "meta.yaml").exists():
            return True
        return self._evidence_path(ref_id).exists()

    # --- claims ------------------------------------------------------------

    def _validate_claim_refs(self, claim: Claim) -> None:
        """Reject dangling graph references on a Claim before it lands.

        The #124 graph-integrity fix closed `Relation.source/target/evidence`
        and `Page.entities/sources` (see the note above `_node_exists`) but
        left the Claim's own four reference fields — `entities`,
        `supersedes`, `superseded_by`, `contradicts` — unchecked on every
        write path. `fsck._check_lifecycle_chains` already declares three of
        them as `error`-severity findings (`dangling_supersedes`,
        `dangling_superseded_by`, `dangling_contradicts`), so the invariant
        was articulated but enforced by no writer. Enforce it here, the same
        way `_validate_relation_refs` guards relation endpoints.

        `evidence` is validated separately by `put_claim` (it accepts either
        a Source id or an Evidence id, a different resolution surface).
        """
        for eid in claim.entities:
            if not self._entity_path(eid).exists():
                raise ValueError(
                    f"claim {claim.id} references unknown entity {eid!r}"
                )
        claim_refs = [*claim.supersedes, *claim.contradicts]
        if claim.superseded_by is not None:
            claim_refs.append(claim.superseded_by)
        for cid in claim_refs:
            if not self._claim_path(cid).exists():
                raise ValueError(
                    f"claim {claim.id} references unknown claim {cid!r}"
                )

    def put_claim(self, claim: Claim) -> Claim:
        # Evidence entries can be Source IDs or Evidence IDs -- accept either.
        for cid_or_sid in claim.evidence:
            if (self._source_dir(cid_or_sid) / "meta.yaml").exists():
                continue
            if self._evidence_path(cid_or_sid).exists():
                continue
            raise ValueError(
                f"claim {claim.id} cites unknown source/evidence {cid_or_sid}"
            )
        self._validate_claim_refs(claim)
        try:
            with self._claim_path(claim.id).open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(claim.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"claim {claim.id} already exists -- use update_claim()"
            ) from e
        self._embed_and_store(kind="claim", id=claim.id, text=claim.text)
        return claim

    def get_claim(self, claim_id: str) -> Claim:
        p = self._claim_path(claim_id)
        if not p.exists():
            raise ArtifactNotFoundError(f"claim {claim_id}")
        return Claim.model_validate(_yaml_load(p.read_text(encoding="utf-8")))

    def list_claims(self) -> list[Claim]:
        cdir = self.kb_dir / "claims"
        if not cdir.is_dir():
            return []
        return [
            c
            for p in sorted(cdir.glob("*.yaml"))
            if (c := _load_or_skip(p, Claim, "claim")) is not None
        ]

    def update_claim(self, claim: Claim) -> Claim:
        if not self._claim_path(claim.id).exists():
            raise ArtifactNotFoundError(f"claim {claim.id}")
        # Re-validate the in-memory Claim before persisting so model
        # invariants (e.g. evidence must be non-empty — see #81) hold
        # even when a caller mutated fields in place after get_claim().
        # The Claim model's field validators only run at construction
        # time; mutation alone bypasses them unless we round-trip.
        Claim.model_validate(claim.model_dump(mode="json"))
        # Re-check graph references too: an in-place mutation can introduce a
        # dangling entities/supersedes/superseded_by/contradicts link that the
        # model validator can't catch (it has no KB access). Mirrors the
        # put_claim guard so the update path can't reintroduce the gap.
        self._validate_claim_refs(claim)
        self._claim_path(claim.id).write_text(
            _yaml_dump(claim.model_dump(mode="json")), encoding="utf-8")
        self._embed_and_store(kind="claim", id=claim.id, text=claim.text)
        # Keep the FTS5 row in sync with the on-disk claim so lifecycle
        # mutations (archive, supersede, contradict, confirm) are reflected
        # in retrieval immediately. Without this, claims_fts.status stays
        # frozen at first-index time and retracted claims keep matching
        # kb.search / kb.context.
        from . import index_db as _index_db

        try:
            with _index_db.open_db(self.kb_dir) as conn:
                _index_db.index_claim(
                    conn, id=claim.id, text=claim.text,
                    type=claim.type.value, status=claim.status.value,
                    tags=list(claim.tags),
                )
        except sqlite3.Error as e:
            _embed_log.warning(
                "claim %s: FTS5 reindex skipped on update (%s)", claim.id, e,
            )
        return claim

    # --- pages -------------------------------------------------------------

    def put_page(self, page: Page) -> Page:
        for cid in page.claims:
            if not self._claim_path(cid).exists():
                raise ValueError(f"page {page.id} references unknown claim {cid}")
        for eid in page.entities:
            if not self._entity_path(eid).exists():
                raise ValueError(f"page {page.id} references unknown entity {eid}")
        for sid in page.sources:
            if not (self._source_dir(sid) / "meta.yaml").exists():
                raise ValueError(f"page {page.id} references unknown source {sid}")
        try:
            # Explicit UTF-8: page bodies are user / agent prose and routinely
            # contain non-ASCII (em-dashes, smart quotes, unicode in claims).
            # The default text-mode encoding follows the locale (Latin-1 on a
            # bare Linux container), which would mangle anything past 0x7F.
            with self._page_path(page.id).open("x", encoding="utf-8") as f:
                f.write(_serialize_page(page))
        except FileExistsError as e:
            raise ValueError(
                f"page {page.id} already exists -- choose a different slug"
            ) from e
        self._embed_and_store(kind="page", id=page.id, text=f"{page.title}\n\n{page.body}")
        return page

    def get_page(self, page_id: str) -> Page:
        p = self._page_path(page_id)
        if not p.exists():
            raise ArtifactNotFoundError(f"page {page_id}")
        return _deserialize_page(p.read_text(encoding="utf-8"))

    def update_page(self, page: Page) -> Page:
        """Overwrite an existing page on disk. Used by the vault-edit approve path.

        Parallel to `update_claim`: the caller is responsible for ensuring the
        page id already exists (raises ArtifactNotFoundError otherwise). The
        embedding index is refreshed so search reflects the new body.
        """
        if not self._page_path(page.id).exists():
            raise ArtifactNotFoundError(f"page {page.id}")
        self._page_path(page.id).write_text(
            _serialize_page(page), encoding="utf-8"
        )
        self._embed_and_store(
            kind="page", id=page.id,
            text=f"{page.title}\n\n{page.body}",
        )
        return page

    def list_pages(self) -> list[Page]:
        pdir = self.kb_dir / "pages"
        if not pdir.is_dir():
            return []
        return [
            _deserialize_page(p.read_text(encoding="utf-8"))
            for p in sorted(pdir.glob("*.md"))
        ]

    # --- entities ----------------------------------------------------------

    def put_entity(self, entity: Entity) -> Entity:
        try:
            with self._entity_path(entity.id).open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(entity.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"entity {entity.id} already exists -- choose a different slug"
            ) from e
        self._embed_and_store(
            kind="entity", id=entity.id,
            text=f"{entity.name}\n\n{entity.description or ''}",
        )
        return entity

    def get_entity(self, eid: str) -> Entity:
        p = self._entity_path(eid)
        if not p.exists():
            raise ArtifactNotFoundError(f"entity {eid}")
        return Entity.model_validate(_yaml_load(p.read_text(encoding="utf-8")))

    def list_entities(self) -> list[Entity]:
        d = self.kb_dir / "entities"
        if not d.is_dir():
            return []
        return [e for p in sorted(d.glob("*.yaml"))
                if (e := _load_or_skip(p, Entity, "entity")) is not None]

    # --- relations ---------------------------------------------------------

    def _validate_relation_refs(self, rel: Relation) -> None:
        if not self._node_exists(rel.source):
            raise ValueError(
                f"relation {rel.id} references unknown source endpoint "
                f"{rel.source!r} (must be an existing claim, page, entity, "
                f"or source id)"
            )
        if not self._node_exists(rel.target):
            raise ValueError(
                f"relation {rel.id} references unknown target endpoint "
                f"{rel.target!r} (must be an existing claim, page, entity, "
                f"or source id)"
            )
        for eid in rel.evidence:
            if not self._evidence_ref_exists(eid):
                raise ValueError(
                    f"relation {rel.id} cites unknown source/evidence {eid!r}"
                )

    def put_relation(self, rel: Relation) -> Relation:
        self._validate_relation_refs(rel)
        try:
            with self._relation_path(rel.id).open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(rel.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"relation {rel.id} already exists -- choose a different slug"
            ) from e
        self._embed_and_store(
            kind="relation", id=rel.id,
            text=f"{rel.source} {rel.relation.value} {rel.target}",
        )
        return rel

    def put_relation_idempotent(self, rel: Relation) -> Relation:
        """Write a relation only if it does not already exist.

        Used by lifecycle ops (supersede, contradict) that need to converge
        to a consistent state on retry without raising if the relation file
        was already written in a previous partial execution.
        """
        path = self._relation_path(rel.id)
        if path.exists():
            self._embed_and_store(
                kind="relation", id=rel.id,
                text=f"{rel.source} {rel.relation.value} {rel.target}",
            )
            return rel
        # Validate before the exclusive create. Skipping validation for the
        # "already on disk" branch above is deliberate — a relation that's
        # already durable was validated when it landed; re-checking would
        # turn supersede/contradict retries into spurious failures whenever
        # the linked claim was subsequently archived or retracted.
        self._validate_relation_refs(rel)
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(rel.model_dump(mode="json")))
        except FileExistsError:
            self._embed_and_store(
                kind="relation", id=rel.id,
                text=f"{rel.source} {rel.relation.value} {rel.target}",
            )
            return rel  # lost the race — already written, that's fine
        self._embed_and_store(
            kind="relation", id=rel.id,
            text=f"{rel.source} {rel.relation.value} {rel.target}",
        )
        return rel

    def get_relation(self, rid: str) -> Relation:
        p = self._relation_path(rid)
        if not p.exists():
            raise ArtifactNotFoundError(f"relation {rid}")
        return Relation.model_validate(_yaml_load(p.read_text(encoding="utf-8")))

    def list_relations(self) -> list[Relation]:
        d = self.kb_dir / "relations"
        if not d.is_dir():
            return []
        return [r for p in sorted(d.glob("*.yaml"))
                if (r := _load_or_skip(p, Relation, "relation")) is not None]

    def relations_from(self, node_id: str) -> list[Relation]:
        return [r for r in self.list_relations() if r.source == node_id]

    def relations_to(self, node_id: str) -> list[Relation]:
        return [r for r in self.list_relations() if r.target == node_id]

    # --- evidence ----------------------------------------------------------

    def put_evidence(self, ev: Evidence) -> Evidence:
        if not (self._source_dir(ev.source_id) / "meta.yaml").exists():
            raise ValueError(f"evidence {ev.id} cites unknown source {ev.source_id}")
        try:
            with self._evidence_path(ev.id).open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(ev.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"evidence {ev.id} already exists -- choose a different slug"
            ) from e
        self._embed_and_store(kind="evidence", id=ev.id, text=ev.quote or "")
        return ev

    def get_evidence(self, eid: str) -> Evidence:
        p = self._evidence_path(eid)
        if not p.exists():
            raise ArtifactNotFoundError(f"evidence {eid}")
        return Evidence.model_validate(_yaml_load(p.read_text(encoding="utf-8")))

    def list_evidence(self) -> list[Evidence]:
        d = self.kb_dir / "evidence"
        if not d.is_dir():
            return []
        return [ev for p in sorted(d.glob("*.yaml"))
                if (ev := _load_or_skip(p, Evidence, "evidence")) is not None]

    # --- sessions ----------------------------------------------------------

    def put_session(self, sess: Session) -> Session:
        try:
            with self._session_path(sess.id).open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(sess.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"session {sess.id} already exists -- choose a different id"
            ) from e
        return sess

    def update_session(self, sess: Session) -> Session:
        # session_end() mutates an already-on-disk session (sets ended_at /
        # backfills proposal_ids). put_session() uses exclusive create as a
        # guard against duplicate ids, so updates need a separate path.
        if not self._session_path(sess.id).exists():
            raise ArtifactNotFoundError(f"session {sess.id}")
        self._session_path(sess.id).write_text(
            _yaml_dump(sess.model_dump(mode="json")), encoding="utf-8")
        return sess

    def get_session(self, sid: str) -> Session:
        p = self._session_path(sid)
        if not p.exists():
            raise ArtifactNotFoundError(f"session {sid}")
        return Session.model_validate(_yaml_load(p.read_text(encoding="utf-8")))

    def list_sessions(self) -> list[Session]:
        d = self.kb_dir / "sessions"
        if not d.is_dir():
            return []
        return [s for p in sorted(d.glob("*.yaml"))
                if (s := _load_or_skip(p, Session, "session")) is not None]

    # --- embedding hook ------------------------------------------------------

    def _embed_and_store(
        self, *, kind: str, id: str, text: str, force: bool = False
    ) -> None:
        """Compute and persist an embedding for an artifact.

        Skipped only if (kind, id) already has an embedding produced by
        the *current* embedder model for the same content. Changing
        embedders mid-life means the existing vector is in the wrong
        space, so we must re-embed even when the content hash matches.

        Every failure in here is swallowed (logged at DEBUG) — embeddings
        are an enhancement, not a hard requirement. The caller has
        already committed the artifact to disk; we must not undo that.
        """
        if not text or not text.strip():
            return
        try:
            from . import index_db as _index_db
            from .embeddings import content_hash, get_embedder
        except ImportError:
            return
        try:
            embedder = get_embedder()
        except (KeyError, ImportError):
            # No embedder registered, or the registered adapter's heavy deps
            # (e.g. sentence-transformers) aren't installed. Best-effort hook.
            return
        try:
            h = content_hash(text)
            existing = _index_db.get_embedding(self.kb_dir, kind=kind, id=id)
            # existing is (vec, content_hash, model); skip only when both the
            # content AND the embedder model match what's on disk.
            if (
                not force
                and existing is not None
                and existing[1] == h
                and existing[2] == embedder.name
            ):
                return
            vec = embedder.encode(text)
            with _index_db.open_db(self.kb_dir) as conn:
                _index_db.put_embedding(
                    conn, kind=kind, id=id, vec=vec, content_hash=h,
                    model=embedder.name, model_version=embedder.version,
                    dim=embedder.dim,
                )
            _index_db.set_embedding_meta(
                self.kb_dir, model=embedder.name,
                version=embedder.version, dim=embedder.dim,
            )
        except Exception as e:
            _embed_log.debug("embedding write failed for %s/%s: %s", kind, id, e)
            return
        try:
            # NB: dedup module is added in a later phase; ignore missing-stub
            # / missing-module noise in CI's [dev]-only mypy run.
            from .embeddings.dedup import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
                check_and_log,
            )
            check_and_log(self.kb_dir, kind=kind, id=id, vec=vec)
        except ImportError:
            pass
        except Exception as e:
            _embed_log.debug("dedup check failed for %s/%s: %s", kind, id, e)

    # --- proposals ---------------------------------------------------------

    def put_proposal(self, proposal: Proposal) -> Proposal:
        try:
            with self._proposal_path(proposal.id).open("x", encoding="utf-8") as f:
                f.write(_yaml_dump(proposal.model_dump(mode="json")))
        except FileExistsError as e:
            raise ValueError(
                f"proposal {proposal.id} already exists -- choose a different id"
            ) from e
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal:
        for path in (self._proposal_path(proposal_id), self._decided_path(proposal_id)):
            if path.exists():
                return Proposal.model_validate(_yaml_load(path.read_text(encoding="utf-8")))
        raise ArtifactNotFoundError(f"proposal {proposal_id}")

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        out: list[Proposal] = []
        for sub in ("proposed", "decided"):
            for p in sorted((self.kb_dir / sub).glob("*.yaml")):
                pr = _load_or_skip(p, Proposal, "proposal")
                if pr is None:
                    continue
                if status is None or pr.status == status:
                    out.append(pr)
        return out

    def move_proposal_to_decided(self, proposal: Proposal) -> None:
        src = self._proposal_path(proposal.id)
        dst = self._decided_path(proposal.id)
        dst.write_text(_yaml_dump(proposal.model_dump(mode="json")), encoding="utf-8")
        if src.exists():
            src.unlink()

    # --- substring search (fallback when state.db is absent) --------------

    def search_substring(self, query: str, *, limit: int = 10
                         ) -> list[tuple[str, str, str, float]]:
        q = query.strip().lower()
        if not q:
            return []
        hits: list[tuple[str, str, str, float]] = []
        for claim in self.list_claims():
            text = claim.text.lower()
            if q in text:
                hits.append(("claim", claim.id, claim.text, float(text.count(q))))
        for page in self.list_pages():
            text = (page.title + "\n" + page.body).lower()
            if q in text:
                hits.append(("page", page.id, page.title, float(text.count(q))))
        for entity in self.list_entities():
            text = (entity.name + " " + (entity.description or "")).lower()
            if q in text:
                hits.append(("entity", entity.id, entity.name,
                             float(text.count(q))))
        hits.sort(key=lambda h: h[3], reverse=True)
        return hits[:limit]

    # Back-compat alias — existing tests call store.search().
    search = search_substring
