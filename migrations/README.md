# Schema migration manifests

Each file here is a **migration manifest**: a single, consecutive
model-schema version step, declared as data. Adding one is a contributor-friendly,
single-file PR — the same shape as `adapters/`. The runner
(`vouch.migrations.runner`) chains them from a KB's current
`.vouch/schema_version` toward the target.

Provenance is derived; migrations are not. A manifest rewrites the durable
source-of-truth files under `.vouch/` (yaml claims, markdown page frontmatter,
etc.). The `state.db` cache is disposable and rebuilt afterward by `vouch index`.

## File naming

`NNNN-short-slug.yaml`, e.g. `0001-add-scope-spec.yaml`. Files are applied in
filename order; exactly one manifest may migrate *from* any given version.

## Format

```yaml
from_version: "0.1.0"          # must equal the previous manifest's to_version
to_version:   "0.2.0"          # strictly greater than from_version
artifact:     claims           # claims | pages | entities | relations | evidence | sessions
description:  "rename confidence -> certainty; default scope=project"
transforms:                    # applied in order to each artifact's dict
  - rename:  {from: confidence, to: certainty}
  - default: {field: scope, value: project}
  - drop:    {field: legacy_flag}
  - split:   {field: name, into: [first, last], on: " "}
  - merge:   {fields: [first, last], into: name, with: " "}
reverse:                        # documents the inverse (rollback is journal-based)
  - rename:  {from: certainty, to: confidence}
  - drop:    {field: scope}
```

### Transform verbs

| Verb | Spec | Effect |
|------|------|--------|
| `rename`  | `{from, to}` | move a field's value to a new key |
| `default` | `{field, value}` | set a field only if absent |
| `drop`    | `{field}` | remove a field |
| `split`   | `{field, into: [..], on}` | split a string field into several |
| `merge`   | `{fields: [..], into, with}` | join several fields into one |

## Apply / rollback

`vouch migrate apply` rewrites each artifact atomically (temp + fsync +
rename) and journals the prior content to `.vouch/migrations/rollback-<id>.jsonl`
*before* writing, then bumps `.vouch/schema_version` last. `vouch migrate
rollback` replays the newest journal to restore the exact prior bytes. An
interrupted apply therefore always leaves the KB at its prior version with a
journal to recover from.

> No real manifests ship yet — the first lands with ROADMAP 0.2's multi-dim
> scopes (#2). This directory documents the convention and is exercised by the
> synthetic manifests in `tests/fixtures/migrations/`.
