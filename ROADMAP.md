# Roadmap

This is a rough plan, not a contract. Anything dated is a target, not a
promise. Items marked **[VEP]** require a written proposal in
[proposals/](proposals/) before implementation.

The 0.x milestones that used to live here shipped in the 1.x line:
embeddings + hybrid retrieval, `vouch diff`, HTTP transport, migrations,
adapter templates for the major runtimes, and the web console. What
follows is what's next.

## 1.3.x — integrity & retrieval defaults

- Audit-log hardening: file locking on the hash-chain append, and
  audit-before-move ordering in `approve()` so a crash can never leave a
  durable claim without its audit event.
- Full three-surface parity enforcement: the capabilities test compares
  MCP tools and JSONL handlers today; the CLI mirror joins it.
- Zero-config retrieval quality: hybrid fusion is already the default;
  next is rerank + recency in the default read path (not flag-only) and
  a story for local embeddings that works on a base install.
- Retrieval honesty: when the backend degrades (no embeddings, FTS5
  unavailable), say so in `_meta` instead of silently returning worse
  results.

## 1.4 — the wiki front door

Pages are the product; claims are the citation layer. This milestone
makes the wiki browsable and self-maintaining:

- A `/page` browse surface in the console (the chat's page drawer grows
  into a real reader with an index).
- Backlinks: `[[wikilinks]]` are validated today, then discarded;
  persist the link graph and render it.
- Wiki lint: orphan pages, stale pages, dead links, uncited sections.
- `vouch compile` maintains existing pages (compounding updates), not
  just new ones.
- Good synthesized answers can be filed back as page proposals — gated
  by review like every other write.

## 1.5 — measurement

- A committed, reproducible eval harness beyond the recall gate:
  follow-rate tracking with confidence intervals and a coding-specific
  corpus.
- Benchmarks: search latency, proposal throughput, bundle import time on
  KBs in the 1k / 10k / 100k claim range.
- `vouch fsck` — deeper consistency checks than `doctor`.

## 2.0 — connected KBs

Multiple vouch instances — personal, per-project, team — exchanging
reviewed knowledge. The review gate is non-negotiable: a receiving KB
accepts inbound knowledge as *proposals*; nothing lands past
`proposals.approve()`. Self-hosted, local-first — not a hosted service.

- Deterministic merge semantics for two `.vouch/` directories that
  diverged (replaces "git merge and hope") — the audit hash chain is
  what makes divergence detectable. **[VEP]**
- Bundle push/pull between KBs: signed bundles of decided claims and
  pages that arrive as proposals, actor identity preserved end-to-end in
  the audit log. **[VEP]**
- Scopes beyond a single field — at minimum `(visibility, project,
  agent)` so a multi-KB deployment can carve up who-sees-what. **[VEP]**
- A hub daemon: registry of connected KBs, scope-based subscription
  rules, federated search with provenance (every result names the KB
  that vouched for it), and a hub view in the console.
- Conformance suite: a runnable test pack that any KB server claiming to
  speak `kb.*` can be measured against — connecting is a protocol
  property, not a product feature.

## Explicitly out of scope (today)

These come up in discussion. The current answer is "not now":

- **Hosted vouch service.** vouch is a library + CLI + self-hosted
  console. A hosted multi-tenant version is somebody else's product.
- **Removing the review gate for "trusted" agents.** The gate is the
  product.
- **Cross-language clients beyond the protocol.** If you want a
  TypeScript client, implement the `kb.*` JSONL contract; we keep it
  documented and (in 2.0) conformance-testable.

If something here matters to you, please file an issue — order is
negotiable.
