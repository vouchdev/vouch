# vouch init --template — domain starter packs

## Problem

A fresh `.vouch/` KB starts nearly empty (one generic starter claim). When you
spin up vouch inside a domain-specific repo — e.g. Gittensor (SN74) — it knows
nothing about that domain, so the first agent session has no cited context to
retrieve. We want `vouch init` to optionally seed a domain-aware starter pack.

## Goal

Generalize `vouch init`'s seeding into a named-template registry and ship a
`gittensor` pack as the first non-default template:

```
vouch init [--path P] [--template starter|gittensor]
```

Default stays the current `starter` seed (behavior unchanged). `gittensor`
seeds a small, cited, **already-approved** knowledge base about how SN74
contribution scoring works.

## Decisions

- **Registry, not a one-off flag.** A `TEMPLATES` dict maps name → seed
  function so future packs (research-notes, codebase-audit) plug in the same
  way.
- **Approved-direct seeding.** Like the existing `seed_starter_kb`, template
  artifacts are written approved at init time — this is bootstrap content, not
  the review queue.
- **Idempotent.** Stable ids / content hashes mean re-running `init` creates
  nothing new.
- **In-code templates, not files.** No `templates/<name>/` on disk and no
  reliance on `vouch ingest`; the pack is a seed function.

## Components — `src/vouch/onboarding.py`

### `SeedResult` (dataclass)
`template: str`, `created: list[str]` (artifact ids actually created),
`created_anything: bool`. Replaces/absorbs the current `StarterSeedResult`.

### `TEMPLATES` registry
```python
TEMPLATES: dict[str, Callable[[KBStore, str], SeedResult]] = {
    "starter": seed_starter_kb,
    "gittensor": seed_gittensor_kb,
}
```
`available_templates() -> list[str]` returns sorted keys (for help + errors).

### `seed_starter_kb(store, approved_by) -> SeedResult`
The current default seed, refactored to return `SeedResult`. Behavior unchanged
(same source + claim ids).

### `seed_gittensor_kb(store, approved_by) -> SeedResult`
Seeds, idempotently and approved-direct:
- **1 Source** — SN74 description text (`title="Gittensor SN74"`,
  `locator="vouch:template/gittensor"`, `source_type="message"`,
  `media_type="text/markdown"`), content from Gittensor's public README facts.
- **1 Entity** — `gittensor-sn74` (`type=project`).
- **4 Claims** (`type=fact`, `status=stable`, each citing the source, linked to
  the `gittensor-sn74` entity):
  1. Miners earn TAO for pull requests merged into whitelisted repositories.
  2. Validators verify GitHub account ownership via a fine-grained PAT before scoring.
  3. Contributions are scored by code quality, repository allocation, and language factors.
  4. Sybil-resistant: GitHub account verification + merged-PR requirement prevent gaming.
- Each artifact keyed by a stable id; existence checks make re-runs no-ops.

## CLI — `src/vouch/cli.py`

`init` gains `--template` (default `"starter"`):
- Resolve the name against `TEMPLATES`; unknown → `_cli_errors`-style
  `Error: unknown template '<x>' (available: gittensor, starter)`.
- Call the selected seed; print a generalized summary
  (`Seeded <template> template: <n> artifact(s)` / `already present`).
- `rebuild_index` + `kb.init` audit event as today.

## Error handling

- Unknown `--template` value → clean CLI error listing available templates.
- Re-running `init` with the same template → no duplicates, prints
  "already present".

## Testing (TDD)

- `seed_gittensor_kb`: creates source + entity + 4 claims; every claim cites the
  source and links the entity; second call creates nothing (idempotent).
- registry: `available_templates()` == `["gittensor", "starter"]`; unknown
  lookup handled cleanly.
- `seed_starter_kb` still produces the same starter source + claim ids
  (regression).
- CLI: `vouch init --template gittensor` seeds the pack and prints the summary;
  `--template bogus` → clean error; default `vouch init` unchanged.

## Non-goals

- Template files on disk / `vouch ingest` integration.
- `--template` on commands other than `init`.
- Live Gittensor data (scores/queues) — that's Gittensory's domain; this pack is
  static cited facts, deliberately complementary.
