# `kb.*` method reference

This is the per-method reference for the surface declared in
[SPEC.md §5](../SPEC.md#5-kb-method-surface). Shapes are JSON; field
types follow JSON Schema conventions.

Implementations expose these methods on every transport listed by
`kb.capabilities.transports`. MCP exposes them as tools named with an
underscore (`kb_search`, `kb_propose_claim`, …); the JSONL transport
uses the dotted form (`kb.search`).

---

## Read methods

### `kb.capabilities`

**Params:** none.
**Result:** [Capabilities](../schemas/capabilities.schema.json).

### `kb.status`

**Params:** none.
**Result:**
```json
{
  "root": "/abs/path/.vouch",
  "counts": {"claims": 12, "pages": 3, "sources": 9, "entities": 5, "relations": 4},
  "pending": 2,
  "last_audit_at": "2026-05-17T10:00:00Z"
}
```

### `kb.search`

**Params:**
- `query` *(string, required)*
- `limit` *(int, default 10)*
- `kinds` *(array of `claim`/`page`/`entity`/`source`, optional)*

**Result:** array of `{kind, id, snippet, score, backend}`.

### `kb.context`

**Params:**
- `task` *(string, required)* — natural-language task description
- `max_chars` *(int, default 4000)*
- `min_items` *(int, default 0)*
- `require_citations` *(bool, default false)*

**Result:** [ContextPack](../schemas/context-pack.schema.json).

### `kb.read_{page,claim,entity,relation}`

**Params:** `{ "id": str }`.
**Result:** the matching object, or `null` if not found.

### `kb.list_{pages,claims,entities,relations,sources,pending}`

**Params:** `{ "limit": int?, "offset": int?, "filter": object? }`.
**Result:** array of objects of the requested kind.

---

## Source intake

### `kb.register_source`

**Params:**
- `content` *(string, required)* — UTF-8 text content
- `type` *(SourceType, default `file`)*
- `locator` *(string, required)*
- `title` *(string, optional)*
- `media_type` *(string, optional)*
- `tags` *(array of string, optional)*

**Result:** `{ "id": "<sha256>", "deduplicated": bool }`.

### `kb.register_source_from_path`

**Params:**
- `path` *(string, required)* — absolute or repo-relative path
- `type` *(SourceType, optional)*
- `title` *(string, optional)*

**Result:** same as `register_source`.

### `kb.source_verify`

**Params:** `{ "id": str? }` — omit to verify all.
**Result:** `{ "ok": bool, "issues": [{"id": str, "kind": str, "detail": str}] }`.

---

## Write (gated)

### `kb.propose_claim`

**Params:**
- `text` *(string, required)*
- `evidence` *(array of source/evidence ids, required, non-empty)*
- `type` *(ClaimType, default `observation`)*
- `confidence` *(float `0..1`, default 0.7)*
- `entities` *(array of entity ids, optional)*
- `scope` *(Scope, optional)*
- `tags` *(array of string, optional)*
- `session_id` *(string, optional)*
- `rationale` *(string, optional)* — *why* this claim, recorded on the proposal
- `dry_run` *(bool, default false)*

**Result:** `{ "proposal_id": str, "claim_id": str, "valid": bool, "errors": [str] }`.

When `dry_run`, no file is written; only validation runs.

### `kb.propose_page`

**Params:**
- `title` *(string, required)*
- `body` *(string, optional, markdown)*
- `type` *(PageType, default `concept`)*
- `claims` *(array of claim ids, optional)*
- `entities` *(array of entity ids, optional)*
- `sources` *(array of source ids, optional)*
- `tags` *(array of string, optional)*
- `dry_run` *(bool, default false)*

**Result:** `{ "proposal_id": str, "page_id": str, "valid": bool, "errors": [str] }`.

### `kb.propose_entity`

**Params:**
- `name` *(string, required)*
- `type` *(EntityType, required)*
- `aliases` *(array of string, optional)*
- `description` *(string, optional)*
- `dry_run` *(bool, default false)*

**Result:** `{ "proposal_id": str, "entity_id": str, "valid": bool, "errors": [str] }`.

### `kb.propose_relation`

**Params:**
- `source` *(id, required)*
- `relation` *(RelationType, required)*
- `target` *(id, required)*
- `confidence` *(float, default 0.7)*
- `evidence` *(array, optional)*
- `dry_run` *(bool, default false)*

**Result:** `{ "proposal_id": str, "relation_id": str, "valid": bool, "errors": [str] }`.

---

## Decisions

### `kb.approve`

**Params:**
- `proposal_id` *(string, required)*
- `reason` *(string, optional)*
- `approver` *(string, optional)* — overrides the env-derived actor

**Result:** `{ "ok": bool, "object_id": str, "object_kind": str }`.

### `kb.reject`

**Params:**
- `proposal_id` *(string, required)*
- `reason` *(string, required)*

**Result:** `{ "ok": bool }`.

---

## Lifecycle

| method | params | effect |
|---|---|---|
| `kb.supersede` | `{old_id, new_id, reason?}` | `old.status = superseded`; `old.superseded_by = new` |
| `kb.contradict` | `{a_id, b_id, reason?}` | adds A↔B to each other's `contradicts` |
| `kb.archive` | `{id, reason?}` | `status = archived` |
| `kb.confirm` | `{id}` | sets `last_confirmed_at = now` |
| `kb.cite` | `{id, evidence}` | appends an evidence/source id to the claim |

All emit a corresponding audit event.

---

## Sessions

### `kb.session_start`

**Params:** `{ "task": str?, "note": str? }`.
**Result:** `{ "session_id": str }`.

### `kb.session_end`

**Params:** `{ "session_id": str }`.
**Result:** `{ "ok": bool, "proposals": int }`.

### `kb.crystallize`

**Params:** `{ "session_id": str, "no_page": bool? }`.
**Result:** `{ "page_id": str?, "claims_promoted": int }`.

Promotes the durable parts of a session into a session-summary page and
flags promotable claims for review.

---

## Maintenance

### `kb.index_rebuild`

**Params:** none.
**Result:** `{ "ok": bool, "indexed": int }`.

### `kb.lint`

**Params:** `{ "stale_days": int? }`.
**Result:** `{ "issues": [{"id": str, "kind": str, "severity": "warn"|"error", "message": str}] }`.

### `kb.doctor`

**Params:** none.
**Result:** like `lint` plus source-hash verification, dangling-reference checks, and config-drift.

### `kb.audit`

**Params:** `{ "tail": int?, "filter": {"event": str?, "actor": str?}? }`.
**Result:** array of `AuditEvent`.

### `kb.export`

**Params:** `{ "out": str }`.
**Result:** `{ "path": str, "files": int, "bytes": int }`.

### `kb.export_check`

**Params:** `{ "path": str }`.
**Result:** `{ "ok": bool, "issues": [str] }`.

### `kb.import_check`

**Params:** `{ "path": str }`.
**Result:** `{ "new": [str], "conflict": [str], "identical": [str] }`.

### `kb.import_apply`

**Params:** `{ "path": str, "on_conflict": "skip"|"overwrite"|"fail" }`.
**Result:** `{ "imported": int, "skipped": int, "overwritten": int }`.

---

## Error codes (JSONL transport)

| code | meaning |
|---|---|
| `method_not_found` | unknown `method` |
| `missing_param` | required param absent or null |
| `invalid_request` | malformed envelope or invalid params |
| `internal_error` | unexpected server-side failure |

MCP transports use MCP-native error semantics; the dotted-name mapping
is documented in [transports.md](transports.md).
