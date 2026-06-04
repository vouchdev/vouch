---
vep: "0004"
title: "Versioned on-disk format migration with atomic rollback"
author: greatjourney589
status: draft
created: 2026-06-04
landed-in: ""
supersedes: []
superseded-by: ""
---

# VEP-0004: Versioned on-disk format migration with atomic rollback

## Summary

Add `schema_version` tracking to every vouch KB and a `vouch migrate`
command that upgrades the on-disk `.vouch/` layout atomically between
minor versions. Migration is all-or-nothing: artifacts are transformed
into a temp directory, validated against the target Pydantic models, then
swapped in via an atomic rename. Any failure leaves the original KB
completely untouched. This is the last hard blocker before 1.0 can freeze
the on-disk format.

## Motivation

Today there is no migration path between vouch schema versions. Any field
added to a Pydantic model without a default causes `KBStore.load_claim()`
(and its siblings) to throw a `ValidationError` at read time, making the
entire KB unreadable. There is no `vouch migrate`, no version header in any
artifact, and no recovery path short of manually patching every YAML file.

Concrete example: a team with 500 approved claims upgrades from vouch 0.1
to 0.2. If 0.2 adds a single non-defaulted field to `Claim`, every claim
file becomes unreadable. `vouch doctor` crashes before it can surface a
useful error. `vouch export` fails for the same reason. The bundle import
path (`bundle.py`) validates artifact files against the current Pydantic
models before accepting them, so even the export-then-reimport escape hatch
is blocked.

Three workarounds were evaluated and found lacking:

- **`vouch export` + manual YAML rewrite + `vouch import-apply`:** the bundle
  import path calls the same storage layer and fails identically.
- **`vouch doctor`:** has no notion of schema versions; crashes at read time
  before any health check runs.
- **Pinning `pyproject.toml` version constraints:** prevents upgrading the
  tool, not a solution.

This is the last missing piece before 1.0 can declare the on-disk format
stable and guarantee upgrade safety.

## Proposal

### 1. Schema version constant

`src/vouch/models.py` gains a module-level constant:

```python
VOUCH_SCHEMA_VERSION = "0.1"
```

This is the single source of truth for the schema version the installed
vouch expects. It is bumped (by hand, in the same commit that makes a
breaking on-disk change) whenever the format changes.

### 2. `schema_version` in `config.yaml`

`KBStore.init()` writes `schema_version: "0.1"` into `config.yaml`.
`KBStore.__init__()` reads it back and raises `SchemaMismatchError` when
the stored version does not match `VOUCH_SCHEMA_VERSION`:

```python
class SchemaMismatchError(RuntimeError):
    """Stored KB schema_version does not match the installed vouch."""
```

KBs created before this change have no `schema_version` key; they are
treated as `"0.1"` (the current version) and so pass the guard without
requiring migration. Future breaking changes bump `VOUCH_SCHEMA_VERSION`
to `"0.2"`, at which point old KBs fail the guard with a clear error
message pointing to `vouch migrate`.

### 3. `vouch migrate` CLI command

```
vouch migrate [--dry-run] [--backup] [--from VERSION] [--to VERSION]
```

- `--dry-run`: walks every artifact, applies all transforms, reports what
  would change — no writes.
- `--backup`: copies `.vouch/` to `.vouch-backup-<timestamp>/` before
  mutating anything.
- `--from` / `--to`: explicit version override (defaults: read from
  `config.yaml` → `VOUCH_SCHEMA_VERSION`).
- Exits non-zero if any artifact fails to migrate; partial runs are rolled
  back (all-or-nothing write via temp directory + atomic rename, the same
  pattern used by `rebuild_index`).

### 4. Migration registry in `src/vouch/migration.py`

A new module containing a list of `Migration` objects, each covering one
version step:

```python
@dataclass
class Migration:
    from_version: str
    to_version: str
    transforms: list[Transform]

MIGRATIONS: list[Migration] = []  # populated as schema versions are added
```

Each `Transform` is a callable `(raw_dict: dict, artifact_type: str) -> dict`
operating on the YAML-parsed dict before Pydantic deserialisation. This
decouples migration logic from the Pydantic models at the target version —
a 0.1→0.2 transform can run against raw dicts even after the 0.1 model
class is removed.

`migrate_kb(root, from_v, to_v, *, dry_run)` chains the relevant
`Migration` steps, writes to `.vouch/.migrate-tmp/`, validates every file,
then atomically replaces `.vouch/` or rolls back on any failure.

### 5. Rollback guarantee

`migrate_kb()` follows the atomic-swap pattern already proven in
`rebuild_index`:

1. Write all migrated artifacts to `.vouch/.migrate-tmp/`.
2. Validate every migrated artifact loads cleanly under the target Pydantic
   models.
3. Rename `.vouch/` → `.vouch-pre-migrate/`, `.vouch/.migrate-tmp/` →
   `.vouch/`.
4. On any failure in step 2 or 3: leave the original `.vouch/` untouched,
   write `migration.rollback` to the audit log, exit non-zero.

### 6. Bundle schema version

`build_manifest()` includes `schema_version` in `manifest.json`.
`import_check()` rejects bundles whose `schema_version` is newer than the
installed version with a clear message. Bundles with no `schema_version`
field (pre-VEP-0004) are accepted as-is — additive compatibility.

### 7. Health and status surfaces

- `health.status()` includes `schema_version` from `config.yaml`.
- `health.doctor()` adds a `critical`-severity finding when the stored
  `schema_version` does not match `VOUCH_SCHEMA_VERSION`.

## Design

### migrate_kb algorithm

```python
def migrate_kb(root: Path, from_v: str, to_v: str, *, dry_run: bool = False) -> MigrateResult:
    steps = _chain(from_v, to_v)           # ordered list of Migration objects
    if not steps:
        return MigrateResult(changed=[], skipped=[], from_v=from_v, to_v=to_v)

    kb_dir = root / KB_DIRNAME
    tmp_dir = kb_dir / ".migrate-tmp"
    tmp_dir.mkdir(exist_ok=True)

    changed = []
    try:
        for sub in MIGRATABLE_SUBDIRS:
            ...  # read each yaml, apply transforms, write to tmp_dir
        _validate_all(tmp_dir)             # load every file under target models
        if not dry_run:
            _atomic_swap(kb_dir, tmp_dir)  # rename old → pre-migrate, tmp → live
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return MigrateResult(...)
```

### Version chain resolution

`_chain(from_v, to_v)` walks `MIGRATIONS` to find every contiguous step
between `from_v` and `to_v`. A multi-hop upgrade (0.1 → 0.2 → 0.3) applies
each Migration in sequence so every intermediate transform runs correctly.
If no path exists, `migrate_kb` raises `ValueError` before touching any
files.

### SchemaMismatchError guard placement

`KBStore.__init__` reads `config.yaml` and calls `assert_schema_ok()`.
The `server.py` and `jsonl_server.py` startup paths already construct a
`KBStore`; they gain a `try/except SchemaMismatchError` that returns a
structured error to the caller rather than a traceback.

The destructive CLI commands (`approve`, `reject`, `index`, `crystallize`)
use `_load_store()`, which already calls `KBStore(discover_root(...))`, so
the guard fires automatically.

## Compatibility

- Writing `schema_version` to `config.yaml` is **additive** for consumers
  that ignore unknown fields. KBs created before this VEP have no
  `schema_version` key; they are treated as `"0.1"` and pass the guard
  without requiring migration.
- The `manifest.json` bundle format gains a `schema_version` field —
  additive. `import_check` applies a compatibility window for bundles
  without the field.
- `health.status()` gains a `schema_version` key — additive.
- The `SchemaMismatchError` guard in `KBStore.__init__` is a **breaking
  change** for callers that were silently relying on cross-version reads
  succeeding despite `ValidationError`s. This is the correct trade-off:
  the current behaviour (silent data corruption on round-trip) is worse
  than a loud, actionable error.

## Security implications

None. The migration path reads existing `.vouch/` files (which are already
readable by the vouch process) and writes into a sibling temp directory
under `.vouch/`. No new trust boundaries are crossed. The `--backup` flag
copies files but does not change their permissions.

## Performance implications

`migrate_kb` is a one-shot maintenance operation, not a hot path. It reads
and rewrites every YAML artifact once. For a KB with 1 000 claims this is
expected to complete in under a second on a modern SSD. The atomic rename
in step 3 is O(1) on any POSIX filesystem.

## Open questions

- Should `vouch migrate` also rebuild `state.db` at the end? The index is
  a derived cache, so a `vouch index` after migration is always safe; but
  wrapping it automatically would be friendlier.

## Alternatives considered

- **Never break the on-disk format:** requires never adding fields without
  defaults and never removing fields. Untenable for a pre-1.0 project where
  the schema must evolve to fix design mistakes.
- **Always-forward-compatible Pydantic models (`extra="ignore"`, all fields
  `Optional`):** silently drops data on round-trip, which is worse than a
  migration error for a system whose value proposition is durable, audited
  knowledge.
- **Require users to re-init and re-approve everything on upgrade:**
  destroys the KB's audit trail and decided history.

## References

- ROADMAP.md: "Migration story: `vouch migrate` to upgrade on-disk layout
  between minor versions without losing the audit trail."
- `health.py:rebuild_index` — the atomic temp-file-swap pattern this VEP
  reuses.
- `embeddings/migration.py` — the embedding-model backfill pattern, a
  narrower precedent for the same idea.
