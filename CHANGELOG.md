# Changelog

All notable changes to vouch are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added
- `vouch install-mcp <host>` — one-command adapter writer that drops the
  right MCP config templates into a project tree, idempotently. Eight hosts
  ship in the catalogue: `claude-code`, `claude-desktop`, `cursor`,
  `continue`, `codex`, `windsurf`, `cline`, `zed`. Each adapter is described
  by a declarative `adapters/<host>/install.yaml` manifest so adding a new
  host is a single-file PR. `--tier T1..T4` stacks adoption layers (T1 = MCP
  wire only, T2 = CLAUDE.md/AGENTS.md fenced snippet, T3 = optional slash
  commands, T4 = optional host hooks/settings). `--list` enumerates the
  catalogue; `--path` (or `--target`) installs into a project other than
  cwd. Existing files are left alone; CLAUDE.md gets a fenced append so
  re-runs stay flat-noop (#179).
- Propose-time similarity warnings: `propose_claim` / `kb.propose_claim` return
  non-blocking `warnings` (`similar_approved`, `similar_pending`) when the
  embeddings extra is installed. Configurable via `review.similarity_threshold`
  (default `0.95`). CLI prints warnings to stderr; dry-run included.
- `vouch fsck` performs deep consistency checks beyond `vouch doctor`:
  orphaned embeddings, dangling supersede/contradict chains, decided
  proposals whose artifact is missing, and FTS5 index-vs-file drift
  (orphan rows, missing rows, status drift). Read-only; reports findings
  with object ids. `--fix` is intentionally out of scope (#96).
- `vouch migrate` checks, dry-runs, and applies on-disk KB format migrations,
  preserving audit history and rebuilding derived indexes after successful
  upgrades.
- `vouch expire` garbage-collects stale pending proposals: dry-run by default,
  `--apply` moves them to `decided/` with `decision_reason: expired`, emits
  `proposal.expire` audit events, and honors `review.expire_pending_after_days`
  in `config.yaml` (default 90; `0` disables). `kb.expire` on MCP/JSONL.
- `vouch init --template <name>` seeds a domain starter pack. The default `starter` template is unchanged; the new `gittensor` template seeds a small, cited, approved KB about Gittensor (SN74) contribution scoring (1 source, 1 entity, 7 claims — merged-PR rewards, PAT verification, scoring factors, sybil-resistance, repo allow-list policy, issue-solving multiplier, and emission split) so a fresh KB in a Gittensor repo has retrievable context on day one. Templates are an in-code registry — future packs plug in the same way.
- Structured JSON logging via `VOUCH_LOG_FORMAT=json`. When set, the
  `vouch` logger emits one JSON object per line with `level`, `logger`,
  `event`, and any structured extras (e.g. `actor`, `object_ids`) passed
  through stdlib `extra=`. Unset (or any other value) keeps the existing
  human-readable format — no behaviour change beyond formatting. Wired
  into the CLI, MCP server, and JSONL server entry points. `VOUCH_LOG_FORMAT`
  was already documented in `ROADMAP.md` and `adapters/generic-mcp/README.md`
  but had no implementation (#97).
- Performance benchmark suite in `benchmarks/` covering search latency, proposal write throughput, bundle export/import/verify round-trips, and index rebuild time at 1k/10k claim sizes. Run with `pytest benchmarks/ --benchmark-only`.

### Fixed
- `discover_root()` now honours `VOUCH_KB_PATH=/abs/path/.vouch` and returns the parent root, instead of always walking up from cwd. The env var was already documented in `adapters/generic-mcp/README.md` but wasn't wired into the code — closing the doc-vs-code drift removes the `"cwd": "..."` ceremony hosts like Claude Desktop need today to point at a specific KB.

## [0.1.0] — 2026-05-26

### Packaging
- Published to PyPI as `vouch-kb` (the `vouch` name was already taken by an
  unrelated project); the installed command is still `vouch`. Install with
  `pipx install vouch-kb`. A tag-triggered release workflow publishes via PyPI
  Trusted Publishing (OIDC).

### Added
- HTTP transport: `vouch serve --transport http` exposes the full `kb.*`
  surface over HTTP, reusing the same dispatch table as the MCP/JSONL
  transports (#94, implements VEP-0004). `POST /rpc` carries the JSONL
  envelope; `GET /capabilities` and `GET /healthz` are unauthenticated.
  Binds `127.0.0.1` by default and refuses any non-loopback bind without
  both `--allow-public` and a bearer token (`--token` / `VOUCH_HTTP_TOKEN`,
  constant-time compared). The `X-Vouch-Agent` header sets the audit actor
  per request. Zero new runtime dependencies (stdlib `http.server`); no TLS
  in-process — terminate at a reverse proxy. `kb.capabilities.transports`
  now includes `http`.
- `vouch approve <id1> <id2> …` approves multiple proposals in one
  non-interactive call for CI and backlog clearing (#93). Default is
  all-or-nothing: every id is validated as an approvable pending proposal
  before any is written, so a typo or already-decided id aborts the batch
  without approving anything. `--keep-going` switches to best-effort
  (approve what you can, report the rest, exit non-zero on partial failure).
  One audit event is still recorded per approved artifact. Complements the
  interactive `vouch review` queue.
- Friendlier CLI output (#54, track 2): colourised `vouch status` / `lint` /
  `doctor` / `search` (honours `NO_COLOR`, `FORCE_COLOR`, and TTY detection);
  `--json` on `vouch lint` and `vouch search` for machine-readable output
  while the default stays human-readable; progress callbacks on the long ops
  (`rebuild_index`, `doctor`, bundle `export`/`import_apply`) surfaced as
  status lines on interactive terminals; and `vouch index` / `vouch export`
  now report a clean `Error:` instead of a traceback on a malformed artifact.
- `vouch sync-check` and `vouch sync-apply` reconcile another `.vouch`
  directory or bundle by importing only non-conflicting durable artifacts and
  reporting conflicts without overwriting reviewed knowledge.
- `vouch pending --json` emits pending proposals as structured JSON for shell
  scripts, CI checks, and multi-agent review dashboards.
- `vouch diff <id-old> <id-new>` shows what changed between two claim revisions or two page revisions — field-level changes plus a line-diff of the long text/body. Auto-detects the artifact kind and hides always-churning metadata. Read-only; supports `--json`.
- Seed a cited starter source and claim during `vouch init`, print first-run
  next steps, and document a 30-second onboarding tour (#54).
- Add `vouch review`, a guided CLI queue for approving, rejecting, skipping,
  or dry-running pending proposals without bypassing the review gate.

### Fixed
- `store.put_relation`, `store.put_relation_idempotent`, and `store.put_page` now reject artifacts whose foreign-id references don't resolve in the KB (relation `source` / `target` / `evidence`; page `entities` / `sources`). `proposals.propose_relation` and `proposals.propose_page` surface the same checks at proposal time as `ProposalError`. `bundle.import_check` and `sync.sync_check` run an equivalent cross-artifact pass against the post-merge id set so manifest-consistent bundles can't smuggle relations / pages whose references resolve to nothing — closes the write-time counterpart of the after-the-fact `dangling_relation` finding in `health.lint` (`src/vouch/health.py:135-145`).
- `bundle.import_check` / `import_apply` and `sync.sync_check` / `sync_apply` now enforce the Source content-addressing invariant: a `sources/<sha>/content` member must hash to `<sha>`, and a `sources/<sha>/meta.yaml` must carry a matching `id`/`hash`. Previously the import side trusted the bundle's directory layout, so a manifest-consistent bundle could land a Source whose content did not match its claimed id — `verify_source` would report `stored_ok=False` only after the import had already succeeded with a clean `bundle.import` audit event. The per-file sha256 gate (#74) only proves bytes match the manifest; this closes the write-time counterpart of the `verify.verify_source` detection.
- Add `put_relation_idempotent()` to `KBStore` and use it in `supersede()` and `contradict()` so retrying after a partial failure converges to a consistent state instead of raising `ValueError`.
- Raise `ProposalError("forbidden_self_approval")` in `proposals.approve()` when `approved_by == proposal.proposed_by`, enforcing the review-gate guarantee documented in the README and CONTRIBUTING.
- `crystallize()` now sets `review.approver_role: trusted-agent` context so single-agent sessions can be crystallized without hitting the `forbidden_self_approval` guard (#47).
- Narrow `except Exception` to `except ArtifactNotFoundError` in `propose_claim()` evidence validation so I/O and parse errors propagate with their original type instead of being masked as `unknown source/evidence id` (#48).
- Bundle import rejects tar members whose path escapes `kb_dir`
  (CVE-2007-4559, #9). Previously a crafted `.tar.gz` with a member
  named `../../evil.txt` could write outside `.vouch/`; the manifest
  allow-list did not prevent this because the manifest lives inside
  the same tarball. `import_apply`, `import_check`, and `export_check`
  now validate every member path and raise on unsafe names.
- Fix `vouch search` CLI: assign backend label per code path so substring fallback results are no longer mislabelled as `fts5`; update stale docstring to reflect multi-backend search surface (#52).
- Close the review-gate bypass in `sessions.crystallize` (#76). The
  durable session-summary page wrote `sess.task`, `sess.note`, and
  `sess.agent` verbatim into rendered markdown, letting an agent
  land arbitrary content into `pages/` by calling
  `kb.session_start(task=...)` and getting any one claim approved
  via crystallize. The summary body now contains only fields the
  proposing agent cannot influence (session id, server-clock
  timestamps, list of approved artifact ids). The
  `session.crystallize` audit event now also includes the summary
  page id in `object_ids` when a page is written, so `vouch audit`
  truthfully attributes the write.
- `context._retrieve` now honors `retrieval.backend` in `config.yaml`
  instead of always running embeddings first (#92). Accepts `auto`
  (default — embedding → FTS5 → substring), `embedding`, `fts5`, or
  `substring`; a legacy `retrieval.backends` list is still read for
  back-compat. `vouch init` now writes `retrieval.backend: auto`, and the
  README/ROADMAP describe the actual behavior.
- `vouch crystallize` now indexes its session-summary page into FTS5 so it
  surfaces from `vouch search` / `kb.search` / `kb.context` without a
  `vouch index` rebuild. Previously the summary was written via
  `store.put_page()` only, so on KBs with a populated `state.db` it was
  silently absent from search results (#60).
- Bundle export uses POSIX `/` separators in `manifest.json` and tar member
  names on every platform. Previously on Windows the manifest stored
  `sources\<sha>\meta.yaml` while the tarball stored `sources/<sha>/meta.yaml`,
  so `vouch export-check` returned `ok: false` on the bundle vouch had just
  produced, `manifest.counts` was always zero, and `vouch import-apply` was
  a silent no-op. Existing Linux/macOS bundles are unchanged (their paths
  were already POSIX); Windows bundles produced before this fix should be
  re-exported.
- `kb.context` no longer returns claims whose status is `archived`,
  `superseded`, or `redacted` (#78). Two compounding bugs were combining
  to leak retracted knowledge back to agents: `build_context_pack` had
  no status filter, and `store.update_claim` only refreshed the
  embedding cache without keeping `claims_fts.status` in sync — so even
  after `lifecycle.archive` / `supersede` / `contradict`, the FTS5 row
  kept its first-index status and `kb.search` / `kb.context` matched the
  retracted claim. `update_claim` now re-indexes the FTS5 row (mirroring
  what `proposals.approve` does on first index), and
  `build_context_pack` drops retracted claims from the assembled pack.
  `CONTESTED` claims continue to surface so contradictions remain
  visible.
- `bundle.import_check` and `bundle.import_apply` now verify each tar
  member's `sha256` against `manifest.json` (#74). Previously the
  per-file hash was only enforced by `export_check`; the import side
  trusted any tar member whose path appeared in the manifest, so a
  tampered tarball with an unchanged manifest could land
  attacker-controlled content into the KB while the audit log
  recorded a clean `bundle.import` event. `import_apply` re-verifies
  at write time and raises on mismatch, so a bundle that is tampered
  with between `import_check` and the apply re-open is rejected
  before anything reaches disk and the audit log does not record
  a `bundle.import` event.
- `Claim.evidence` now enforces "at least one citation" at the model
  layer via a `@field_validator` (#81). Previously the
  README-documented guarantee ("Claims must cite sources … a claim
  without at least one Source/Evidence id is a validation error")
  was enforced only in `proposals.propose_claim`, so every other
  write path — direct `store.put_claim`, `store.update_claim`, and
  `bundle.import_apply` via `_validate_content` — silently accepted
  `evidence: []` and landed an uncited claim. The validator closes
  all three paths at once; `store.update_claim` additionally
  re-validates via `Claim.model_validate(...)` before persisting so
  in-place mutation (`c.evidence = []; store.update_claim(c)`)
  also raises before the YAML hits disk. **Migration note:** because
  the validator also fires when claims are read back, a KB that
  already has an uncited `claims/<id>.yaml` on disk from before this
  fix would otherwise crash `vouch lint` / `vouch doctor` with a
  `pydantic.ValidationError`. `vouch lint` now iterates `claims/`
  per-file and surfaces unparseable / uncited YAMLs as
  `invalid_claim` findings ("edit the YAML to add a citation, or
  delete the file") instead of bailing out — so existing KBs get a
  clean repair list rather than a traceback.

## [0.0.1] — 2026-05-17

Initial alpha. Surface intentionally small; expect breaking changes pre-1.0.

### Added
- Object model: `Source`, `Evidence`, `Claim`, `Page`, `Entity`, `Relation`,
  `Session`, `Proposal`, `AuditEvent`.
- `.vouch/` repository layout with `claims/`, `pages/`, `sources/`,
  `entities/`, `relations/`, `evidence/`, `sessions/`, `proposed/`,
  `decided/`, `audit.log.jsonl`, `state.db`, `config.yaml`.
- Review gate: agents file proposals; humans approve with `vouch approve`.
  `proposed/` gitignored so rejected drafts never enter history.
- CLI: `init`, `discover`, `capabilities`, `status`, `lint`, `doctor`,
  `pending`, `show`, `approve`, `reject`, `propose-*`, `source add/verify`,
  `supersede`, `contradict`, `archive`, `confirm`, `cite`,
  `session start/end`, `crystallize`, `search`, `context`, `index`,
  `audit`, `export`, `export-check`, `import-check`, `import-apply`,
  `serve`.
- Transports: MCP over stdio and newline-delimited JSON (JSONL).
- SQLite FTS5 index, rebuildable from files.
- Append-only audit log, JSONL.
- Portable bundle export/import (tar.gz + manifest with per-file sha256).
- Content-addressed sources; evidence registration de-duplicates.
- Claim validation: at least one source/evidence citation required.
- Per-agent attribution via `VOUCH_AGENT` env var.

[Unreleased]: https://github.com/vouchdev/vouch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vouchdev/vouch/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/vouchdev/vouch/releases/tag/v0.0.1
