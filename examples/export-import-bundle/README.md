# Portable bundles: export, export-check, import-check, import-apply

Move a reviewed knowledge base safely between trees without trusting opaque
state. `export` tars the durable KB into a portable bundle; `export-check`
fail-closes if any file drifts from its manifest hash (the gate another tool
runs before trusting it); `import-check` diffs a bundle against a destination
KB without writing (new vs conflict vs identical); `import-apply` applies it
with a non-destructive default policy (`skip`).

Mirrors AKBP's `portable-bundle` example, adapted to vouch's tar-based,
hash-manifested bundle format.

## Run it

```bash
VOUCH=/path/to/vouch examples/export-import-bundle/run.sh
```

`VOUCH` defaults to `vouch` on your `PATH`. The script builds two throwaway
KBs and a bundle in `mktemp` dirs and cleans them up on exit. Success marker:

```text
export-import-bundle example passed
```

## Steps

1. **Build KB1, approve two claims.** Register two sources, propose two
   claims as `proposer-agent`, then approve them as `reviewer-agent` —
   vouch forbids self-approval, so every write still passes the review gate.
2. **`export --out bundle.tar.gz`.** Bundles the durable KB into a portable
   `.tar.gz`. Prints `bundle_id` (the content hash of the manifest) and the
   file count. Local indexes (`state.db`) are derived and intentionally
   excluded — the consumer rebuilds them.
3. **`export-check bundle.tar.gz`.** Re-hashes every file in the bundle
   against its manifest. `ok: true` and exit 0 mean the bundle is intact and
   safe to hand off; any drift yields `ok: false` and a non-zero exit.
4. **Init a separate KB2** as the import destination.
5. **`import-check bundle.tar.gz`.** Diffs the bundle against KB2 *without
   writing*. Buckets every file into `new_files` (the two imported claims +
   their sources), `conflicts` (present in both with different content — here
   the seeded starter artifacts), and `identical_files` (byte-identical,
   safe to skip). This is the contract boundary — review it before applying.
6. **`import-apply --on-conflict skip`.** Applies the bundle. The default
   `skip` policy never clobbers a conflicting file (`--on-conflict overwrite`
   or `fail` change that). `written` lists what landed; `skipped_conflicts`
   lists what was left untouched.
7. **`status`** in KB2 shows the imported claims are now durable.

## Real output excerpt

```text
=== export — tar the durable KB into a portable bundle ===
{
  "bundle_id": "9b269aca88e87e48e0cff8c231e1545b8efdbd5f893039c6bf19e4131c6759b0",
  "files": 13,
  "out": ".../bundle.tar.gz"
}

=== export-check — fail-closed gate: every file must match its manifest hash ===
{ "bundle_id": "9b269aca...", "files_checked": 13, "issues": [], "ok": true }

=== import-check — diff the bundle against KB2 without writing anything ===
{
  "conflicts": [ "claims/vouch-starter-reviewed-knowledge.yaml", ... ],
  "identical_files": 2,
  "new_files": [
    "claims/acme-example-ships-releases-on-fridays.yaml",
    "claims/alice-example-owns-the-deploy-runbook.yaml",
    ...
  ],
  "ok": true
}

=== import-apply — non-destructive default (skip): never clobbers conflicts ===
{
  "on_conflict": "skip",
  "skipped_conflicts": [ "claims/vouch-starter-reviewed-knowledge.yaml", ... ],
  "written": [ "claims/acme-example-ships-releases-on-fridays.yaml", ... ]
}

=== status in KB2 — the imported claims are now durable ===
KB at .../.vouch
  durable: 3 claims  •  1 pages  •  3 sources  •  0 entities  •  0 relations
  pending: 0 proposals
```

## Methods demonstrated

- `kb.export` — tar the durable KB into a portable `.tar.gz` bundle.
- `kb.export_check` — verify every bundled file matches its manifest hash.
- `kb.import_check` — diff a bundle against a destination KB without writing.
- `kb.import_apply` — apply a bundle under a non-destructive conflict policy.
- `kb.status` — artifact counts confirming the import landed.
