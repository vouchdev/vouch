# Audit event vocabulary

Every mutation in a vouch KB MUST emit exactly one `AuditEvent` to
`.vouch/audit.log.jsonl`. This document is the canonical list of event
names.

Event names are **dotted**: `<noun>.<verb>`. Implementations MUST use
the names below; adding new mutations means adding a new entry to this
document, not making one up.

---

## Schema

Each line is a JSON object:

```json
{
  "id": "01HM3K…",
  "event": "claim.create",
  "actor": "claude-code",
  "created_at": "2026-05-17T10:00:00Z",
  "object_ids": ["auth-uses-jwt"],
  "dry_run": false,
  "reversible": true,
  "data": {"proposal_id": "p-…", "session_id": "s-…"}
}
```

- `actor` — value of `VOUCH_AGENT` at the time of the call (or
  `unknown` if unset).
- `object_ids` — primary objects affected; usually one entry.
- `reversible` — `true` if a corresponding reverse exists
  (`claim.archive` is reversible by re-creating; `claim.redact` is
  not).

---

## Source events

| event | fired by | object_ids | notes |
|---|---|---|---|
| `source.register` | `kb.register_source*` | source sha256 | `data.deduplicated: bool` |
| `source.verify` | `kb.source_verify` | source sha256(s) | not strictly a mutation; logged anyway for traceability |

## Evidence events

| event | fired by | object_ids |
|---|---|---|
| `evidence.create` | implicit during claim proposal that includes new evidence | evidence id |

## Proposal events

| event | fired by | object_ids | notes |
|---|---|---|---|
| `proposal.create` | `kb.propose_*` | proposal id | `data.kind, data.payload_summary` |
| `proposal.approve` | `kb.approve` | proposal id | `data.object_id` (the durable artifact) |
| `proposal.reject` | `kb.reject` | proposal id | `data.reason` (required) |
| `proposal.expire` | GC | proposal id | implementations that GC pending |

## Claim events

| event | fired by | object_ids | notes |
|---|---|---|---|
| `claim.create` | `kb.approve` of a claim proposal | claim id | follows `proposal.approve` |
| `claim.supersede` | `kb.supersede` | `[old, new]` | both ids in `object_ids` |
| `claim.contradict` | `kb.contradict` | `[a, b]` | symmetric |
| `claim.archive` | `kb.archive` | claim id | `reversible: true` |
| `claim.confirm` | `kb.confirm` | claim id | updates `last_confirmed_at` |
| `claim.cite` | `kb.cite` | claim id | `data.evidence_added` |
| `claim.redact` | (admin) | claim id | `reversible: false` |

## Page events

| event | fired by | object_ids |
|---|---|---|
| `page.create` | `kb.approve` of a page proposal | page id |
| `page.update` | (direct edit; not via proposal) | page id |
| `page.archive` | `kb.archive` on a page | page id |

`page.update` is intentionally direct: pages are *maintained* artifacts.
The change is still audited.

## Entity / Relation events

| event | fired by | object_ids |
|---|---|---|
| `entity.create` | `kb.approve` of an entity proposal | entity id |
| `relation.create` | `kb.approve` of a relation proposal | relation id |
| `relation.delete` | (admin) | relation id |

## Session events

| event | fired by | object_ids |
|---|---|---|
| `session.start` | `kb.session_start` | session id |
| `session.end` | `kb.session_end` | session id |
| `session.crystallize` | `kb.crystallize` | session id, page id (if produced) |

## Maintenance events

| event | fired by | object_ids | notes |
|---|---|---|---|
| `index.rebuild` | `kb.index_rebuild` | — | `data.indexed: int` |
| `bundle.export` | `kb.export` | — | `data.path, data.files` |
| `bundle.import` | `kb.import_apply` | bundle file path | `data.imported, data.skipped, data.overwritten` |

---

## Forward compatibility

- Consumers MUST tolerate unknown event names — log them, don't crash.
- New events SHOULD be added in `<noun>.<verb>` form using nouns from
  the object model.
- Once an event name appears in a stable release, it MUST NOT be
  renamed or reshaped within the same major version.
