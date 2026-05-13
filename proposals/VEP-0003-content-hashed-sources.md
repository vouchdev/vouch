---
vep: 0003
title: Content-hashed sources
author: alaingm
status: final
created: 2026-05-04
landed-in: 0.0.1
---

# VEP-0003: Content-hashed sources

## Summary

Every Source's id is the sha256 of its captured bytes. Re-registering
identical content yields the same id and is a no-op. Tampered or
truncated content fails verification.

## Motivation

A KB built on cited claims is only as trustworthy as its sources. If a
source's `locator` (path or URL) is treated as its identity, you have
three problems:

1. **Drift.** The bytes at a URL change. The claim cites a source that
   no longer says what it said.
2. **Duplication.** The same paper is added three times under three
   slightly different URLs. The KB has three "sources" but one underlying
   document.
3. **Tamperability.** Someone (an agent, a confused human, a hostile
   actor) edits a captured source file. The claims still cite it.
   Nothing flags the change.

Content addressing solves all three. The id is the bytes; the bytes
are the id.

## Proposal

A Source's `id` field is the lowercase hex sha256 of its captured
content, exactly 64 hex characters. The `locator` retains the
human-facing path/URL.

Validation: `Source.id` is checked at construction by pydantic.
Registration: `kb.register_source` computes the hash, returns the
existing record if it matches an already-registered source
(`deduplicated: true`), otherwise writes a new one.

Storage: `.vouch/sources/<sha>/meta.yaml` for metadata,
`.vouch/sources/<sha>/content` for the bytes (when captured).

Verification: `kb.source_verify` recomputes the hash of every captured
source and reports drift. `vouch doctor` runs this as part of its
sweep.

## Design

Captured vs referenced sources:

- **Captured**: the bytes live under `.vouch/sources/<sha>/content`.
  The id is unambiguous; verification is deterministic.
- **Referenced**: only `meta.yaml` exists (no `content` file). For
  external URLs we may not want to copy 400MB locally. The hash is
  still recorded — it's what we saw when we first registered. If the
  URL drifts, future verification flags it.

Verification details:

- For captured sources: recompute sha256 of `content`; compare to id.
- For referenced sources: skipped by default; opt-in re-fetch with
  `--refetch` (lives outside the spec; CLI affordance).

## Compatibility

Founding decision. No prior format to be compatible with.

For future versions: changing the hash function (e.g. to sha512) is a
breaking change to ids. If we ever do that, ids become
`<algo>:<hex>`. Migration: keep the old ids working as aliases for a
major version, then drop. This is not planned.

## Security implications

Content addressing **does not validate semantics**. A poisoned web
page that gets registered as a source has a correct hash; the bytes
are still poisoned. The hash protects against post-hoc tampering, not
against bad input.

The hash is a sha256, not a cryptographic signature. Anyone with
write access can register any content under its true hash. We are not
defending against an attacker who controls registration — we're
defending against drift, dedup, and accidental edits.

## Performance implications

sha256 of typical document sizes (≤ few MB) is sub-millisecond.
Storage cost: one extra directory level per source. Negligible.

## Open questions

Resolved:
- ~~Use a faster hash (blake3)?~~ No. sha256 is everywhere, has a
  one-liner in every language, and we're not in a hot loop.
- ~~Truncate the id to 16 hex chars?~~ No. Collisions are not the
  concern; the cost of the full id is one filename.

## Alternatives considered

**ULIDs or random ids.** Simpler. Loses dedup, drift detection, and
the cryptographic-ish guarantee. Rejected.

**Hash of `locator`.** Useless — two captures of a drifting URL would
collide.

**Merkle hashes of segmented content.** Overkill. Sources are mostly
flat documents.

## References

- [spec/methods.md §register_source](../spec/methods.md#kbregister_source)
- [SECURITY.md](../SECURITY.md) for the threat model.
