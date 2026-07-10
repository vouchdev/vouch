# Changelog

All notable changes to vouch are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added
- `kb.activity` read method (+ `vouch activity` CLI mirror): audit-log
  activity buckets for dashboards — per-day counts with proposal/decision
  breakdowns, an hour-of-week matrix, and actor/event histograms. windowed
  in viewer-local calendar days (IANA `tz` or a fixed utc offset), scope-
  filtered like `kb.audit`.
- console Dashboard view: 12-month activity calendar, last-30-days bars,
  hour-of-week heatmap, top actors and event mix, driven by `kb.activity`.

## [1.2.2] — 2026-07-07

### Packaging
- published to the mcp registry (`registry.modelcontextprotocol.io`, mirrored
  at `github.com/mcp/vouchdev/vouch`) as `io.github.vouchdev/vouch`. a
  `server.json` at the repo root carries the metadata; the pypi `vouch-kb`
  package is the artifact, run over stdio via `uvx vouch-kb serve`.
- `vouch-kb` console-script alias (alongside `vouch`) so `uvx vouch-kb serve`
  resolves — the registry launches a package by its pypi identifier, which
  otherwise wouldn't match the `vouch` script name.
- README carries an `<!-- mcp-name: io.github.vouchdev/vouch -->` marker;
  the registry verifies package ownership by matching it against `server.json`.

## [1.2.1] — 2026-07-06

### Fixed
- `vouch --version` (and `__version__`) reported 1.1.0 from the released
  1.2.0 wheel — `src/vouch/__init__.py` was a fourth version site nothing
  kept in step. the manifest lockstep test now ties it to pyproject.toml,
  openclaw.plugin.json and package.json. (pypi's 1.2.0 dists are immutable,
  hence this patch release.)

### Packaging
- container image: the docker build context now carries `adapters/`
  (dockerfile copy + dockerignore), which the wheel force-include requires
  — without it `pip install` failed the 1.2.0 image build with "Forced
  include not found", so no 1.2.0 images were published.

## [1.2.0] — 2026-07-06

### Added
- `vouch compile` — the llm-wiki ingest pass: a deployment-configured LLM
  (`compile.llm_cmd` in `.vouch/config.yaml`) drafts topic pages from live
  approved claims; every inline `[claim: …]` marker and `[[wikilink]]` is
  verified mechanically against the store, and surviving drafts are filed as
  PENDING page proposals by the `wiki-compiler` actor. never approves — the
  review gate is the ingest review. `--dry-run`, `--max-pages`, `--llm-cmd`,
  `--json`. see `docs/compile.md`.
- review-ui: a **compile wiki** button on the queue (shown once
  `compile.llm_cmd` is configured) runs the same ingest pass and lands the
  drafts in the queue; success and per-draft drop counts surface as a notice.
- company-brain template: `vouch init --template company-brain` declares
  typed record kinds (contact, org, project-record, meeting-notes, followup,
  decision-record, voice) as `page_kinds` config and seeds a cited guide
  page. operator-declared kinds always win; the merge is additive and
  idempotent. see `docs/company-brain.md`.
- `vouch init --template <name>` dispatches the onboarding template registry
  (starter stays the default; templates layer on top of it).
- frontmatter filters on `kb.list_pages` across mcp/jsonl/cli: kind equality,
  field equality, and inclusive ordered bounds (numbers, iso dates), plus the
  `vouch pages` human mirror. a viewport over `store.list_pages()`, not a
  query language.
- `kb.digest` / `vouch digest`: read-only reviewer briefing — pending
  proposals oldest-first, recent decisions, stale claims, followups due, and
  citation coverage. `--format text|json|markdown`; writes nothing, so it is
  safe to run from cron.
- five company-brain slash commands in the claude-code adapter (mirrored to
  the plugin skills list): `/vouch-ask`, `/vouch-remember`, `/vouch-record`,
  `/vouch-followup`, `/vouch-standup`. every flow terminates at
  `kb_propose_*` — none may call `kb_approve`.
- `vouch source fetch <url>`: snapshot a url's exact bytes as a
  content-addressed source so claims cite immutable evidence. conservative
  intake: http/https only, redirects re-validated, private-network hosts
  refused, 2 mib cap, `fetched_at` recorded in source metadata.
- `vouch inbox --dir <path> [--watch]`: dropped `.md`/`.txt` files become a
  registered source plus one pending page proposal each — mechanical, no
  model in the loop, never approves. content-hash seen-state makes re-runs
  idempotent; bounded stdlib poll, no daemon.
- `vouch notify sweep|test`: config-declared reviewer webhooks
  (`proposal.created`, `queue.backlogged`, `proposal.aged`) with optional
  hmac-signed envelopes and `env:` secret refs. read-and-notify only;
  best-effort delivery; idempotent per (event, proposal).
- protected page kinds: `page_kinds.<kind>.protected: true` exempts a kind
  from the `trusted-agent` self-approval opt-out — its pages always need a
  reviewer other than the proposer. the template marks `voice` and
  `decision-record` protected.
- string-typed frontmatter schema fields now accept yaml's native
  date/datetime scalars, fixing approve-time re-validation of pages whose
  frontmatter round-tripped through disk (e.g. `due_at: 2026-07-01`).

### Fixed
- installer: the curl one-liner no longer dead-ends on pep 668 hosts
  (debian 12+, ubuntu 23.04+, homebrew python). when `pip install --user
  pipx` is refused, pipx is hosted in a private venv under
  `~/.local/share/vouch/pipx-venv` — still no sudo. re-runs recreate that
  venv (brew pythons ship read-only activate scripts), an existing
  `~/.local/bin/pipx` is preferred and never overwritten, and installer
  failures now print the actual pip/venv errors instead of a guess.
- cli: non-utf-8 locales (e.g. `LANG=en_US.ISO-8859-1`) crashed
  `vouch status` / `vouch search` / `vouch --help` with UnicodeEncodeError
  on the `•` / `…` / `—` output glyphs. stdio is reconfigured to utf-8
  (`errors="replace"`) at module import — before click renders eager help —
  covering the mcp/jsonl servers too; the last locale-dependent file i/o
  sites (capture/themes config reads, the migration rewriter) pin
  `encoding="utf-8"`.
- `vouch install-mcp <host>`: works from pip/pipx installs. adapter
  templates now ship inside the wheel (`vouch/adapters/`) and the resolver
  falls back to that packaged copy when no repo checkout is present.
  previously every installed copy failed with "unknown adapter …
  (available: (none))"; source checkouts keep resolving the repo's
  `adapters/` directory.

## [1.1.0] — 2026-07-03

### Added
- auto-capture: claude code sessions are harvested via hooks and filed as a
  single pending session-summary proposal for human approval. a `PostToolUse`
  hook (`vouch capture observe`) appends compact tool-use observations to an
  ephemeral, gitignored `.vouch/captures/<session>.jsonl` buffer; a
  `SessionEnd` hook (`vouch capture finalize`) rolls the buffer plus a git-diff
  backstop into one `session` page proposal — mechanical, no llm, and never
  auto-approved. a `SessionStart` banner (`vouch capture banner`) nudges the
  next session when captured summaries await review. opt out with
  `capture.enabled: false` in `.vouch/config.yaml`.
- session-start recall: a `SessionStart` hook (`vouch recall`) injects a digest
  of every live approved claim (`[id] text`) plus approved page titles into a
  new claude session's context, so it starts aware of the reviewed KB. only
  approved knowledge is emitted; archived / superseded / redacted claims are
  excluded; size-guarded by `recall.max_chars` with an explicit truncation
  notice. opt out with `recall.enabled: false`.
- `vouch install-mcp claude-code` now merges its hooks and read-only permission
  allowlist into an existing `.claude/settings.json` (a `json_merge` install
  strategy) instead of skipping it, so the capture / recall hooks land on
  projects that already have a settings file. idempotent; user entries are
  preserved.
- `vouch new <kind>` — scaffold a typed page or entity proposal from the
  page-kind registry: stubs required frontmatter fields, supports
  `--field key=value`, `--interactive`, `--dry-run`, and `--json`; entity
  kinds (`person`, `project`, …) route to `propose_entity`, with page kinds
  taking precedence on name collisions unless `--entity` is set (#330).
- GitHub PR auto-labeling: a pull-request metadata-only labeler workflow now
  applies vouch surface labels from `.github/labeler.yml`, keeps those labels
  in sync as files change, and adds OpenClaw-style `size: XS` through
  `size: XL` labels based on non-doc changed lines. Maintainers can also run
  it manually to backfill labels on already-open PRs.
- `vouch detect-themes` — cross-session pattern detection via deterministic
  entity co-occurrence scoring. `kb.detect_themes` is read-only (returns
  ranked clusters); `kb.propose_theme` routes synthesis pages through the
  review gate so they appear in `kb.list_pending`. Supports `--propose` for
  one-shot propose-all and `--json` for machine-readable output. Configurable
  via `themes.min_sessions`, `themes.min_claims`, `themes.top_k`, and
  `themes.enabled` in `config.yaml` (#311).
- dual-solve JSON, review-ui job, and choose responses now include
  `changed_files` for each candidate and the kept branch, so desktop and browser
  clients can show the resulting files without parsing unified diffs.

### Changed
- `vouch dual-solve --sandbox` default docker image is now
  `vouch/coder:latest` (was `amika/coder:latest`).

### Fixed
- `vouch pending` (and every bulk `list_*` path) no longer crashes when a
  single artifact file is unreadable — a corrupt or mojibake yaml is skipped
  with a warning instead of aborting the whole listing.
- all text-mode file i/o under `src/vouch/` now pins `encoding="utf-8"`, so a
  non-utf-8 locale (e.g. latin-1) can no longer mangle non-ascii claim text
  into raw control bytes that the yaml loader rejects, nor crash on write.

### Packaging
- restored the tag-triggered `release.yml` workflow that was accidentally
  deleted alongside unrelated files in the #95 squash. It publishes to PyPI
  via Trusted Publishing (OIDC) exactly as before, and now also creates the
  GitHub release for the tag with the built sdist and wheel attached and the
  matching CHANGELOG section as the release body.
- restored the `vouch-kb` distribution name in `pyproject.toml` — the same
  #95 squash had reverted it to `vouch`, which PyPI rejects (the name belongs
  to an unrelated project, and the trusted publisher is registered for
  `vouch-kb`). The installed command is still `vouch`.
- container image: every release now also pushes `ghcr.io/vouchdev/vouch`
  (linux/amd64 + linux/arm64, tagged `X.Y.Z`, `X.Y`, and `latest`). The
  entrypoint is the `vouch` CLI with the stdio MCP server as the default
  command; bind-mount the project root at `/data`. Built from the new
  repo-root `Dockerfile`; installs the `web` extra, leaves embeddings out.
- the `[1.0.0]` section below was restored: a merge after the release folded
  its entries back under `[Unreleased]`, dropping the version header.

### Docs
- example KBs now carry their own screenshots: `examples/README.md` and the
  `tiny/` + `decision-log/` READMEs embed terminal renders of `vouch status`,
  `search`, `show`, `audit`, and a supersession `diff` against the shipped
  fixtures, so a reader can see what vouch looks like before installing it.
  Images live under `docs/img/examples/` and are generated deterministically
  from the fixtures by `docs/img/examples/render.py` (`make
  examples-screenshots`); `tests/test_example_screenshots.py` asserts the
  committed SVGs stay reproducible (#286).

## [1.0.0] — 2026-06-26

### Added
- `vouch dual-solve <issue-url>` — run claude + codex on one github issue in
  isolated git worktrees, compare the two diffs, keep the branch you pick, and
  propose the chosen solution's rationale into the KB. A sibling tool to
  `auto-pr`, and the first that writes to the KB — but only ever as review-gated
  proposals: the winning commit is registered as a `Source` and the decision
  plus up to three approach claims land in `proposed/`, so approval still
  requires a human `vouch approve`. Nothing is auto-approved. `--json` is
  non-interactive (emits both diffs, keeps both branches); `--no-record` and
  `--dry-run` propose nothing. Each phase (fetch, ground, and per-engine run
  with elapsed time and diff size) reports progress to stderr while it works.
- `vouch dual-solve --sandbox` and
  `vouch review-ui --dual-solve-sandbox` — run Claude Code and Codex inside a
  Docker image (default `amika/coder:latest`) while leaving git/GitHub commands
  on the host. The sandbox runner mounts only each candidate worktree plus a
  temporary copied home containing known Claude/Codex credential files, so agent
  writes stay in the throwaway dual-solve branches and host credential files are
  not modified.
- `vouch review-ui --allow-dual-solve` — a browser SPA that runs `dual-solve`
  on a github issue link, streams progress over the review-ui's websocket, shows
  both engines' diffs side by side, and lets you pick the winner. Off by default;
  localhost-first; edit-only over http; the pick keeps the branch and proposes
  the rationale into the KB through the existing review gate (nothing
  auto-approves). See `proposals/VEP-0006-dual-solve-web.md`.
- `vouch auto-pr <repo-url>` — open N mergeable PRs against any github repo.
  Sources open issues first then agent-discovered improvements, bootstraps a
  contribution skill from the repo's merged PRs when it ships no guidance, and
  cross-verifies each diff by alternating claude/codex as fixer and the other
  as reviewer; a PR opens only when the repo's own test gate is green and the
  reviewer signs off. A sibling tool — it never writes to the KB or the review
  gate. Paired with the `auto-pr` skill.
- typed page kinds (#234): a KB can declare extra page kinds in
  `.vouch/config.yaml` under `page_kinds`, each with `required_fields`, a
  JSON-Schema-subset `frontmatter_schema`, `required_citations`, and one level
  of `extends`. `kb.propose_page` now takes a `metadata` frontmatter dict and
  validates the kind at both the propose and approve gates, surfacing one error
  per offending field. The built-in `PageType` kinds keep working unchanged.
  New `vouch schema list` and `vouch schema sync` commands inspect declared
  kinds and audit existing pages against them; `propose-page` gains `--kind`
  and repeatable `--meta key=value`.
- `kb.synthesize` — answer-mode retrieval over the review-gated KB. Answers a
  query in prose from approved claims only, with an inline `[claim_id]`
  citation behind every sentence, an explicit `gaps` block listing query
  topics no approved claim covered, and a `synthesis_confidence` grade derived
  from the cited claims' lifecycle status. Deterministic in v1 (no LLM in the
  loop). Exposed across the CLI (`vouch synthesize`), MCP (`kb_synthesize`),
  and JSONL (`kb.synthesize`) surfaces (#222).
- `_meta.vouch_trust` on every dict-shaped kb.* response: `{remote, caller_kind,
  auth_subject}` so clients can detect remote confinement and surface it in
  their UI. HTTP MCP calls report `remote: true, caller_kind: mcp_http`; CLI
  `--json` reports `remote: false, caller_kind: cli`. Bearer-authenticated
  HTTP calls include a stable token fingerprint as `auth_subject` (#233).
- `vouch-context` OpenClaw context engine (#228): `src/vouch/openclaw/context_engine.py`
  wraps `kb.context` retrieval, the entity-salience reflex, and session hot
  memory into a cited `systemPromptAddition` on every `assemble()`. The plugin
  manifest declares `contracts.contextEngines: ["vouch-context"]` and registers
  `adapters/openclaw/vouch-context-engine.mjs`; engine identity is advertised
  on `kb.capabilities.context_engines`. Compaction stays delegated to the
  legacy OpenClaw runtime (`ownsCompaction: false`).
- Entity-salience retrieval reflex: a per-session, in-memory ring buffer of
  recent caller queries drives a zero-LLM substring/FTS entity pass that
  attaches top-K matched claim candidates as `_meta.vouch_salience` on
  `kb_context` read responses. Config-gated via `retrieval.reflex`
  (`enabled`/`window`/`top_k`); the buffer is never persisted and resets on
  `session_end` (#223).
- `vouch eval recall <queries.jsonl>` — score `kb.context` retrieval against a
  labeled query set with pure-Python P@k / R@k / MRR / nDCG, compare against a
  committed `eval/baseline.json`, and fail CI on a P@5 regression beyond
  tolerance (default 5%). Ships a starter labeled set, a reproducible fixture
  KB under `eval/fixture-kb/`, and an `eval` workflow gating retrieval changes
  (#226).
### Fixed
- `parse_since` (the `--since` parser behind `vouch metrics`/`vouch audit`) now raises a clean `MetricsError` for a duration too large to represent (e.g. `--since 1000000000000d`), instead of letting an uncaught `OverflowError` traceback escape — restoring the documented "clean error, not a traceback" contract.
- `sync_apply` now loads the sync source exactly once and passes the same `_SyncSource` instance into `sync_check`, closing a TOCTOU window where a bundle replaced on disk between the two `_load_source` calls could cause the validation and write phases to operate on different snapshots. Also eliminates redundant directory walks (KB sources) and triple tarball opens (bundle sources). Fixes #217.
- `vault_to_kb` now passes `slug_hint=page_id` to `propose_page` so vault edit proposals target the existing page id from frontmatter instead of a slugified copy of the title (fixes #219).
- `vault_to_kb` skips mirror files whose page no longer exists in the KB, preventing ghost-page proposals that would fail on approve (fixes #219).
- `vault_to_kb` skips filing a second proposal when a pending proposal already targets the same page id (with differing body), preventing duplicate proposals on repeated sync runs before approval (fixes #219).
- `vault_to_kb` now warns when a user edits a claim stub instead of silently dropping the edit, directing the user to edit the citing page instead, and reports it via the dedicated `claim_stubs_edited` field on `VaultSyncResult` (fixes #219).
- `approve()` now supports updating an existing page via `KBStore.update_page` when a PAGE proposal's id matches an existing artifact (the vault-edit flow), instead of raising `cannot approve: page already exists` for every vault edit (fixes #219).

### Fixed
- `vouch serve` now fails fast with a clear `vouch init` hint when no `.vouch/` KB is present, instead of starting a server that immediately misbehaves (#95).

### Added
- `kb.volunteer_context` — confidence-gated push context for active sessions.
  `kb.session_start(task=…)` opens a background watch on retrieval salience;
  when an approved claim's normalized relevance exceeds the configured
  threshold (default `0.85`), vouch queues `{claim_id, relevance, why}` and
  emits an MCP notification (`kb.volunteer_context`). JSONL and CLI clients
  poll via `kb.volunteer_context` / `vouch session volunteer`. Pushes are
  throttled (default 30s) and respect scope visibility (#236).
- Auto-extracted typed edges: approving a page now files `mentions` (wiki-links),
  `relates_to` (entity frontmatter), and `derived_from` (source frontmatter)
  relation proposals automatically, tagged `proposed_by: vouch-extractor`.
  They land in `proposed/` like any hand-filed relation and need the usual
  review; `vouch reject-extracted [--page <id>]` mass-rejects them (#224).
- Visibility-aware `kb.audit` / `vouch audit`: audit reads accept optional
  `project` / `agent` viewer context (or nested `viewer_scope` on JSONL).
  Events whose `object_ids` reference scoped claims, sources, or claim
  proposals outside the viewer context are filtered out; events with no
  `object_ids` remain visible to everyone (#232).
- `vouch install-mcp openclaw` — ninth host in the adapter catalogue.
  Declares plugin enablement (`.openclaw/plugins.json`), an `AGENTS.md`
  fenced snippet, the four slash commands reused in place from the
  `claude-code` adapter, and a project-local trust-boundary policy
  (`.openclaw/policy.json`). Complements the repo-root
  `openclaw.plugin.json` bundle manifest, which covers loading vouch into
  an OpenClaw deployment rather than into one managed project (#230).
- `vouch sync --vault <dir>` — bidirectional sync between the KB and an
  Obsidian/Logseq-style markdown vault. Forward (vault → KB): edits to
  `<vault>/vouch/pages/<id>.md` become page-edit proposals citing a
  `vault:<relpath>` source. Backward (KB → vault): approved pages mirror
  to `<vault>/vouch/pages/` and approved claims surface as markdown stubs
  in `<vault>/vouch/claims/` with Obsidian wikilink backlinks to citing
  pages. `--watch` keeps a polling loop alive; `--direction` lets you
  run forward-only or backward-only. The starter KB now seeds an
  approved "Edit in Obsidian" walkthrough page so new users discover the
  workflow the moment they `vouch init` (#181).
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
- `vouch stats` and `kb.stats` expose read-only KB observability: pending
  proposals by agent (with median/max age), review decision counts and
  approval rate over a configurable window (`--days`, default 30; `0` for
  all-time), citation coverage (valid / invalid / broken), plus audit-log
  cross-check totals. Available on MCP, JSONL, and HTTP transports.
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
- `put_claim` / `update_claim` now reject a Claim whose `entities`,
  `supersedes`, `superseded_by`, or `contradicts` reference an artifact that
  is not in the KB, via a new `KBStore._validate_claim_refs`. `bundle.import_check`
  gains the matching check so a bundle can no longer land a claim with dangling
  graph refs through `import_apply`'s direct write. Previously only `claim.evidence`
  was checked: the graph-integrity fix for Relations/Pages (#124) skipped the
  Claim model's own four reference fields, even though `fsck` already declared
  `dangling_supersedes` / `dangling_superseded_by` / `dangling_contradicts` as
  error-severity findings — the invariant was articulated but enforced by no
  writer. Same model-layer/storage pattern as #81 / #123. Closes #196.
- `lifecycle.supersede` / `lifecycle.contradict` pre-validate both touched
  claims before the first disk write so a legacy dangling ref on either
  side can't half-apply the operation (one update written without the
  reciprocal, no relation, no audit event).
- `proposals.check_approvable` dry-runs the put_*-side ref guards so the
  default `vouch approve a b` batch flow (#93) catches a dangling
  `claim.entities` (or relation endpoint / page reference) before any
  disk write, preserving the all-or-nothing contract.
- `vouch fsck` reports `claim.entities` pointing at a missing entity as a
  `dangling_claim_entity` error finding, alongside the existing
  `dangling_supersedes` / `_superseded_by` / `_contradicts` checks.
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

[Unreleased]: https://github.com/plind-junior/vouch/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/plind-junior/vouch/releases/tag/v0.0.1
