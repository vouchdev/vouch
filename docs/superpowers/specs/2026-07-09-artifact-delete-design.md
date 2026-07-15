# review-gated artifact delete

status: draft
date: 2026-07-09
scope: add a hard-delete path for durable artifacts (claim, page, entity,
relation) that routes through the review gate.

## why

vouch has a soft-delete today — `kb.archive` flips a claim to
`status=archived`, keeps the file and every link. there is no way to
*remove* an artifact. junk that should never have been written (a
mis-slugged claim, a duplicate entity, an auto-extracted edge that is
plain wrong) can only be hidden, never erased. this leaves the on-disk
kb and its diffs cluttered with records the maintainer has explicitly
judged to be garbage.

hard delete fills that gap. it is deliberately the most consequential
write in the system, so it is held to the strongest control vouch has:
the review gate.

## decisions (locked)

1. **hard delete.** the artifact's file is physically removed from
   `.vouch/`, dropped from the derived index, and the audit event is the
   only in-place trace. git history still holds the file — delete is not
   "unrecoverable", it is "gone from the working kb".
2. **through the review gate.** deletion is a proposal. an agent (or
   human) files `kb.propose_delete`; a *different* reviewer approves it
   via the existing `kb.approve`. no direct-mutation path exists, in
   keeping with the north star: every write is reviewed, and no parallel
   path bypasses `proposals.approve()`.
3. **block if referenced.** a delete is refused — at propose time as a
   friendly error, and again at approve time as the authoritative gate —
   if any other artifact points at the target. the maintainer must
   supersede or remove the referring artifacts first. this preserves
   vouch's existing no-dangling-refs invariant (the same invariant the
   `put_*` write guards and `kb.lint`/`kb.doctor` enforce).
4. **all four durable kinds.** claim, page, entity, and relation are all
   deletable through one generic path. (sources and evidence are out of
   scope for this pass — see "out of scope".)

## object model

a delete is represented as a new proposal kind, not smuggled through an
existing one.

* `ProposalKind.DELETE = "delete"` (new enum member in `models.py`).
* payload shape:

  ```yaml
  target_kind: claim | page | entity | relation
  id: <artifact id>
  snapshot: { ...full model_dump of the artifact at propose time... }
  ```

`target_kind` is explicit rather than inferred: claim / page / entity
slugs can collide across kinds, so the caller must name the kind. the
`snapshot` is the full serialized artifact captured at propose time; it
makes the resulting `decided/<id>.yaml` proposal a tombstone record of
exactly what was removed, and is copied into the audit event. the
snapshot is for the reviewer's context and the audit trail only — the
actual delete re-reads live state (see "approve").

## the reference matrix

"referenced" means an *inbound* pointer from another artifact. outbound
refs (what the target itself points at) never block a delete: removing
the holder simply drops its own pointers, and the things it pointed at
survive.

| deleting a… | blocked if referenced (inbound) by | storage remove | index remove |
|---|---|---|---|
| **claim** | page `claims[]`; relation `source`/`target`; another claim's `supersedes` / `superseded_by` / `contradicts` | unlink `claims/<id>.yaml` | `claims_fts`, `embedding_index(claim,id)`, `prov_edges` touching id |
| **page** | relation `source`/`target` (pages are valid relation endpoints) | unlink `pages/<id>.md` | `pages_fts`, `embedding_index(page,id)`, `prov_edges` touching id |
| **entity** | claim `entities[]`; page `entities[]`; relation `source`/`target` | unlink `entities/<id>.yaml` | `entities_fts`, `embedding_index(entity,id)`, `prov_edges` touching id |
| **relation** | *nothing* — an edge has no inbound refs → always deletable | unlink `relations/<id>.yaml` | `embedding_index(relation,id)`, `prov_edges` touching id (relations are embedded on write but never in FTS) |

a consequence worth stating plainly: to delete a page or entity that a
relation points at, you first delete those inbound relations (relations
delete freely, having no inbound refs of their own). that chain is the
"block if referenced" contract behaving as designed — the maintainer
never silently orphans an edge.

## flow

### propose

`proposals.propose_delete(store, *, target_kind, target_id, proposed_by,
rationale=None, session_id=None, dry_run=False) -> Proposal`

1. validate `target_kind` is one of the four kinds.
2. resolve the artifact via the matching getter; raise `ProposalError`
   if it does not exist.
3. run `referenced_by(store, target_kind, target_id)`. if it returns a
   non-empty list, raise `ProposalError` naming the referrers. for a
   claim, the message also points at `supersede` as the usual intended
   operation.
4. snapshot = `artifact.model_dump(mode="json")`.
5. file via the existing `_file_proposal` with `kind=DELETE`. dry-run
   behaves exactly as the other `propose_*` paths (no disk write, audit
   `proposal.delete.dry_run`).

### approve

a new branch in `proposals.approve()`, dispatched on
`proposal.payload["target_kind"]`.

* **skip `_ensure_no_existing_artifact`.** that guard refuses to write
  over an existing artifact; a delete is the inverse — the artifact must
  exist. the guard is currently applied to every non-page kind, so the
  approve path is restructured to exempt DELETE.
* **re-resolve the artifact.** if it is already gone, treat the delete as
  already done: skip the unlink, but still mark the proposal decided and
  write the audit event. this makes approve idempotent under a
  crash-retry between the unlink and `move_proposal_to_decided`.
* **re-run `referenced_by`.** refs may have appeared since propose time
  (mirroring how page approval re-validates the page kind at the gate).
  if now referenced, raise `ProposalError` — the delete is blocked, the
  proposal stays pending.
* **remove.** call `storage.delete_<kind>(id)` then
  `index_db.deindex(conn, kind, id)`.
* **audit.** write a per-kind `claim.delete` / `page.delete` /
  `entity.delete` / `relation.delete` event carrying the snapshot, plus
  the generic `proposal.delete.approve` the approve path already emits.
* mark the proposal `APPROVED` and move it to `decided/` exactly as every
  other kind does.

### reject / list / expire

no new code. `kb.reject`, `kb.list_pending`, `kb.triage_pending`, and
`expire_pending` are all kind-agnostic — a rejected delete simply leaves
the target untouched, and a stale delete proposal expires like any other.

### batch precheck

`check_approvable` / `_payload_block_reason` gain a DELETE branch: verify
the target exists and is unreferenced, so `vouch approve a b` stays
all-or-nothing when a delete is in the batch.

### shared helper

`referenced_by(store, target_kind, target_id) -> list[str]` implements
the matrix. returns human-readable referrer descriptions (e.g.
`page 'foo'`, `relation x--rel--abc`). used by propose, approve, and
`check_approvable` so the three paths never drift.

## storage layer

`storage.py` gains four pure-I/O unlink methods mirroring the existing
source-unlink pattern — no business logic, no ref checks (those live in
`proposals`):

* `delete_claim(claim_id)` → unlink `claims/<id>.yaml`
* `delete_page(page_id)` → unlink `pages/<id>.md`
* `delete_entity(entity_id)` → unlink `entities/<id>.yaml`
* `delete_relation(relation_id)` → unlink `relations/<id>.yaml`

each raises `ArtifactNotFoundError` if the file is absent (the approve
path treats that as the idempotent already-deleted case).

## index layer

`index_db.py` gains `deindex(conn, *, kind, id)`:

* `kind in {claim, page, entity}` → `DELETE FROM <kind>s_fts WHERE id=?`
  (relations have no FTS table).
* all four kinds → `DELETE FROM embedding_index WHERE kind=? AND id=?`.
  every `put_*` calls `_embed_and_store`, so an embedding row can exist
  for any kind, relations included (present only when the embeddings
  extra is installed; the delete is a harmless no-op otherwise).
* all kinds → remove `prov_edges` rows referencing the id (as `src_id`
  or `dst_id`). `prov_edges` is otherwise derived and rebuildable via
  `kb.provenance_rebuild`; removing the touched rows inline keeps
  `state.db` consistent without a full rebuild.

## surfaces (four registration sites + test)

one new method, `kb.propose_delete`, mirrored across every surface per
the "when you add a new kb.\* method" checklist in CLAUDE.md:

1. **MCP** — `kb_propose_delete(target_kind, target_id, rationale=None)`
   in `server.py`.
2. **JSONL** — `_h_propose_delete` + `HANDLERS["kb.propose_delete"]` in
   `jsonl_server.py`.
3. **METHODS** — `"kb.propose_delete"` in `capabilities.py`.
4. **CLI** — `vouch propose-delete <target_kind> <target_id>` in
   `cli.py`, echoing the filed proposal id. sits alongside the existing
   `supersede` / `archive` / `contradict` lifecycle commands.

no new approve/reject method — the human uses the existing
`vouch approve <pid>` / `kb.approve`.

`test_capabilities` enforces method-list parity across all four; a new
`tests/test_delete.py` covers the behavior.

## testing

`tests/test_delete.py`:

* propose + approve happy path for each of the four kinds; assert the
  file is gone, the index row is gone, and the audit event is written.
* block-if-referenced for each kind per the matrix (claim cited by a
  page; entity in a claim's `entities`; page as a relation endpoint;
  etc.); assert `propose_delete` raises and nothing is written.
* the block is re-checked at approve: propose a valid delete, then add a
  referring artifact, then approve → raises, target survives, proposal
  stays pending.
* relation deletes freely even when its endpoints exist.
* idempotent approve: delete the underlying file out from under a pending
  approved-in-flight proposal → approve still finalizes cleanly.
* forbidden self-approval still applies (proposer ≠ approver, unless
  `review.approver_role: trusted-agent`).
* `check_approvable` returns the block reason for a referenced target so
  the batch path is all-or-nothing.
* dry-run writes nothing and returns the would-be proposal id.

the ci gate is unchanged: `pytest tests/ -q --ignore=tests/embeddings`,
`mypy src`, `ruff check src tests`.

## out of scope

* **source / evidence delete.** sources have their own directory shape
  (`_source_dir`, a directory not a single file) and different reference
  semantics (claims cite them). deferred; the generic `target_kind`
  dispatch leaves room to add them without reshaping the payload.
* **cascade delete.** explicitly rejected in favor of "block if
  referenced" — one approval never mutates a second reviewed artifact.
* **undo / restore.** git history is the recovery path; the snapshot in
  `decided/` is the record. no in-product un-delete in this pass.
* **web review-ui rendering.** the review console lives in the separate
  vouch-ui repo. it will need a row renderer for the DELETE kind (show
  "delete <kind> <id>" with the snapshot); tracked there, not built here.

## north-star check

deletion is a proposal approved through `proposals.approve()`. there is
no direct mutation and no parallel data path. the review gate remains the
single chokepoint for every write, destructive ones included.
