# Roadmap

This is a rough plan, not a contract. Anything dated is a target, not a
promise. Items marked **[VEP]** require a written proposal in
[proposals/](proposals/) before implementation.

## 0.1 — surface stabilises (next)

- Vector embeddings as a retrieval backend alongside FTS5 (shipped).
  Retrieval is controlled by `retrieval.backend` in `config.yaml`:
  `auto` (default) tries embedding → FTS5 → substring, gracefully
  degrading to FTS5 when the embeddings extras aren't installed; set
  `embedding`, `fts5`, or `substring` to pin a single path.
- `vouch diff <id-old> <id-new>` for claim/page revisions.
- `vouch approve --batch` for reviewing N proposals in one transaction.
- HTTP transport (`vouch serve --transport http`) behind a localhost
  bind by default. **[VEP]**
- Migration story: `vouch migrate` to upgrade on-disk layout between
  minor versions without losing the audit trail.

## 0.2 — multi-agent & scopes

- Scopes on Claim and Source beyond a single field — at minimum
  `(visibility, project, agent)` so a multi-agent KB can carve up
  who-sees-what. **[VEP]**
- Multi-agent sync: well-defined merge semantics for two `.vouch/`
  directories that diverged. Today this is "git merge and hope";
  we want a deterministic resolver for `decided/` and `audit.log.jsonl`.
- Adapter templates checked in for the major runtimes
  (Claude Code, Cursor, Codex, Continue).
- Conformance suite: a runnable test pack that any KB server claiming
  to speak `kb.*` can be measured against.

## 0.3 — operational maturity

- Benchmarks: search latency, proposal throughput, bundle import time
  on KBs in the 1k / 10k / 100k claim range.
- `vouch fsck` — deeper consistency checks than `doctor`.
- Structured logging behind `VOUCH_LOG_FORMAT=json`.
- First-class observability hooks (proposal counts, approval rate,
  citation-coverage).

## 1.0 — frozen on-disk format + frozen method surface

Once we cut 1.0:
- The on-disk layout in `.vouch/` is a stable format. Breaking changes
  require a major bump and a migration tool.
- The `kb.*` method surface is stable. Adding methods is fine; removing
  or changing signatures requires a major bump.
- Semantic versioning applies normally from this point.

## Explicitly out of scope (today)

These come up in discussion. The current answer is "not now":

- **Hosted vouch service.** vouch is a library + CLI. A hosted
  multi-tenant version is somebody else's product.
- **A web UI for review.** PRs are the review UI. A standalone web
  reviewer might be nice but it isn't on this list.
- **Cross-language clients beyond the protocol.** If you want a
  TypeScript client, implement the `kb.*` JSONL contract; we'll keep
  it documented.

If something here matters to you, please file an issue — order is
negotiable.
