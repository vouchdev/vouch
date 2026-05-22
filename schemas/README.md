# schemas/

JSON Schema (draft 2020-12) files for every persisted vouch artifact
plus the bundle manifest and JSONL transport envelope.

These are **generated** from the pydantic models in
[../src/vouch/models.py](../src/vouch/models.py). If you find drift,
the pydantic models are the source of truth — regenerate with:

```bash
python scripts/gen_schemas.py
```

The generator is [../scripts/gen_schemas.py](../scripts/gen_schemas.py);
it writes `*.schema.json` for every model in `MODELS` and is
deterministic (sorted keys, stable `$id`s) so re-runs produce no diff
unless a model actually changed.

(`bundle.manifest.schema.json` and `jsonl-envelope.schema.json` are
hand-maintained — they don't have pydantic counterparts.)

## Index

### Domain objects

| slug | model | notes |
|---|---|---|
| `source` | `Source` | content-addressed by sha256 |
| `evidence` | `Evidence` | span pointer into a source |
| `claim` | `Claim` | atomic durable assertion |
| `entity` | `Entity` | graph node |
| `relation` | `Relation` | graph edge |
| `page` | `Page` | markdown with frontmatter |
| `session` | `Session` | agent work block |
| `proposal` | `Proposal` | review-gate primitive — vouch's own |
| `audit-event` | `AuditEvent` | one line in `audit.log.jsonl` |

### Retrieval

| slug | model |
|---|---|
| `context-item` | `ContextItem` |
| `context-quality` | `ContextQuality` |
| `context-pack` | `ContextPack` |

### Protocol

| slug | model | notes |
|---|---|---|
| `capabilities` | `Capabilities` | returned by `kb.capabilities` |
| `bundle.manifest` | — | `manifest.json` inside an export tarball |
| `jsonl-envelope` | — | request/response shape for JSONL transport |

## Validation

Implementations of `kb.*` SHOULD validate inputs against the relevant
schema before persisting. The reference implementation does this via
pydantic; alternative implementations can use any JSON Schema validator.

```bash
# Example with check-jsonschema
pipx install check-jsonschema
check-jsonschema --schemafile schemas/claim.schema.json my-claim.json
```
