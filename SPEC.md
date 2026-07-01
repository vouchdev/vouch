# vouch specification (draft)

**Version:** 2026-05-21 · **Status:** draft for 0.x. Pinned format expected at 1.0.
**Audience:** people writing alternative implementations of the `kb.*`
surface, people building adapters, people auditing what lands on disk.

Dated snapshots of this document live under [spec/](spec/) — e.g.
[spec/2026-05-21/SPEC.md](spec/2026-05-21/SPEC.md) — so the protocol can
evolve while older versions stay readable. The file you are reading is
the *latest*; treat dated copies as immutable history.

This is the canonical written description of:

1. The on-disk layout of a `.vouch/` repository.
2. The object model (Source, Evidence, Claim, Entity, Relation, Page,
   Session, Proposal, AuditEvent).
3. The `kb.*` method surface, with parameter and result shapes.
4. The review-gate state machine (proposal → decision → durable artifact).
5. The audit log, bundle format, and capabilities descriptor.

For implementation-level documentation see [docs/](docs/). For the
pydantic source of truth see [src/vouch/models.py](src/vouch/models.py).
For JSON Schema versions see [schemas/](schemas/).

This document uses RFC 2119 keywords (MUST / SHOULD / MAY).

---

## 1. On-disk layout

A vouch knowledge base is a directory named `.vouch/` at any path the
host chooses (typically the repo root). Every file inside is plain text
(YAML, markdown, or JSONL) except `state.db`.

```
.vouch/
├── config.yaml                 # KB settings
├── .gitignore                  # ignores proposed/, state.db
├── audit.log.jsonl             # append-only audit log (committed)
├── state.db                    # SQLite FTS5 index (derived; not committed)
├── claims/<id>.yaml            # durable, reviewed claims
├── pages/<id>.md               # markdown with YAML frontmatter
├── sources/<sha>/meta.yaml     # source metadata
├── sources/<sha>/content       # captured bytes (optional)
├── entities/<id>.yaml          # graph nodes
├── relations/<id>.yaml         # graph edges
├── evidence/<id>.yaml          # span pointers into sources
├── sessions/<id>.yaml          # agent session records
├── proposed/<id>.yaml          # pending proposals (gitignored, local)
└── decided/<id>.yaml           # accepted/rejected (committed for audit)
```

Implementations MUST treat files on disk as the source of truth.
`state.db` is a derivable cache; any operation MUST be reproducible by
deleting `state.db` and rebuilding it from the files.

### 1.1 `config.yaml`

```yaml
version: "0.1"
kb_name: my-project
agent: claude-code          # default actor if VOUCH_AGENT is unset
retrieval:
  backend: fts5             # fts5 | substring
  fts5_porter: true
review:
  require_citations: true   # claims without citations fail validation
  approver_role: human      # human | trusted-agent
```

### 1.2 ID rules

- **Source ids** MUST be the lowercase hex sha256 of the captured bytes
  (64 hex chars). Re-registering identical bytes MUST be idempotent.
- **Claim / Page / Entity / Relation / Evidence ids** are kebab-case
  slugs unique within their kind. Implementations MAY derive them from
  user-provided titles but MUST guarantee uniqueness.
- **Session ids** SHOULD be ULIDs or timestamp-prefixed slugs so that
  sort order matches chronology.
- **Proposal ids** MUST be unique across `proposed/` and `decided/`
  combined — a proposal keeps its id when it moves between directories.

---

## 2. Object model

The authoritative pydantic definitions are in
[src/vouch/models.py](src/vouch/models.py). The shapes below are
informative.

### 2.1 Source

Immutable input material. Content-addressed by sha256.

| field        | type                | required | notes                           |
|--------------|---------------------|----------|---------------------------------|
| `id`         | hex sha256          | yes      | 64 lowercase hex chars          |
| `type`       | enum (see §2.1.1)   | yes      | default `file`                  |
| `locator`    | string              | yes      | path/URL/commit/etc.            |
| `title`      | string              | no       |                                 |
| `hash`       | hex sha256          | no       | mirrors `id`                    |
| `immutable`  | bool                | no       | default `true`                  |
| `scope`      | enum                | no       | `private`/`project`/`team`/`public` |
| `byte_size`  | int                 | no       |                                 |
| `media_type` | string              | no       | default `text/plain`            |
| `created_at` | ISO-8601 datetime   | yes      | UTC                             |
| `metadata`   | object              | no       | implementation-defined          |
| `tags`       | array of string     | no       |                                 |

#### 2.1.1 SourceType

`file`, `url`, `transcript`, `message`, `commit`, `issue`, `screenshot`,
`pdf`, `audio`, `video`, `folder`.

### 2.2 Evidence

A span pointer into a Source. Claims cite Evidence ids *or* Source ids;
both are valid citations.

| field         | type     | required | notes                              |
|---------------|----------|----------|------------------------------------|
| `id`          | slug     | yes      |                                    |
| `source_id`   | sha256   | yes      | must reference an existing source  |
| `source_type` | enum     | no       | mirrored from source for fast filter |
| `locator`     | string   | yes      | `L10-L20`, `t=00:14:23`, `#sec-3`  |
| `quote`       | string   | no       | extracted span text                |
| `hash`        | sha256   | no       | hash of the quoted span            |
| `created_at`  | datetime | yes      | UTC                                |

### 2.3 Claim

The smallest assertion that can be cited, contradicted, superseded, or
archived.

| field                | type           | required | notes                         |
|----------------------|----------------|----------|-------------------------------|
| `id`                 | slug           | yes      |                               |
| `text`               | string         | yes      | one-sentence assertion        |
| `type`               | enum (§2.3.1)  | yes      |                               |
| `status`             | enum (§2.3.2)  | yes      |                               |
| `confidence`         | float `0..1`   | yes      | default `0.7`                 |
| `evidence`           | array of id    | yes      | MUST be non-empty (see §3)    |
| `entities`           | array of id    | no       |                               |
| `supersedes`         | array of id    | no       |                               |
| `superseded_by`      | id             | no       | set when superseded           |
| `contradicts`        | array of id    | no       |                               |
| `scope`              | enum           | no       |                               |
| `tags`               | array of str   | no       |                               |
| `created_at`         | datetime       | yes      |                               |
| `updated_at`         | datetime       | yes      |                               |
| `last_confirmed_at`  | datetime       | no       |                               |
| `approved_by`        | string         | no       | actor who ran `kb.approve`    |

#### 2.3.1 ClaimType

`fact`, `decision`, `preference`, `workflow`, `observation`, `question`,
`warning`.

#### 2.3.2 ClaimStatus

`working`, `actionable`, `stable`, `contested`, `superseded`, `archived`,
`redacted`.

### 2.4 Entity / Relation

Entities are typed named things; Relations are typed edges.

EntityType: `person`, `project`, `repo`, `company`, `concept`,
`decision`, `workflow`, `file`, `api`, `incident`, `source`, `agent`,
`tool`, `team`, `system`.

RelationType: `uses`, `depends_on`, `contradicts`, `supersedes`,
`supports`, `caused_by`, `owned_by`, `derived_from`, `similar_to`,
`blocks`, `implements`, `references`.

### 2.5 Page

Maintained markdown with YAML frontmatter on disk. Body is GFM markdown.

PageType: `entity`, `concept`, `decision`, `workflow`, `session`,
`index`, `log`, `report`, `source-summary`.

PageStatus: `draft`, `active`, `stale`, `archived`.

### 2.6 Session

A work block. Bundles the proposals an agent produced within one session
so `kb.crystallize` can promote the durable parts.

### 2.7 Proposal

vouch's review-gate primitive. See §4.

### 2.8 AuditEvent

One line in `audit.log.jsonl` per mutation. `event` is a dotted verb:
`source.register`, `proposal.create`, `proposal.approve`,
`proposal.reject`, `claim.supersede`, `claim.archive`, `bundle.import`,
etc. The complete vocabulary is defined in
[spec/audit-vocabulary.md](spec/audit-vocabulary.md).

---

## 3. Validation rules

These rules MUST be enforced server-side before a proposal can be
created and before any approved artifact is written to disk:

1. **Every Claim MUST carry at least one citation** (Source id or
   Evidence id). Claims without citations are a validation error, not
   a warning. Implementations MAY relax this for `working`-status
   claims if `review.require_citations: false` in `config.yaml`.
2. **Every Evidence MUST reference an existing Source.** Dangling
   evidence is rejected.
3. **Source ids MUST be the sha256 of their content.** If the captured
   bytes don't hash to the id, the source is rejected.
4. **Supersession is directional.** If A `supersedes` B then B's status
   becomes `superseded` and `B.superseded_by` is set to A. Cycles are
   rejected.
5. **Contradiction is symmetric.** If A `contradicts` B then B also
   gains A in its `contradicts` list.
6. **Approval is once-only.** A proposal in `decided/` cannot be moved
   back to `proposed/`. Re-proposing creates a new proposal id.

---

## 4. Review gate state machine

```
                  kb.propose_*                 kb.approve
   agent  ────────────────────────►  proposed/  ──────────►  durable artifact
                                        │                    (claims/, pages/, …)
                                        │     kb.reject
                                        └───────────────►   decided/<id>.yaml
                                                            (status: rejected)
```

- `kb.propose_*` writes the proposal to `proposed/<id>.yaml` and emits
  an audit event `proposal.create`.
- `kb.approve <id>` moves `proposed/<id>.yaml` to `decided/<id>.yaml`
  (status: `approved`) and writes the corresponding durable artifact
  to its kind-specific directory.
- `kb.reject <id>` moves the proposal to `decided/<id>.yaml`
  (status: `rejected`) and does *not* write a durable artifact.
- `proposed/` MUST be gitignored. `decided/` MUST be committed.
- Proposals cannot mutate existing durable artifacts; they can only
  *create*. Mutating durable artifacts uses the lifecycle methods
  (`kb.supersede`, `kb.contradict`, `kb.archive`, `kb.confirm`,
  `kb.cite`), each of which is directly audited.

The full state diagram and edge cases are in
[spec/review-gate.md](spec/review-gate.md).

---

## 5. `kb.*` method surface

A conforming implementation MUST expose the following methods. The
canonical names appear in `kb.capabilities`. Parameter and result
shapes are defined in [spec/methods.md](spec/methods.md).

### 5.1 Read (unrestricted)

`kb.capabilities`, `kb.status`, `kb.search`, `kb.context`,
`kb.read_page`, `kb.read_claim`, `kb.read_entity`, `kb.read_relation`,
`kb.list_pages`, `kb.list_claims`, `kb.list_entities`,
`kb.list_relations`, `kb.list_sources`, `kb.list_pending`.

### 5.2 Source intake (not gated)

`kb.register_source`, `kb.register_source_from_path`, `kb.source_verify`.

Evidence is harmless and de-duplicates by content hash; gating it would
just slow agents down without protecting anything.

### 5.3 Write (gated)

`kb.propose_claim`, `kb.propose_page`, `kb.propose_entity`,
`kb.propose_relation`. All accept `dry_run: true` for validation-only.

### 5.4 Decisions (host-trusted)

`kb.approve`, `kb.reject`. Implementations MUST require an approver
identity distinct from `proposed_by` unless explicitly configured for
trusted-agent self-approval.

### 5.5 Lifecycle (direct, audited)

`kb.supersede`, `kb.contradict`, `kb.archive`, `kb.confirm`, `kb.cite`.
These mutate already-reviewed knowledge — they're metadata about
durable artifacts, not new assertions, so they don't need the gate.

### 5.6 Sessions

`kb.session_start`, `kb.session_end`, `kb.crystallize`.

### 5.7 Maintenance

`kb.index_rebuild`, `kb.lint`, `kb.doctor`, `kb.audit`, `kb.export`,
`kb.export_check`, `kb.import_check`, `kb.import_apply`.

---

## 6. Transports

A conforming server MUST speak one of:

- **MCP over stdio.** JSON-RPC 2.0 framed per the MCP spec. Methods
  appear as MCP tools named `kb_<method>` (underscore for tool-naming
  compatibility); the canonical name in capabilities is still
  `kb.<method>`.
- **JSONL over stdin/stdout.** One JSON object per line. Request shape:
  `{"id": str, "method": str, "params": object}`. Response shape:
  `{"id": str, "ok": bool, "result"?: any, "error"?: {"code": str, "message": str}}`.
  Error codes: `method_not_found`, `missing_param`, `invalid_request`,
  `internal_error`.

Servers MAY support both. See [spec/transports.md](spec/transports.md).

---

## 7. Audit log

`audit.log.jsonl` is one `AuditEvent` per line, appended on every
mutation. The log is committed and is the durable history of the KB.

- Events MUST include `id`, `event`, `actor`, `created_at`,
  `object_ids`.
- Events MAY include `dry_run`, `reversible`, and an `data` object for
  event-specific payload.
- Events MUST NOT be deleted or rewritten. Rotation (compressing old
  segments into separate files) is permitted; in-place edits are not.

Full event vocabulary: [spec/audit-vocabulary.md](spec/audit-vocabulary.md).

---

## 8. Bundle format

`vouch export` produces a tar.gz containing:

```
manifest.json                # version, generated_at, files: [{path, sha256, bytes}]
claims/...
pages/...
sources/.../meta.yaml
sources/.../content
entities/...
relations/...
evidence/...
sessions/...
decided/...
config.yaml
```

Excluded from bundles: `proposed/`, `state.db`, `audit.log.jsonl`.
Proposals are draft-state; the audit log is local to one KB instance;
the index is derivable.

`manifest.json` carries a sha256 for every file. Import is gated by
`vouch import-check`, which produces a diff (`new` / `conflict` /
`identical`) before any destructive action. `vouch import-apply`
defaults to `--on-conflict skip`; overwriting requires explicit opt-in.

Bundle schema: [schemas/bundle.manifest.schema.json](schemas/bundle.manifest.schema.json).

---

## 9. Capabilities descriptor

Returned by `kb.capabilities`:

```json
{
  "name": "vouch",
  "version": "1.0.0",
  "spec": "vouch-0.1",
  "methods": ["kb.capabilities", "kb.status", ...],
  "retrieval": ["fts5", "substring"],
  "review_gated": true,
  "transports": ["mcp", "jsonl"],
  "knowledge_capability": {
    "kind": "local-cited-review-gated-kb",
    "stores_evidence": true,
    "audit_log": true
  }
}
```

Implementations claiming to speak this spec MUST set
`"review_gated": true`. Implementations without a review gate are not
conforming vouch servers — they're something else.

---

## 10. Versioning

This spec follows the project version. Pre-1.0:

- The on-disk layout may change between minor versions; a migration
  tool is provided.
- The `kb.*` surface may add methods at any time. Removing or
  reshaping a method is a breaking change and must appear in
  `CHANGELOG.md`.

At 1.0:

- The on-disk layout is frozen. Breaking changes require a major bump
  and a migration.
- The `kb.*` surface is frozen for removal/reshape. Additions are
  always permitted.

---

## 11. Conformance

A conforming implementation:

1. Implements every method in §5 with the documented shapes.
2. Enforces every validation rule in §3.
3. Implements the review-gate state machine in §4.
4. Writes audit events for every mutation per §7.
5. Produces and consumes bundles matching §8.
6. Returns a capabilities descriptor matching §9.

A test pack measuring this is planned for 0.2 — see [ROADMAP.md](ROADMAP.md).
