# Review-Gated Artifact Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hard-delete path for durable artifacts (claim, page, entity, relation) that routes entirely through the review gate — an agent files `kb.propose_delete`, a different reviewer approves via the existing `kb.approve`, and the artifact's file + index rows are removed.

**Architecture:** Deletion is modeled as a new `ProposalKind.DELETE` proposal carrying `{target_kind, id, snapshot}`. `proposals.approve()` gains a branch that *removes* instead of *creates*. A shared `referenced_by()` helper enforces "block if referenced" at both propose and approve time. Storage gains pure-I/O `delete_*` unlinks; `index_db` gains a `deindex()` helper. One new method, `kb.propose_delete`, is mirrored across the four surfaces.

**Tech Stack:** Python 3, pydantic v2, click (CLI), FastMCP (`@mcp.tool()`), SQLite FTS5 (`index_db`), pytest.

## Global Constraints

- Every write goes through `proposals.approve()`. No direct-mutation delete path. (CLAUDE.md north star.)
- `storage.py` is pure I/O — no business logic (no ref checks) in the `delete_*` methods.
- New `kb.*` method must be registered at all four sites: MCP tool (`server.py`), JSONL handler (`jsonl_server.py`), `METHODS` (`capabilities.py`), CLI (`cli.py`). `test_capabilities` enforces parity.
- CI gate (must stay green): `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings`, `.venv/bin/python -m mypy src`, `.venv/bin/python -m ruff check src tests`.
- Conventional commits, lowercase summary ≤72 chars, lowercase body, **no `Co-Authored-By` trailer**.
- Stage specific files only — never `git add -A`.
- Commit messages via `git commit -F <file>` (the pre-commit hook rejects heredocs / some `-m` forms). Write the message to the scratchpad first.

---

### Task 1: Storage delete methods (pure I/O)

**Files:**
- Modify: `src/vouch/storage.py` (add four methods near `put_relation`, ~line 617)
- Test: `tests/test_delete.py` (create)

**Interfaces:**
- Consumes: existing `self._claim_path`, `self._page_path`, `self._entity_path`, `self._relation_path`, and `ArtifactNotFoundError` (all already in `storage.py`).
- Produces: `KBStore.delete_claim(claim_id: str) -> None`, `.delete_page(page_id: str) -> None`, `.delete_entity(entity_id: str) -> None`, `.delete_relation(relation_id: str) -> None`. Each unlinks the file; raises `ArtifactNotFoundError` if absent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_delete.py`:

```python
"""Review-gated hard delete for durable artifacts (claim/page/entity/relation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.models import Claim, Entity, EntityType, Page, Relation, RelationType
from vouch.storage import ArtifactNotFoundError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _claim(store: KBStore, cid: str = "c1", text: str = "a claim") -> Claim:
    src = store.put_source(b"src-bytes")
    return store.put_claim(Claim(id=cid, text=text, evidence=[src.id]))


def test_delete_claim_removes_file(store: KBStore) -> None:
    _claim(store, "c1")
    assert store._claim_path("c1").exists()
    store.delete_claim("c1")
    assert not store._claim_path("c1").exists()
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("c1")


def test_delete_claim_missing_raises(store: KBStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.delete_claim("nope")


def test_delete_page_removes_file(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="hi"))
    assert store._page_path("p1").exists()
    store.delete_page("p1")
    assert not store._page_path("p1").exists()


def test_delete_entity_removes_file(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    store.delete_entity("e1")
    assert not store._entity_path("e1").exists()


def test_delete_relation_removes_file(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    rel = store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    store.delete_relation(rel.id)
    assert not store._relation_path(rel.id).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_delete.py -q`
Expected: FAIL — `AttributeError: 'KBStore' object has no attribute 'delete_claim'`.

- [ ] **Step 3: Write minimal implementation**

In `src/vouch/storage.py`, add after `put_relation_idempotent` (i.e. after the relation write methods, before the page/source read helpers):

```python
    def delete_claim(self, claim_id: str) -> None:
        """Remove a claim file. Pure I/O; ref checks live in `proposals`."""
        path = self._claim_path(claim_id)
        if not path.exists():
            raise ArtifactNotFoundError(f"claim {claim_id}")
        path.unlink()

    def delete_page(self, page_id: str) -> None:
        """Remove a page file. Pure I/O; ref checks live in `proposals`."""
        path = self._page_path(page_id)
        if not path.exists():
            raise ArtifactNotFoundError(f"page {page_id}")
        path.unlink()

    def delete_entity(self, entity_id: str) -> None:
        """Remove an entity file. Pure I/O; ref checks live in `proposals`."""
        path = self._entity_path(entity_id)
        if not path.exists():
            raise ArtifactNotFoundError(f"entity {entity_id}")
        path.unlink()

    def delete_relation(self, relation_id: str) -> None:
        """Remove a relation file. Pure I/O; ref checks live in `proposals`."""
        path = self._relation_path(relation_id)
        if not path.exists():
            raise ArtifactNotFoundError(f"relation {relation_id}")
        path.unlink()
```

- [ ] **Step 4: Run tests + typecheck + lint**

Run: `.venv/bin/python -m pytest tests/test_delete.py -q`
Expected: PASS (5 passed).

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

Write the message to `scratchpad/msg1.txt`:

```
feat(delete): add pure-io storage delete_* for four kinds

delete_claim/page/entity/relation unlink the artifact file and raise
ArtifactNotFoundError if absent. no ref checks here — those live in the
proposals review gate.
```

```bash
git add src/vouch/storage.py tests/test_delete.py
git commit -F scratchpad/msg1.txt
```

---

### Task 2: `index_db.deindex()` helper

**Files:**
- Modify: `src/vouch/index_db.py` (add near `index_claim`, ~line 180)
- Test: `tests/test_delete.py` (append)

**Interfaces:**
- Consumes: existing tables `claims_fts`, `pages_fts`, `entities_fts`, `embedding_index(kind, id, ...)`, `prov_edges(src_id, dst_id, ...)`; existing `open_db`, `index_claim`, `index_prov_edge`.
- Produces: `index_db.deindex(conn: sqlite3.Connection, *, kind: str, id: str) -> None` — removes the FTS row (claim/page/entity only), the embedding row (any kind), and any prov edge touching `id`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_delete.py`:

```python
from vouch import index_db


def test_deindex_removes_fts_and_prov(store: KBStore) -> None:
    _claim(store, "c1", "searchable claim text")
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id="c1", text="searchable claim text",
            type="observation", status="working", tags=[],
        )
        index_db.index_prov_edge(conn, src_id="c1", dst_id="src-x", kind="cites")
        index_db.index_prov_edge(conn, src_id="other", dst_id="c1", kind="cites")
    # sanity: the fts row is present
    with index_db.open_db(store.kb_dir) as conn:
        pre = conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0]
        assert pre == 1

    with index_db.open_db(store.kb_dir) as conn:
        index_db.deindex(conn, kind="claim", id="c1")

    with index_db.open_db(store.kb_dir) as conn:
        assert conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0] == 0
        prov = conn.execute(
            "SELECT count(*) FROM prov_edges WHERE src_id='c1' OR dst_id='c1'"
        ).fetchone()[0]
        assert prov == 0


def test_deindex_relation_only_touches_embedding_and_prov(store: KBStore) -> None:
    # relations have no FTS table; deindex must not raise for them.
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_prov_edge(conn, src_id="r1", dst_id="c2", kind="edge")
        index_db.deindex(conn, kind="relation", id="r1")
        assert conn.execute(
            "SELECT count(*) FROM prov_edges WHERE src_id='r1' OR dst_id='r1'"
        ).fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_delete.py -k deindex -q`
Expected: FAIL — `AttributeError: module 'vouch.index_db' has no attribute 'deindex'`.

- [ ] **Step 3: Write minimal implementation**

In `src/vouch/index_db.py`, add directly after `index_claim` (before the provenance section comment):

```python
def deindex(conn: sqlite3.Connection, *, kind: str, id: str) -> None:
    """Remove every derived index row for a deleted artifact.

    FTS row for claim/page/entity (relations have no FTS table); the
    embedding row for any kind (every put_* calls _embed_and_store, so an
    embedding may exist for a relation too); and any provenance edge that
    touches the id. prov_edges is otherwise rebuildable via
    `kb.provenance_rebuild` — this keeps state.db consistent without a
    full rebuild.
    """
    if kind == "claim":
        conn.execute("DELETE FROM claims_fts WHERE id = ?", (id,))
    elif kind == "page":
        conn.execute("DELETE FROM pages_fts WHERE id = ?", (id,))
    elif kind == "entity":
        conn.execute("DELETE FROM entities_fts WHERE id = ?", (id,))
    conn.execute(
        "DELETE FROM embedding_index WHERE kind = ? AND id = ?", (kind, id)
    )
    conn.execute(
        "DELETE FROM prov_edges WHERE src_id = ? OR dst_id = ?", (id, id)
    )
```

- [ ] **Step 4: Run tests + typecheck + lint**

Run: `.venv/bin/python -m pytest tests/test_delete.py -q`
Expected: PASS (7 passed).

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

Write to `scratchpad/msg2.txt`:

```
feat(delete): add index_db.deindex for removed artifacts

drops the fts row (claim/page/entity), the embedding row (any kind), and
prov edges touching the id, so state.db stays consistent when an artifact
is hard-deleted.
```

```bash
git add src/vouch/index_db.py tests/test_delete.py
git commit -F scratchpad/msg2.txt
```

---

### Task 3: `ProposalKind.DELETE` + `referenced_by()` matrix helper

**Files:**
- Modify: `src/vouch/models.py` (`ProposalKind` enum, ~line 392)
- Modify: `src/vouch/proposals.py` (add constants + `referenced_by`, near top-level helpers)
- Test: `tests/test_delete.py` (append)

**Interfaces:**
- Consumes: `store.list_pages()`, `store.list_relations()`, `store.list_claims()` and the `Claim.supersedes/superseded_by/contradicts`, `Page.claims/entities`, `Relation.source/target` fields (all existing).
- Produces:
  - `ProposalKind.DELETE = "delete"`.
  - `proposals._DELETE_KINDS: set[str]` = `{"claim","page","entity","relation"}`.
  - `proposals._DELETE_GETTERS: dict[str, str]` mapping kind → `KBStore` getter method name.
  - `proposals.referenced_by(store, target_kind: str, target_id: str) -> list[str]` — human-readable descriptions of inbound referrers; empty list ⇒ deletable. Raises `ProposalError` on unknown kind.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_delete.py`:

```python
from vouch.models import ProposalKind
from vouch.proposals import ProposalError, referenced_by


def test_proposalkind_has_delete() -> None:
    assert ProposalKind.DELETE.value == "delete"


def test_claim_referenced_by_page(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    refs = referenced_by(store, "claim", "c1")
    assert any("p1" in r for r in refs)


def test_claim_referenced_by_relation_and_supersede(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c2--supports--c1", source="c2",
        relation=RelationType.SUPPORTS, target="c1",
    ))
    refs = referenced_by(store, "claim", "c1")
    assert any("relation" in r for r in refs)


def test_unreferenced_claim_is_deletable(store: KBStore) -> None:
    _claim(store, "lonely")
    assert referenced_by(store, "claim", "lonely") == []


def test_entity_referenced_by_claim(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    src = store.put_source(b"s")
    store.put_claim(Claim(id="c1", text="mentions e1", evidence=[src.id], entities=["e1"]))
    refs = referenced_by(store, "entity", "e1")
    assert any("c1" in r for r in refs)


def test_relation_never_blocked(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    # nothing points at an edge → always deletable
    assert referenced_by(store, "relation", "c1--supports--c2") == []


def test_referenced_by_unknown_kind_raises(store: KBStore) -> None:
    with pytest.raises(ProposalError):
        referenced_by(store, "source", "x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_delete.py -k "referenced_by or proposalkind_has_delete or deletable or never_blocked" -q`
Expected: FAIL — `AttributeError: DELETE` / `ImportError: cannot import name 'referenced_by'`.

- [ ] **Step 3a: Add the enum member**

In `src/vouch/models.py`, extend `ProposalKind`:

```python
class ProposalKind(StrEnum):
    CLAIM = "claim"
    PAGE = "page"
    ENTITY = "entity"
    RELATION = "relation"
    DELETE = "delete"
```

- [ ] **Step 3b: Add constants + helper in proposals.py**

In `src/vouch/proposals.py`, add near the other module-level maps (e.g. just below `_ARTIFACT_GETTERS`, ~line 682):

```python
_DELETE_KINDS = {"claim", "page", "entity", "relation"}

_DELETE_GETTERS = {
    "claim": "get_claim",
    "page": "get_page",
    "entity": "get_entity",
    "relation": "get_relation",
}


def referenced_by(store: KBStore, target_kind: str, target_id: str) -> list[str]:
    """Inbound referrers to `target_id` — the "block if referenced" gate.

    Returns human-readable descriptions of artifacts that point AT the
    target. Only inbound refs count; outbound refs (what the target itself
    points at) are never returned, because deleting the holder simply drops
    its own pointers. An empty list means the artifact is safe to delete.
    """
    if target_kind not in _DELETE_KINDS:
        raise ProposalError(
            f"unknown target_kind {target_kind!r}; expected one of "
            f"{sorted(_DELETE_KINDS)}"
        )
    refs: list[str] = []
    if target_kind == "claim":
        for page in store.list_pages():
            if target_id in page.claims:
                refs.append(f"page {page.id!r}")
        for rel in store.list_relations():
            if target_id in (rel.source, rel.target):
                refs.append(f"relation {rel.id!r}")
        for claim in store.list_claims():
            if claim.id == target_id:
                continue
            if (
                target_id in claim.supersedes
                or claim.superseded_by == target_id
                or target_id in claim.contradicts
            ):
                refs.append(f"claim {claim.id!r}")
    elif target_kind == "page":
        for rel in store.list_relations():
            if target_id in (rel.source, rel.target):
                refs.append(f"relation {rel.id!r}")
    elif target_kind == "entity":
        for claim in store.list_claims():
            if target_id in claim.entities:
                refs.append(f"claim {claim.id!r}")
        for page in store.list_pages():
            if target_id in page.entities:
                refs.append(f"page {page.id!r}")
        for rel in store.list_relations():
            if target_id in (rel.source, rel.target):
                refs.append(f"relation {rel.id!r}")
    # target_kind == "relation": edges have no inbound refs → refs stays empty
    return refs
```

- [ ] **Step 4: Run tests + typecheck + lint**

Run: `.venv/bin/python -m pytest tests/test_delete.py -q`
Expected: PASS (all green).

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

Write to `scratchpad/msg3.txt`:

```
feat(delete): add ProposalKind.DELETE and referenced_by matrix

referenced_by returns inbound referrers per kind (claim: pages, relations,
supersede/contradict; page: relations; entity: claims, pages, relations;
relation: none). shared by the propose and approve delete gates.
```

```bash
git add src/vouch/models.py src/vouch/proposals.py tests/test_delete.py
git commit -F scratchpad/msg3.txt
```

---

### Task 4: `propose_delete()`

**Files:**
- Modify: `src/vouch/proposals.py` (add `propose_delete` near the other `propose_*`, ~line 317)
- Test: `tests/test_delete.py` (append)

**Interfaces:**
- Consumes: `referenced_by`, `_DELETE_KINDS`, `_DELETE_GETTERS`, `_file_proposal`, `ProposalKind.DELETE`, `ArtifactNotFoundError`.
- Produces: `proposals.propose_delete(store, *, target_kind: str, target_id: str, proposed_by: str, rationale: str | None = None, session_id: str | None = None, dry_run: bool = False) -> Proposal`. Payload shape `{"target_kind","id","snapshot"}`. Raises `ProposalError` for unknown kind, missing target, or referenced target.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_delete.py`:

```python
from vouch.models import ProposalStatus
from vouch.proposals import propose_delete


def test_propose_delete_files_pending(store: KBStore) -> None:
    _claim(store, "c1", "delete me")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    assert pr.kind is ProposalKind.DELETE
    assert pr.status is ProposalStatus.PENDING
    assert pr.payload["target_kind"] == "claim"
    assert pr.payload["id"] == "c1"
    assert pr.payload["snapshot"]["text"] == "delete me"
    # still pending in the queue
    assert any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))


def test_propose_delete_unknown_target_raises(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown claim id"):
        propose_delete(store, target_kind="claim", target_id="ghost", proposed_by="a")


def test_propose_delete_bad_kind_raises(store: KBStore) -> None:
    with pytest.raises(ProposalError, match="unknown target_kind"):
        propose_delete(store, target_kind="source", target_id="x", proposed_by="a")


def test_propose_delete_referenced_claim_blocked(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ProposalError, match="referenced by"):
        propose_delete(store, target_kind="claim", target_id="c1", proposed_by="a")


def test_propose_delete_claim_block_hints_supersede(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ProposalError, match="supersede"):
        propose_delete(store, target_kind="claim", target_id="c1", proposed_by="a")


def test_propose_delete_dry_run_writes_nothing(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(
        store, target_kind="claim", target_id="c1",
        proposed_by="a", dry_run=True,
    )
    assert store.list_proposals(ProposalStatus.PENDING) == []
    assert pr.id  # id is still returned for preview
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_delete.py -k propose_delete -q`
Expected: FAIL — `ImportError: cannot import name 'propose_delete'`.

- [ ] **Step 3: Write minimal implementation**

In `src/vouch/proposals.py`, add after `propose_relation` (before the `# --- decisions ---` divider, ~line 317):

```python
def propose_delete(
    store: KBStore,
    *,
    target_kind: str,
    target_id: str,
    proposed_by: str,
    rationale: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    """File a review-gated request to hard-delete a durable artifact.

    Blocked (at propose time, re-checked at approve) if the target is still
    referenced by another artifact — the maintainer must supersede or remove
    the referrers first. The full artifact is snapshotted into the payload so
    the decided proposal and audit event record exactly what was removed.
    """
    if target_kind not in _DELETE_KINDS:
        raise ProposalError(
            f"unknown target_kind {target_kind!r}; expected one of "
            f"{sorted(_DELETE_KINDS)}"
        )
    getter = getattr(store, _DELETE_GETTERS[target_kind])
    try:
        artifact = getter(target_id)
    except ArtifactNotFoundError as e:
        raise ProposalError(f"unknown {target_kind} id: {target_id}") from e
    refs = referenced_by(store, target_kind, target_id)
    if refs:
        hint = " (supersede it instead?)" if target_kind == "claim" else ""
        raise ProposalError(
            f"cannot delete {target_kind} {target_id}: referenced by "
            + ", ".join(refs)
            + hint
        )
    payload = {
        "target_kind": target_kind,
        "id": target_id,
        "snapshot": artifact.model_dump(mode="json"),
    }
    return _file_proposal(
        store, kind=ProposalKind.DELETE, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )
```

Note: `referenced_by`, `_DELETE_KINDS`, and `_DELETE_GETTERS` are defined lower in the file (Task 3). Python resolves them at call time, so a forward reference from `propose_delete` is fine.

- [ ] **Step 4: Run tests + typecheck + lint**

Run: `.venv/bin/python -m pytest tests/test_delete.py -q`
Expected: PASS.

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

Write to `scratchpad/msg4.txt`:

```
feat(delete): add proposals.propose_delete

files a PENDING delete proposal for a claim/page/entity/relation, snapshots
the artifact into the payload, and refuses up front if the target is still
referenced (claims get a supersede hint).
```

```bash
git add src/vouch/proposals.py tests/test_delete.py
git commit -F scratchpad/msg4.txt
```

---

### Task 5: `approve()` DELETE branch + batch precheck

**Files:**
- Modify: `src/vouch/proposals.py` (`approve` restructure + `_approve_delete`/`_reconstruct_deleted` helpers + `_payload_block_reason` branch)
- Test: `tests/test_delete.py` (append)

**Interfaces:**
- Consumes: `store.delete_claim/page/entity/relation` (Task 1), `index_db.deindex` (Task 2), `referenced_by` (Task 3), `audit.log_event`, `Claim/Page/Entity/Relation` models.
- Produces: `approve()` handles `ProposalKind.DELETE` — removes the artifact, deindexes, logs a per-kind `<kind>.delete` audit event with the snapshot, returns the (former) artifact model. Idempotent when the artifact is already gone. `check_approvable` returns a block reason for a referenced delete target.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_delete.py`:

```python
from vouch import audit
from vouch.proposals import approve, check_approvable


def _propose_and_approve_delete(store: KBStore, kind: str, tid: str) -> None:
    pr = propose_delete(store, target_kind=kind, target_id=tid, proposed_by="agent")
    approve(store, pr.id, approved_by="reviewer")


def test_approve_delete_removes_claim_and_indexes(store: KBStore) -> None:
    _claim(store, "c1", "gone soon")
    _propose_and_approve_delete(store, "claim", "c1")
    assert not store._claim_path("c1").exists()
    with index_db.open_db(store.kb_dir) as conn:
        assert conn.execute("SELECT count(*) FROM claims_fts WHERE id='c1'").fetchone()[0] == 0
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "claim.delete" in events


def test_approve_delete_page(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="x"))
    _propose_and_approve_delete(store, "page", "p1")
    assert not store._page_path("p1").exists()


def test_approve_delete_entity(store: KBStore) -> None:
    store.put_entity(Entity(id="e1", name="E", type=EntityType.CONCEPT))
    _propose_and_approve_delete(store, "entity", "e1")
    assert not store._entity_path("e1").exists()


def test_approve_delete_relation(store: KBStore) -> None:
    _claim(store, "c1")
    _claim(store, "c2")
    store.put_relation(Relation(
        id="c1--supports--c2", source="c1",
        relation=RelationType.SUPPORTS, target="c2",
    ))
    _propose_and_approve_delete(store, "relation", "c1--supports--c2")
    assert not store._relation_path("c1--supports--c2").exists()


def test_approve_rechecks_reference_added_after_propose(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    # a page starts referencing c1 AFTER the proposal was filed
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    with pytest.raises(ProposalError, match="still referenced"):
        approve(store, pr.id, approved_by="reviewer")
    # target survives, proposal stays pending
    assert store._claim_path("c1").exists()
    assert any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))


def test_approve_delete_idempotent_when_already_gone(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    store.delete_claim("c1")  # simulate a crash-retry: file already removed
    result = approve(store, pr.id, approved_by="reviewer")
    assert result.id == "c1"
    # proposal is finalized (moved out of pending)
    assert not any(p.id == pr.id for p in store.list_proposals(ProposalStatus.PENDING))


def test_delete_forbids_self_approval(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="same")
    with pytest.raises(ProposalError, match="forbidden_self_approval"):
        approve(store, pr.id, approved_by="same")


def test_check_approvable_flags_referenced_delete(store: KBStore) -> None:
    _claim(store, "c1")
    pr = propose_delete(store, target_kind="claim", target_id="c1", proposed_by="agent")
    store.put_page(Page(id="p1", title="P", body="", claims=["c1"]))
    reason = check_approvable(store, pr.id, approved_by="reviewer")
    assert reason is not None and "referenced by" in reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_delete.py -k "approve_delete or rechecks or idempotent or self_approval or check_approvable_flags" -q`
Expected: FAIL — the DELETE proposal falls through `approve()`'s `else: # RELATION` branch and errors constructing a `Relation`, or `_ensure_no_existing_artifact` KeyErrors.

- [ ] **Step 3a: Exempt DELETE from the overwrite guard**

In `src/vouch/proposals.py` `approve()`, change the guard (currently ~line 460):

```python
    if proposal.kind not in (ProposalKind.PAGE, ProposalKind.DELETE):
        _ensure_no_existing_artifact(store, proposal.kind, payload["id"])
```

- [ ] **Step 3b: Add the DELETE branch to the dispatch**

In `approve()`, insert a branch before the final `else:  # RELATION`:

```python
    elif proposal.kind == ProposalKind.DELETE:
        result = _approve_delete(store, proposal, approved_by=approved_by)
    else:  # RELATION
        rel = Relation(**payload)
        store.put_relation(rel)
        result = rel
```

- [ ] **Step 3c: Add the helper functions**

In `src/vouch/proposals.py`, add near `referenced_by` (after it):

```python
def _reconstruct_deleted(
    target_kind: str, snapshot: dict[str, Any]
) -> Claim | Page | Entity | Relation:
    """Rebuild a typed model from a delete proposal's snapshot.

    Used only on the idempotent path (artifact already gone) so the approve
    surfaces still receive a `{kind, id}` result.
    """
    if target_kind == "claim":
        return Claim(**snapshot)
    if target_kind == "page":
        return Page(**snapshot)
    if target_kind == "entity":
        return Entity(**snapshot)
    return Relation(**snapshot)


def _approve_delete(
    store: KBStore, proposal: Proposal, *, approved_by: str
) -> Claim | Page | Entity | Relation:
    """Execute an approved DELETE proposal: remove the artifact + index rows.

    Re-checks references at approve time (they may have appeared since the
    proposal was filed). Idempotent: if the artifact is already gone, finalize
    the proposal without erroring.
    """
    payload = proposal.payload
    target_kind = str(payload["target_kind"])
    target_id = str(payload["id"])
    snapshot = dict(payload.get("snapshot") or {})
    getter = getattr(store, _DELETE_GETTERS[target_kind])
    try:
        artifact = getter(target_id)
    except ArtifactNotFoundError:
        return _reconstruct_deleted(target_kind, snapshot)
    refs = referenced_by(store, target_kind, target_id)
    if refs:
        raise ProposalError(
            f"cannot delete {target_kind} {target_id}: still referenced by "
            + ", ".join(refs)
        )
    deleter = getattr(store, f"delete_{target_kind}")
    deleter(target_id)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.deindex(conn, kind=target_kind, id=target_id)
    audit.log_event(
        store.kb_dir, event=f"{target_kind}.delete", actor=approved_by,
        object_ids=[target_id], data={"snapshot": snapshot},
    )
    return artifact
```

- [ ] **Step 3d: Add the `_payload_block_reason` DELETE branch**

In `_payload_block_reason`, add before the final `return None` (after the `ENTITY` branch, ~line 415):

```python
    elif proposal.kind == ProposalKind.DELETE:
        target_kind = str(payload.get("target_kind", ""))
        target_id = str(payload.get("id", ""))
        if target_kind not in _DELETE_KINDS:
            return f"invalid delete target_kind: {target_kind!r}"
        getter = getattr(store, _DELETE_GETTERS[target_kind])
        try:
            getter(target_id)
        except ArtifactNotFoundError:
            return None  # already gone → idempotent approve is fine
        refs = referenced_by(store, target_kind, target_id)
        if refs:
            return (
                f"cannot delete {target_kind} {target_id}: referenced by "
                + ", ".join(refs)
            )
```

- [ ] **Step 4: Run the whole delete suite + full CI gate**

Run: `.venv/bin/python -m pytest tests/test_delete.py -q`
Expected: PASS.

Run: `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings`
Expected: PASS (no regressions).

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Commit**

Write to `scratchpad/msg5.txt`:

```
feat(delete): execute delete proposals through approve()

approve() now removes the artifact + index rows for a DELETE proposal,
re-checks references at the gate, logs a per-kind <kind>.delete audit event
with the snapshot, and is idempotent if the file is already gone. batch
precheck (check_approvable) reports a referenced target as unapprovable.
```

```bash
git add src/vouch/proposals.py tests/test_delete.py
git commit -F scratchpad/msg5.txt
```

---

### Task 6: Surface wiring (MCP + JSONL + CLI + METHODS) and parity

**Files:**
- Modify: `src/vouch/capabilities.py` (`METHODS`)
- Modify: `src/vouch/server.py` (import + `kb_propose_delete` tool)
- Modify: `src/vouch/jsonl_server.py` (import + `_h_propose_delete` + `HANDLERS`)
- Modify: `src/vouch/cli.py` (import + `propose-delete` command)
- Test: `tests/test_delete.py` (append surface tests)

**Interfaces:**
- Consumes: `proposals.propose_delete` (Task 4).
- Produces: `kb.propose_delete` reachable on all four surfaces; `METHODS` includes `"kb.propose_delete"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_delete.py`:

```python
from vouch import capabilities
from vouch.jsonl_server import handle_request


def test_method_registered_in_capabilities() -> None:
    assert "kb.propose_delete" in capabilities.METHODS


def test_jsonl_propose_delete_end_to_end(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", "kill via jsonl")
    resp = handle_request({
        "id": "r1",
        "method": "kb.propose_delete",
        "params": {"target_kind": "claim", "target_id": "c1"},
    })
    assert resp["ok"] is True, resp
    result = resp["result"]
    assert result["kind"] == "delete"
    assert result["status"] == "pending"
    assert result["proposal_id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_delete.py -k "method_registered or jsonl_propose_delete" -q`
Expected: FAIL — `"kb.propose_delete" not in METHODS` and the JSONL response carries an "unknown method" error.

- [ ] **Step 3a: Register in `METHODS`**

In `src/vouch/capabilities.py`, add after `"kb.propose_relation",`:

```python
    "kb.propose_relation",
    "kb.propose_delete",
```

- [ ] **Step 3b: MCP tool**

In `src/vouch/server.py`, add `propose_delete` to the `from .proposals import (...)` block (keep it alphabetical-ish, after `propose_claim`):

```python
    propose_claim,
    propose_delete,
    propose_entity,
```

Then add a tool alongside the other propose tools (near `kb_propose_relation`):

```python
@mcp.tool()
def kb_propose_delete(
    target_kind: str, target_id: str, rationale: str | None = None
) -> dict[str, Any]:
    """Propose hard-deleting a durable artifact (claim/page/entity/relation).

    Files a PENDING delete request that a *different* reviewer approves via
    kb.approve. Refused if the target is still referenced by another artifact.
    """
    pr = propose_delete(
        _store(), target_kind=target_kind, target_id=target_id,
        proposed_by=_agent(), rationale=rationale,
    )
    return {"proposal_id": pr.id, "status": pr.status.value, "kind": pr.kind.value}
```

- [ ] **Step 3c: JSONL handler**

In `src/vouch/jsonl_server.py`, add `propose_delete` to the `from .proposals import (...)` block (after `propose_claim`):

```python
    propose_claim,
    propose_delete,
    propose_entity,
```

Add the handler near `_h_propose_relation`:

```python
def _h_propose_delete(p: dict) -> dict:
    pr = propose_delete(
        _store(),
        target_kind=p["target_kind"],
        target_id=p["target_id"],
        rationale=p.get("rationale"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    return {
        "proposal_id": pr.id,
        "status": pr.status.value,
        "kind": pr.kind.value,
        "dry_run": bool(p.get("dry_run", False)),
    }
```

Register it in `HANDLERS` after `"kb.propose_relation": _h_propose_relation,`:

```python
    "kb.propose_relation": _h_propose_relation,
    "kb.propose_delete": _h_propose_delete,
```

- [ ] **Step 3d: CLI command**

In `src/vouch/cli.py`, add `propose_delete` to the first `from .proposals import (...)` block (after `propose_claim`):

```python
    propose_claim,
    propose_delete,
    propose_entity,
```

Add the command near the `supersede`/`archive` lifecycle commands (~line 1884):

```python
@cli.command(name="propose-delete")
@click.argument(
    "target_kind",
    type=click.Choice(["claim", "page", "entity", "relation"]),
)
@click.argument("target_id")
@click.option("--rationale", default=None, help="why this should be deleted")
def propose_delete_cmd(
    target_kind: str, target_id: str, rationale: str | None
) -> None:
    """File a review-gated hard-delete request for an artifact.

    A different reviewer approves it with `vouch approve <id>`. Refused if the
    target is still referenced (supersede the claim instead, usually).
    """
    store = _load_store()
    with _cli_errors():
        pr = propose_delete(
            store, target_kind=target_kind, target_id=target_id,
            proposed_by=_whoami(), rationale=rationale,
        )
    click.echo(f"filed delete proposal {pr.id} for {target_kind} {target_id}")
```

- [ ] **Step 4: Run the surface tests + full CI gate**

Run: `.venv/bin/python -m pytest tests/test_delete.py tests/test_capabilities.py -q`
Expected: PASS (capabilities parity holds).

Run: `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings`
Expected: PASS.

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors.

- [ ] **Step 5: Manual smoke via CLI (verification)**

Run:
```bash
cd "$(mktemp -d)" && .venv/bin/vouch init . >/dev/null 2>&1 || true
```
Then in a scratch KB: register a source, propose+approve a claim, then:
```bash
vouch propose-delete claim <claim-id>
vouch list-pending          # shows the delete proposal
vouch approve <proposal-id> --reason "junk"
vouch read-claim <claim-id> # expect: not found
```
Expected: the claim file is gone and `vouch audit` shows a `claim.delete` event. (Skip if a scratch KB is inconvenient; the pytest suite already exercises this end-to-end.)

- [ ] **Step 6: Commit**

Write to `scratchpad/msg6.txt`:

```
feat(delete): expose kb.propose_delete across all surfaces

register the method on the MCP tool surface, the JSONL handler map, the
METHODS list, and the CLI (`vouch propose-delete <kind> <id>`). approval
stays on the existing kb.approve. capabilities parity test passes.
```

```bash
git add src/vouch/capabilities.py src/vouch/server.py src/vouch/jsonl_server.py src/vouch/cli.py tests/test_delete.py
git commit -F scratchpad/msg6.txt
```

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:
- object model (`ProposalKind.DELETE`, payload+snapshot) → Task 3 (enum) + Task 4 (payload).
- reference matrix → Task 3 (`referenced_by`).
- propose flow → Task 4.
- approve flow (skip overwrite guard, re-check refs, idempotent, per-kind audit) → Task 5.
- batch precheck → Task 5 (`_payload_block_reason`).
- storage layer → Task 1.
- index layer (`deindex`) → Task 2.
- four surfaces + parity → Task 6.
- reject/list/expire "no new code" → verified by the full-suite run in Tasks 5–6; nothing to build.
- out of scope (source/evidence, cascade, undo, web-ui) → not implemented, by design.

**2. Placeholder scan** — no TBD/TODO; every code step shows complete code; every test step shows real assertions.

**3. Type consistency** — `referenced_by(store, target_kind, target_id) -> list[str]`, `propose_delete(..., target_kind, target_id, ...) -> Proposal`, `_approve_delete(...) -> Claim|Page|Entity|Relation`, `deindex(conn, *, kind, id)`, `delete_<kind>(id)` names are used identically across the tasks that define and consume them. Payload keys `target_kind` / `id` / `snapshot` are consistent between `propose_delete`, `_approve_delete`, and `_payload_block_reason`. Surface params `target_kind` / `target_id` are consistent across MCP/JSONL/CLI.

**Note for the implementer:** the JSONL/MCP/CLI params are `target_id` (not `id`) at the surface, but the *payload* key is `id`. That mapping is intentional — `propose_delete`'s `target_id` argument becomes `payload["id"]`. Don't "fix" one to match the other.
