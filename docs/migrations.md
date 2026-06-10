# Schema migrations — `vouch migrate`

The `.vouch/` layout is the durable contract: yaml claims, markdown pages with
frontmatter, json sessions, the jsonl audit log. As the pydantic models in
`src/vouch/models.py` evolve, a KB created against an older model would become a
load-time error. `vouch migrate` gives that evolution a versioned, reversible,
audit-logged upgrade path so a schema change never silently breaks a KB in the
wild.

## Two version axes

vouch tracks two independent versions, and `vouch migrate` covers both:

| | Stamp | What it governs | Reached by |
|---|---|---|---|
| **Format** (integer) | `config.yaml` `version` | the `.vouch/` directory layout (subdirs, `.gitignore`) | `vouch migrate` *(no subcommand)* |
| **Schema** (semver) | `.vouch/schema_version` | the model schema of each artifact | `vouch migrate <subcommand>` |

A KB with no `.vouch/schema_version` file is treated as the baseline (`0.1.0`),
so existing KBs keep loading until their first migrate. `vouch init` stamps the
current version on bootstrap.

## Commands

```bash
vouch migrate status                 # current schema version, target, pending migrations
vouch migrate plan                   # dry-run: every file each pending migration would change
vouch migrate plan --to 0.3.0        # plan against a specific target
vouch migrate apply                  # apply pending migrations (audit-logged, atomic)
vouch migrate apply --yes            # skip the confirmation prompt (CI)
vouch migrate rollback               # reverse the most recently applied migration
vouch migrate verify                 # parse-load every artifact under the current version
```

## Manifests

Migrations are **data, not code**: yaml manifests in the repo-root `migrations/`
directory, one consecutive version step each. See
[`migrations/README.md`](../migrations/README.md) for the full format and the
transform verbs (`rename`, `default`, `drop`, `split`, `merge`). Only consecutive
manifests apply — a `0.1 → 0.5` KB walks through `0.2`, `0.3`, `0.4` in order.

## Safety model

- **Atomic per file.** Each artifact is rewritten to a temp file, `fsync`-ed,
  then `os.replace`-d into place. A file is always its old bytes or its new
  bytes — never a torn mix.
- **Reversible.** Before any rewrite, the prior content of every file the step
  touches is journalled to `.vouch/migrations/rollback-<id>.jsonl`. `vouch
  migrate rollback` replays it, restoring the exact prior bytes — so
  apply → rollback is byte-equivalent (modulo the audit-log entries recording
  the round trip).
- **Crash-safe.** The journal is written and fsynced *before* the first rewrite,
  and `.vouch/schema_version` is bumped *last*. An interrupted apply therefore
  leaves the KB reporting its prior version, with a journal to recover from.
- **Precondition.** Apply refuses if the KB has pending proposals — it won't
  rewrite a reviewer's in-flight queue out from under them. Resolve them with
  `vouch list-pending` first.
- **`state.db` is disposable.** The runner does not migrate the FTS5 / embedding
  cache; the CLI rebuilds it after a successful apply.

## Audit trail

Each applied manifest logs one `kb.migrate.apply` event (manifest id, file count,
rollback-journal id); a rollback logs `kb.migrate.rollback`. The legacy format
migration continues to log `kb.migrate`.

## Out of scope (v1)

- Cross-major jumps in a single manifest (consecutive only).
- `custom: path/to/migration.py` script transforms (lands once the verb
  taxonomy stabilises).
- Migrating `state.db` (disposable; rebuilt by `vouch index`).
- Bundle version embedding / refusing mismatched imports — a follow-up.
- Concurrent multi-process migrate runs (single-writer assumption).
