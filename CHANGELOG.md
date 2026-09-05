# Changelog

All notable changes to vouch are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added
- **`kb.backlinks` — the wiki's link graph, agent-facing** (roadmap 1.4):
  `wiki_render.backlinks()` already computed the inbound-link map internally
  (used by `render_moc`'s ranking), but nothing exposed it — no MCP tool, no
  JSONL handler, no CLI command, only reachable indirectly via
  `vouch render-wiki`'s rendered markdown. `kb.backlinks` (MCP `kb_backlinks`,
  JSONL `kb.backlinks`, CLI `vouch backlinks [page_id]`) returns it directly:
  with a `page_id`, that page's inbound *and* outbound `[[wikilink]]` titles
  (`outbound_links`, a new sibling to `backlinks` in `wiki_render.py`); with
  none, the full inbound map. Archived pages are excluded from the checked
  set and treated as unresolvable link targets, matching `render-wiki`'s own
  exclusion policy (#695) — a link to an archived page is exactly as dead as
  a link to nothing. Read-only, like every other `wiki_render` view — never
  proposes, writes, or mutates.
- **bench: composite guards** (#616): `efficiency`, `consistency` and `canary`
  as bounded multipliers over the composite, plus a `bench_version` stamp on
  every report. Reported **beside** the composite, never folded into it —
  `composite` keeps its exact formula and meaning, so no recorded score or
  ladder entry becomes incomparable, and `composite_guarded` is the new
  measurement the ladder can adopt at a season boundary of the maintainer's
  choosing. `run_seeds` tolerates a version-1 report with no guard block.
  Measured on seeds 1-3: efficiency 0.88, consistency 1.00, canary **0.50 —
  tripped on every seed** (leak rate 0.08-0.24), because a 10-item pack over
  this corpus carries ~24% of it. That is the lever the guard exists to expose.
- **pdf and audio sources — page and timestamp receipts** (#613): a spec, a
  paper, a recorded call could not become citable material, because a receipt is
  a byte span into a source's stored bytes and the bytes of a pdf or an mp3 do
  not spell the sentence anyone wants to quote. `vouch source add spec.pdf` (or
  `call.mp3`) now extracts the text layer / transcript, stores *that* as the
  content-addressed artifact, and records a **coordinate map** alongside it, so
  every existing path — ingest, the receipt gate, `kb.source_verify`, receipt
  coverage — works on it unchanged while a verified receipt also resolves to
  `p7` or `t=00:14:23` in the original. `vouch source locate <id> <quote>` prints
  that coordinate; `--raw` registers the binary untouched. **No new hard
  dependency**: pypdf is the optional `[pdf]` extra, imported lazily, and
  transcription is a configured command (`sources.transcribe_cmd`, the
  `compile.llm_cmd` pattern) so vouch never bundles a speech model. A scanned pdf
  with no text layer fails loudly rather than registering an empty source — ocr
  is out of scope. The original's sha256 is recorded, so `vouch source verify`
  re-checks the pdf or the recording for drift instead of losing the link back to
  it the way extracting by hand does.
- **cascade delete — the referrers ride along in the proposal** (#600):
  `kb.propose_delete(..., cascade=true)` and `vouch propose-delete --cascade`.
  `referenced_by()` refuses a delete while anything still points at the target,
  which is correct but leaves most of a compiled kb undeletable — pages cite
  claims in bulk, and a supersede pair is *mutually* locked (`b` lists `a` in
  `supersedes`, `a`'s `superseded_by` points back at `b`), so neither end of a
  chain could ever be removed by any delete ordering. The gate is unchanged;
  what changes is what the reviewer is asked to approve. With `cascade`, the
  required referrer edits are recorded in the payload as a plan, and
  `_approve_delete` **re-derives** that plan at approve time — the same posture
  as the existing ref re-check — applies it, and only then deletes, so the
  approve-time `referenced_by` gate still has to come back empty. Pages and
  claims lose their pointer (frontmatter *and* the inline `[claim: …]` body
  markers, via the same `strip_claim_markers` helper `wipe_dead_refs` uses);
  relations are deleted outright, because an edge whose endpoint is gone has no
  meaning, and relations carry no inbound refs of their own, so the walk is one
  level deep by construction with no transitive cascade to bound. Every edit
  lands its own irreversible audit event (`page.cascade_unlink`,
  `claim.cascade_unlink`, `relation.delete`) and the `{kind}.delete` event names
  what it touched. Additive and default-off: `cascade` omitted reproduces
  today's behaviour exactly, and the refusal message now names the flag so the
  dead end is discoverable.
- **correction capture — the pushback becomes a proposal** (#430): the adapter
  captured tool *outcomes* passively but never the single highest-signal event
  in a session, the user correcting the agent ("no, we deploy from `main` not
  `release`"). That evaporated unless someone remembered to propose a claim
  afterwards. `kb.capture_correction` detects pushback on the turn boundary
  with a cheap regex heuristic — no LLM call, deterministic — and files it as a
  **pending** claim proposal tagged `auto:correction`, wired into the existing
  `UserPromptSubmit` hook so it needs no new plumbing. It proposes and never
  writes: the module routes exclusively through `proposals.propose_quoted_claim`
  and has no import of `approve` at all. The claim cites a receipt — the user's
  message is registered as a `message` source and the corrective sentence is
  quoted verbatim out of it — so what reaches the queue is mechanically
  verifiable rather than a paraphrase. Three guards bound an over-eager
  heuristic: a per-session cap (`capture.correction.max_per_session`, default
  3) counted from the queue so it survives a restart, lexical dedup against
  approved claims and pending corrections folded together with the #147
  embedding path, and secret masking before anything durable is written.
  `capture.correction.enabled` (default true) gates it; declines report
  `{"captured": false, "reason": ...}` rather than failing silently.
  `vouch capture-correction`, plus MCP and JSONL.
- **operator profile page** (#614): `vouch compile --profile` drafts a single
  "how this operator works" page from approved claims and files it PENDING like
  any other page. Selection is **opt-in, never inferred** — a claim qualifies by
  carrying a `compile.profile_tags` tag (default `preference`, `convention`,
  `decision`, `correction`) or by naming `compile.profile_entity`, not by
  looking like a first-person sentence. The prompt forbids personality, trait
  and psychometric inference outright, and every substantive sentence must cite
  a claim or it is dropped, so the page can only ever restate what was already
  approved. A draft citing anything outside the selected set is refused. A
  refresh re-proposes rather than rewriting, so the history of what the system
  believed about you stays auditable instead of being silently mutated.
- **agent registry — who can write, and what each agent did** (#607):
  `vouch agents list / show / pause / resume / revoke`, keyed on the
  `auth_subject` hash `trust.py` already derives, so the committed
  `.vouch/agents.yaml` holds names, status, scopes and claim dates while the
  credential itself stays in local config. Pause and revoke are enforced at one
  chokepoint (`trust.authorized_bearer_token`), so MCP-over-HTTP and
  JSONL-over-HTTP inherit revocation without two implementations, and a denied
  token is indistinguishable from a wrong one. Revocation is terminal by
  design. `vouch agents show` replays every audit event an agent produced
  alongside the control-plane transitions applied to it — the per-action
  attribution ditto's own docs stop short of. Existing deployments are
  unaffected: an unregistered token still authenticates as an unnamed active
  agent, and a corrupted status fails closed rather than reading as active.
- **first-class goals — review-gated in-flight objectives** (#427): vouch could
  record everything a project *knows* and nothing about what it is *doing*, so
  an agent re-orienting after a compaction recovered facts and decisions but
  not intent ("mid-migration to typed config", "release blocked on the
  audit-race fix") — that lived as prose in a session summary, unqueryable.
  Adds a `Goal` artifact with a `GoalStatus` of `open` / `done` / `abandoned` /
  `blocked`, taking the same route as every other write: `kb.propose_goal`
  files a pending proposal, a human approves it, and the goal lands as diffable
  yaml under `.vouch/goals/`. Approval is pinned to `open` — a proposal cannot
  land a goal that is already `done`, which would put a transition on disk that
  never passed the lifecycle path. Every later move goes through
  `lifecycle.set_goal_status`, the single write path, which appends a
  `goal.status` event to `audit.log.jsonl` and a row to the goal's own
  append-only `history`. Open goals resurface oldest-first in `vouch digest`
  and in the SessionStart recall digest, so a returning operator or a fresh
  agent session sees what is in flight before it picks something up.
  `vouch propose-goal`, `vouch goals`, `vouch goal-status`, plus MCP and JSONL.
- **explicit pins — a working set that always enters the pack** (#615):
  `vouch pin <id>` / `vouch pins list` / `vouch unpin <id>`. Pinned claims and
  pages lead every context pack instead of having to win the query each turn,
  which is what `hot_memory` and `salience` cannot do — they are recency-driven
  and decay exactly when a long task needs them not to. Capped at
  `retrieval.pins.budget_share` (default 0.3) so pins can never starve
  retrieval, and de-duplicated against what retrieval already found. Pins are
  **not a gate bypass and not a permission**: they point at already-approved
  artifacts, and lifecycle and viewer scope are re-checked on every build, so a
  pinned claim that is later superseded/archived/redacted — or one the scope
  filter hides — stops being injected. Shared pins live in committed
  `.vouch/pins.yaml`; `--local` keeps a personal set in gitignored
  `.vouch/pins.local.yaml`. `--expires` drops a pin automatically, applied on
  read so building a pack never writes.
- **`kb.effectiveness` — is this claim earning its keep?** (#426): a read-only,
  measurement-only signal ranking approved artifacts by how the sessions they
  were surfaced into ended. Per artifact it reports good/bad session counts, an
  associational lift against the corpus baseline, and a 95% Wilson interval.
  Verdicts are power-gated — `useful` / `harmful` only when the interval clears
  the baseline *and* the sample meets `--min-samples`, otherwise `unverified` /
  `insufficient`, so an untrustworthy number never renders as a confident one.
  Built on the existing `retrieval_events` surfacing log and the audit stream;
  no new derived table, nothing written, and a bad verdict never expires
  anything. `vouch eval effectiveness [--window 90d] [--min-samples N]
  [--format text|json]`, plus MCP and JSONL.
- **`kb.explain_ranking` — why a result ranked where it did** (#432): a
  read-only breakdown of the retrieval pipeline. Per candidate it reports the
  lexical (FTS5) rank, the semantic rank, the RRF contribution, a row for every
  stage — fusion, scope and status filters, recency, pages-first, rerank, the
  pluggable strategy, the limit window, and the optional budget/citation gates
  — with the rank and score delta that stage caused, plus the gate that kept or
  dropped it (`kept` / `scope-filtered` / `status-filtered` / `limit-dropped` /
  `budget-dropped` / `uncited`). Registered on MCP, JSONL and the CLI
  (`vouch explain-ranking "<query>" [--format text|json]`). Viewer-scoped
  through the same `filter_hits` as `kb.context`, so it cannot expose an
  artifact the caller could not already retrieve, and it touches no write path.

### Fixed
- **`extract` no longer fractures file paths/URLs into auto-approved
  garbage claims** (#702): the sentence segmenter only skipped a `.` as a
  boundary when it was flanked by digits on both sides (decimals/versions
  like `6.8.3`) — every other non-whitespace-flanked period, e.g. in
  `src/vouch/cli.py`, `cli.py:2550`, or `github.com`, still split the
  sentence. `segment_source` has no coherence check afterward, only
  length/letter-ratio filters, so the resulting shards (`"see the
  changelog at github."`) got auto-approved via `propose_quoted_claim`'s
  receipt gate as first-class claims. The lookaround is now
  non-whitespace-flanked rather than digit-only, so a period only ends a
  segment when followed by whitespace or end-of-string.
- **vault sync no longer clobbers a second, distinct vault edit made while
  the first edit's proposal is still pending**: `_has_pending_page_proposal`
  dedup-checked pending proposals by page id alone, so re-running
  `vault_to_kb` after a *different* edit to an already-pending page
  silently skipped filing a new proposal instead of recognizing the edit
  as distinct. The second edit was never captured in any proposal, and
  the next backward sync pass then overwrote the vault mirror with the
  KB's still-unapproved-first-edit content, discarding the second edit
  with no trace and no error. Now keyed on the content-address (sha256)
  of the whole edit rather than the page id alone, matching how sources
  are already fingerprinted elsewhere, so a second distinct edit correctly
  files its own proposal instead of being coalesced into the first.
- **`kb.experts` no longer leaks out-of-scope claims into entity rankings**
  (#714): `rank_experts` aggregated evidence density over every claim in
  the KB with no viewer/scope filtering at all, unlike every sibling
  claim-aggregating read surface (`context.py`, `graph.py`, `digest.py`,
  `health.py`, `compile.py`, and `themes.detect_themes`, the closest
  shape-wise sibling). A `project`- or `agent`-scoped claim the caller
  cannot otherwise retrieve still inflated `claim_count`, `citation_count`,
  and `score`, and could surface verbatim in `top_claim_ids` — handing the
  caller a claim id it cannot fetch. `rank_experts` now takes an optional
  `viewer` (defaulting to `scoping.viewer_from(...)`, matching
  `detect_themes`) and filters through `scoping.is_visible` before a claim
  can contribute anything, with the FTS candidate fetch run through
  `scoping.scoped_fetch_limit` so a mostly-out-of-scope KB doesn't starve
  the candidate pool before the filter runs.
- **`vouch render-wiki` drops archived pages** (#695):
  `render_wiki_cmd` passed every on-disk page into index/MOC, so retired
  titles kept wiki links after archive. the CLI now filters to the same
  live set as recall / digest / search.
- **session-split ignores archived pages in TAKEN TOPICS / collisions** (#712):
  prompts and `_file_drafts` treated every on-disk page as taken, so archiving
  a session summary permanently blocked redraft under the same title. Both
  now reuse `compile._live_pages` (same live set as compile post-#700).
- **`kb.neighbors` drops archived pages** (#696):
  `_neighbor_ok` already filtered retracted claims but accepted any
  on-disk page, so archived titles still appeared in neighbors while
  context-pack expansion dropped them via `_page_is_live`. pages now
  use the same live check.
- **`kb.neighbors` no longer leaks edges pointing at excluded nodes**
  (#716): `find_neighbors` appended an edge to the response before
  checking whether its other endpoint passed the same
  retrievability/existence gate that decides node inclusion
  (`_neighbor_ok` / `_node_kind`). superseded, archived, and redacted
  claims — and missing nodes — were correctly excluded from `nodes`, but
  the edge pointing at them still went out, so a response could contain
  an edge whose `target` referenced an id the response itself said didn't
  exist. `kb.neighbors` shares this code path across all three surfaces
  (MCP, JSONL, CLI), so the leak was identical everywhere. an edge is now
  only recorded once its other endpoint has been accepted into the
  visited set — either already, or just now by passing the same gate.
- **`reset()`/`deindex()` now clear the legacy `embeddings` table too**
  (#543 reopened, root-caused): both functions' own docstrings promise to
  remove every embedding row for a reindex or a deleted artifact, but
  neither ever touched the legacy `embeddings` table alongside
  `embedding_index` — a leaked row permanently tripped `fsck`'s
  `orphan_embedding` warning with no way to clear it via reindexing, and
  grew `state.db` unbounded over a KB's lifetime.
- **`recall`/`capture` no longer crash on malformed numeric config
  values** (#488 reopened, root-caused): both `load_config()` functions
  passed `max_chars`/`min_observations`/`dedup_window_seconds` straight
  through bare `int()`/`float()`, raising on a config typo (e.g.
  `max_chars: "12,000"`) instead of falling back to the default like the
  same module's `enabled` boolean already does — and since
  `recall.load_config` backs the SessionStart hook, one bad value took
  down recall-digest injection on every new session. `compile.py`'s own
  `_coerce()` already implemented this fail-soft contract for its numeric
  fields; it's now the shared `coerce_numeric()` in `config_coerce.py`
  (alongside `coerce_bool()`), used by all three modules.
- **`kb.confirm`-ing a claim no longer drops it from the hot-memory
  sidebar** (#520 reopened, root-caused): `_is_active` listed only
  `WORKING`/`STABLE`/`CONTESTED` as live statuses, omitting `ACTIONABLE`
  — the status `lifecycle.confirm()`'s first confirmation moves a
  `WORKING` claim to. A claim disappeared from `_meta.vouch_hot_memory`
  the moment it was confirmed, and a fresh KB's onboarding seed claim
  (filed `ACTIONABLE` from birth) never appeared at all. `_is_active` is
  now the complement of the retired statuses (`SUPERSEDED`/`ARCHIVED`/
  `REDACTED`), matching `context.py`'s `_RETRACTED_CLAIM_STATUSES`
  pattern, so a future status addition defaults to active.
)
- **`vouch stats` / `kb.stats` no longer crash on one corrupt `decided/*.yaml`**:
  `_list_decided` parsed every decided proposal strictly, so a single bad file
  aborted `review_summary` / `collect_stats`. It now uses `_load_or_skip` —
  same resilience as `list_proposals` / `list_pages`.
- **rerank / recency / triage quoted `"true"` stays off** (#658):
  `retrieval.rerank.enabled`, `retrieval.recency.enabled` and
  `triage.enabled` were the last three readers still on the
  isinstance/`bool()` pattern, so a quoted `enabled: "true"` fell through to
  `False` while the sibling values (`top_k`, `half_life_days`) parsed fine
  and the block looked configured. all three now go through `coerce_bool`,
  finishing the migration `#620` started for `pages_first` in the same file
  and `#648` continued for themes / reflex. note the fail direction is the
  opposite of `#648`'s: there a quoted `"false"` left a feature on, here a
  quoted `"true"` left it off.
- **`hub_client` ETag lookup is now case-insensitive** (#662): `_request`
  flattened `resp.headers` (case-insensitive by design) into a plain
  `dict`, so `pull()`'s `resp_headers.get("ETag")` silently returned
  `None` whenever a hub or intermediary sent the header as `etag` rather
  than the exact literal `ETag`. That cleared `link.last_bundle_id`,
  permanently defeating `If-None-Match` dedup — every subsequent
  `vouch hub pull` re-downloaded the whole bundle and re-filed every
  claim as a fresh pending proposal, indefinitely. Header keys are now
  lower-cased on the way into the dict, and the one consuming lookup
  matches.
- **kind-aware relation match in `referenced_by`** (#663):
  relation endpoints are bare ids, so a claim↔claim edge on slug
  `auth` also blocked deleting a page (or entity) that shared the
  slug. the gate now resolves each endpoint to a kind (same priority
  as `_node_exists`) and only counts the relation when it matches the
  delete target's kind. the #600 cascade option is unchanged.
- **security: koth strategy sandbox now blocks filesystem mutation, not
  just `open`-writes** (#660): `_install_audit_hook` blocked `open()` in a
  write mode, but never the separate CPython audit events `os.remove`,
  `os.rename` (and `os.replace`), `os.mkdir`, `os.rmdir`, `os.link`,
  `os.symlink`, `os.chmod`, `os.truncate` — so untrusted competition
  strategy code could delete, rename, or create files/directories despite
  the sandbox's documented claim to block "filesystem writes." Confirmed
  end-to-end: a strategy's `rank()` could silently delete an arbitrary
  file via `os.remove` with no error, timeout, or blocked-call signal.
  All eight events now hit the same blocklist as `open`-writes.
- **`kb.detect_themes` no longer leaks claim and session ids the viewer
  cannot retrieve** (#657): the detector filtered claims on status and
  `approved_by` but never on `ArtifactScope`, so a private or cross-project
  claim contributed its id — and the session that produced it — to a
  returned `ThemeCluster`. `propose_theme` writes both lists into the theme
  page body, so the leak became committed yaml on approval rather than
  stopping at a response. `detect_themes` now filters through
  `scoping.is_visible` like `kb.recall` and the salience sidebar already do,
  and takes an optional `viewer` for callers that carry one.
- **security: empty-quote receipts no longer clear the auto-approve gate**
  (#513 reopened, root-caused): `verify_receipt` and `verify_evidence` both
  guarded only on `quote is None`, not an empty string. An `Evidence` with
  `quote=""` and a zero-length span (`byte_start == byte_end`) decodes to
  `""`, trivially string-equals the empty quote, and returned `VERIFIED` —
  so a claim citing only a forged, content-free receipt cleared
  `evaluate_claim_receipts` and, with `review.auto_approve_on_receipt`
  (the starter-config default), landed as a durable, approved claim with
  zero real evidentiary backing. `Evidence.quote` carries no min-length
  constraint at the model layer and `bundle`/`sync` intake write incoming
  Evidence straight to disk after schema validation only, so this was
  reachable from a hand-crafted bundle or a malicious federation peer, not
  just the normal propose path (which already routes through
  `locate_span`'s existing empty-quote guard on the *mint* side). Both
  guards now reject an empty quote the same way `locate_span` already
  does, closing the gap on the *verify* side.
- **`notify sweep`'s backlog alert re-arms after dropping below threshold**
  (#652): the `queue.backlogged` idempotence marker was a one-way latch,
  cleared only when the pending queue reached exactly zero — not when it
  dropped back under `backlog_threshold`. A queue that drained partway and
  grew again (the common shape of a real backlog) never re-alerted after
  the first trip. The marker now clears per-hook as soon as the count
  falls under threshold, so the next crossing fires again.
- **`kb.explain_ranking` no longer leaks status-filtered candidate text**
  (#650): a retracted/superseded/redacted claim or archived page correctly
  reported `gate: "status-filtered"`, but its `summary` was still sourced
  from the pre-status-filter candidate set, so the full text came back
  anyway. `#640` fixed the equivalent leak for scope-filtered candidates;
  this extends the same withholding to the status gate — summaries now
  come from the post-status-filter set, matching what `kb.search` and
  `kb.context` already exclude.
- **themes / salience quoted `"false"` disables** (#648):
  `themes.enabled` and `retrieval.reflex.enabled` still used the
  isinstance/bool fail-open pattern, so a quoted `enabled: "false"`
  silently kept both features on. both now go through `coerce_bool`
  (same fix class as enrich / hooks / split).
- **`mask_secrets` now catches underscore-adjacent credential key names**
  (#646): the `\b` word boundaries around the keyword alternation treat `_`
  as a word character, so `access_token=`, `client_secret=`, `DB_PASSWORD=`,
  and `AWS_SECRET_ACCESS_KEY=` — the dominant real-world naming shape for
  these env-vars — passed through unmasked. switched to explicit
  alphanumeric lookaround so underscore-delimited segments match while
  substrings like `tokenized=` stay excluded. affects both the capture-time
  guard and the `vouch redact` remediation backstop, which share the regex.
- **triage ignores archived twins for duplication** (#638):
  claim/page pools used every approved artifact, so an archived claim
  (or archived page title) with the same text forced `duplication_risk=1.0`
  and an advisory reject on re-file. pools and embedding hits now skip
  retracted claims and archived pages, matching search/recall/digest.
- **`setup_repo_guards.sh` no longer requires checks that cannot report**:
  `#630` removed the `trust-gate` workflow and the coderabbit gate removed
  `coderabbit-approved`, but both contexts stayed in the script's
  `required_status_checks`, so the provisioning source still declared two
  checks with no workflow behind them. a required context that never reports
  leaves the pr pending rather than failing, so it blocks with no red x to
  point at — every open pr into `test` is stuck this way today. the list is now
  the four ci contexts that actually run; re-running the script against the
  live ruleset clears the stale contexts.
- **hooks quoted `"false"` disables short_circuit / prompt_gate** (#631):
  `#620` / `#558` fixed most loaders, but `short_circuit_cfg` and
  `prompt_gate_cfg` still used bare `bool()`, so a quoted
  `enabled: "false"` silently turned those features *on*. both now
  use `coerce_bool`.
- **a zero `tail` on `kb.audit` returns no events, not every event**: the
  window was `events[-tail:]`, and `-0` is `0`, so asking for zero events
  sliced from the start and handed back the whole visible log — a negative
  tail dropped that many off the front and returned the rest. all three
  surfaces (mcp, jsonl, cli) now share `audit.tail_events`, so the clamp
  cannot drift between them. `retrieval_events.read_events` carried the same
  `[-limit:]` boundary and is fixed with it.
- **digest drops archived followup pages** (#625):
  `followups_due` already skipped `done`/`dropped` metadata, but an
  `ARCHIVED` page with `followup_status=open` and a past `due_at` still
  appeared every morning — stale claims were filtered, pages were not.
  mirror recall: archived followups leave the due list.
- **`kb.explain_ranking` no longer returns the summary of a scope-filtered
  candidate**: the report listed every candidate the pipeline saw, sourcing
  summaries from the pre-scope fused set, so a viewer got back the claim text
  of artifacts `kb.search` and `kb.context` withhold for that same viewer — the
  opposite of the module's stated scoping invariant. the candidate is still
  listed with its `scope-filtered` gate, since naming the gate that hid it is
  the point of the report; only the summary is withheld.
- **`verify_all` / `doctor` treat missing externals like drift** (#622):
  `vouch source verify` already marked `external_status=missing` as `!`,
  but `verify_all`'s audit `failed` list and `health.doctor` only looked
  for `drift`, so a deleted upstream file could leave doctor `ok: true`
  while the CLI failed. missing now joins the failed set and emits a
  `source_missing` warning.
- **salience reflex excludes retracted claims**: `compute_salience` scanned
  every claim regardless of status, so the `_meta.vouch_salience` sidebar
  counted `ARCHIVED` / `SUPERSEDED` / `REDACTED` claims in `claim_count` and
  could name one as an entity's `top_claim_id` — pointing agents at knowledge
  the archive/supersede/redact controls were supposed to retire. the scan now
  applies the same lifecycle filter as the scope filter beside it. an entity
  whose only claims are retracted still appears, reporting zero live claims.
- **config quoted `"false"` disables enrich / events / pages_first** (#620):
  `#558` left three loaders on bare `bool()`, so a quoted `enabled: "false"`
  left `capture.enrich` and `retrieval.events` on, and turned
  `retrieval.pages_first` on. all three now use `coerce_bool` like the
  other config loaders.
- **vault sync mirrors post-approve WORKING/DRAFT artifacts** (#583):
  `kb_to_vault` now includes durable `WORKING` claims and `DRAFT` pages
  (the propose+approve defaults), so Obsidian mirrors fill without
  hand-editing status. `ARCHIVED` pages and retracted claims stay out,
  and stale *mirror-owned* files are deleted (untracked user markdown
  under the vault house is preserved) so the vault cannot keep serving
  dead knowledge.
- **hot-memory sidebar still fills after exclude_ids** (#597): compute_hot_memory used to truncate to limit then drop excluded ids, so search/context sidebars under-filled whenever hit ids overlapped the hot set. exclusions are applied while ranking so the sidebar still returns up to limit other recent claims.
- **sandbox docker argv on Windows** (#582): omit `--user uid:gid` when
  `os.getuid` / `os.getgid` are unavailable so sandboxed dual-solve no
  longer raises `AttributeError` while building the docker command.
- **`kb.session_transcript`'s degraded-path test no longer depends on an
  ambient KB**: it called `handle_request` against whatever `.vouch/` the
  cwd happened to sit under, so it passed on a developer checkout and
  failed in CI — where `.vouch/` is gitignored — with an `internal_error`
  from `_store()`. it now chdirs into a fixture KB and points both
  transcript locators at empty dirs, so the absence it asserts is the
  raw transcript's.
- **`kb.search` excludes retracted claims and archived pages** (#581):
  `search_kb` now drops `ARCHIVED` / `SUPERSEDED` / `REDACTED` claims and
  `ARCHIVED` pages the same way `kb.context` already does, so lifecycle
  controls are not decorative on the detail-search surface. backends
  over-fetch a candidate pool before that filter so retracted top-hits
  cannot starve the requested result limit.
- **bench grading saw highlight markup**: retrieval wraps query-matched
  terms in guillemets, which broke the bench's substring checks exactly
  on query-relevant claims — expected values read as missing (deflating
  recall categories) and highlighted forbidden values slipped past the
  zeroing (inflating dump-guard categories). grading now strips the
  markers; absolute bench scores shift, paired comparisons were fair
  either way. the reference baseline table is refreshed.
### Changed
- **capture.realtime defaults off** (#602):
  per-tool `PostToolUse` observe is opt-in. when off (the new default),
  `capture observe` no-ops with `{"skipped": "realtime-disabled"}` and
  SessionEnd rebuilds tool activity from the Claude transcript so
  `min_observations` still works. shipped claude-code hooks drop
  PostToolUse/Stop; re-install does not prune old hooks from existing
  `settings.json`. set `capture.realtime: true` to restore the buffer.
- **core PRs can auto-merge, on two mechanical bars.** the blanket "core
  is never armed" refusal is gone; both authorization surfaces (the
  `auto-merge` label and the `/auto-merge` comment) now route through one
  reusable `arm-auto-merge.yml`, which arms any klass only when the
  `diff coverage` check is green on that head sha *and* the PR carries a
  closing reference to an issue plind-junior opened. neither bar is
  recomputed in the write-token job — both are read as metadata, so no PR
  code executes there. the owner-only guard and deauthorize-on-push are
  unchanged, and folding the two arming paths into one file removes the
  drift that left `/auto-merge` with no coverage check at all.
- **CodeRabbit's verdict no longer gates anything.** the
  `coderabbit-approved` commit status, the 3-strike auto-close, and the
  daily stale-pr reaper are removed, along with the `coderabbit-gate` and
  `stale-check` pr_bot commands that computed them. the status had
  already been dropped from the `test` ruleset's required checks, so this
  removes the machinery that outlived it rather than lowering a live bar.
  CodeRabbit still reviews every non-draft pr and still files formal
  approve / request-changes reviews — they are advisory now. the merge
  path is ci + CODEOWNERS, with the owner's auto-merge label as the go
  signal.
- **the `trust-gate` workflow is removed.** it failed a pr when an author
  outside the OWNER association touched a core path — a bar that the
  rewritten `arm-auto-merge.yml` already enforces from the other side:
  nothing arms without the owner's own label, green `diff coverage`, and a
  closing reference to an owner-opened issue, and CODEOWNERS still holds
  the review requirement on core paths. the `trust` pr_bot command and its
  `is_trusted` helper go with it. core-path classification stays — it is
  what `arm-auto-merge.yml` reads. **remove `trust-gate` from the `test`
  ruleset's required checks**, or every pr will block on a check that no
  longer reports.
- **auto approval is the default** (`review.approver_role: trusted-agent`
  in the starter config): a fresh KB approves the capturing agent's
  proposals with no human step. nothing bypasses the gate — every write
  still flows through `proposals.approve()` with one audit event and the
  `auto_approved` stamp; the new `proposals.auto_approve_pending` drain
  (run from capture finalize) clears claims, pages, entities and
  relations, rejects duplicates, and still holds protected page kinds,
  dead-reference pages, id conflicts and delete proposals for a
  reviewer. remove `approver_role` from `config.yaml` to put writes back
  behind `vouch review`.

### Added
- **bench: four derivation categories** (#617): `passive-consolidation`,
  `multi-hop-relational`, `temporal-depth` and `aggregation`. Each asks for a
  fact stated in no single claim, so an expected-answer substring check is
  impossible; they are graded on a new `MemoryCase.required` — every
  supporting part must reach the pack inside the budget. The generators draw
  from a derived rng and their own pools, so the ten existing categories
  produce byte-identical datasets and reproduce their recorded per-category
  scores exactly; only the composite moves (0.58 → 0.64) because four rows
  joined the mean. Measured on stock config: consolidation, temporal-depth and
  aggregation at 1.00 (win condition W3 wanted > 0.5 against ditto's 0.00),
  `multi-hop-relational` at 0.17 — the new lever, where a three-link chain
  loses a link because no hop shares a term with the question.
- **shipped ranking champion** (`vouch.strategies.provenance`): the
  engine-lane winner (provenance-aware ranking — hearsay and stored
  instructions demoted, change-of-state phrasing boosted) now ships in
  the package. new KBs get it via the starter config
  (`retrieval.strategy`); existing KBs keep byte-identical ordering until
  they add the key, and `strategy: null` opts out. the competition
  champion `contrib/strategies/baseline.py` delegates to it, so
  challengers now have to beat real ranking, not identity order. with a
  strategy active, retrieval over-fetches a bounded candidate pool and
  the strategy's top-`limit` survive — de-prioritising below the window
  genuinely excludes a candidate from the pack.
- **session-mode answer memory** (`capture.answer_mode`, default `session`):
  claims are extracted once at SessionEnd from the full transcript history
  (`capture_session_answers`, wired into `capture finalize`) instead of on
  every Stop hook. per-turn extraction saw one answer at a time, which is
  where single-turn fragments like "… are noted at the end" came from; the
  session document gives the extractor every turn at once, collapses
  duplicate spans across turns, and spends one `max_claims` budget per
  session rather than per turn. `capture.answer_mode: turn` restores the
  legacy per-turn behaviour; the Stop hook stays wired either way (it defers
  with `deferred-to-session-end` in session mode).
- **an admission gate that filters knowledge-shaped garbage before it is
  filed** (`admission:` config). every ingestion path funnels through
  `proposals._file_proposal`, so a single provenance-keyed predicate there
  raises the floor with no drift across surfaces. it is deterministic and
  receipt-safe — it rejects verbatim payloads, never rewrites them, so
  byte-offset receipts stay intact. a claim that is a markdown heading, a
  colon lead-in, or a truncated code-span/bracket is refused, as is an
  uncited `type: session`/`log` page (a session diary, not durable
  knowledge). only the passive auto-capture actors (`vouch-capture`,
  `session-split`, `codex`) are blocked; a deliberate author's write stays
  advisory and reaches the review gate untouched. tunable via
  `admission.{enabled, min_confidence, reject_uncited_session_pages}`.
- **`vouch rejected`** — list rejected proposals (with `--admission` to show
  only gate auto-rejections). auto-rejections are recorded
  (`decided_by: vouch-admission`) and never deleted, so a false positive is
  always recoverable.

### Deprecated
- the auto-captured session-page pipeline is now a no-op for auto-capture
  actors: `capture.finalize`'s session summary, `session_split` renarrate,
  `codex_rollout` reingest, and the SessionStart review banner all produced
  uncited `type: session` pages, which the admission gate now auto-rejects.
  removal of the dead machinery is a follow-up.

### Changed
- **the per-prompt hook now lets the model decide how much of a turn
  vouch takes** (`retrieval.prompt_gate`). recall used to inject an
  unconditional "open with From vouch memory:" block on *every* prompt,
  so "fix the failing test" spent its reply opener announcing a memory
  search nobody asked for. rather than have the hook guess the prompt's
  intent with a verb list (which never covers the next phrasing), it now
  hands the host model ONE conditional instruction and lets the model —
  which already reads the prompt — choose per turn: a *question* the
  items answer opens with "From vouch memory:" and quotes them; a *task*
  (fix / build / change / run anything, known verb or not) uses them
  silently as background, citing an id inline only where relied on, with
  no banner; *irrelevant* items are ignored. on a miss the same judgment
  applies — "Nothing in vouch on this." for a question, silence for a
  task. chatter with no informative tokens ("ok thanks", "which one is
  better?") injects nothing at all (retrieval ORs every query token, so
  those matched noise on `one`). no per-turn model call and no latency —
  the decision rides in the instruction text the model already
  processes; it generalizes to any phrasing or language because it no
  longer depends on recognizing the verb. compliance measured on real
  `claude -p` across haiku / sonnet / opus (KB-backed block): coding
  tasks are never wrongly announced. new KBs get it on; existing KBs
  keep the unconditional block until they add the key.
- **personal catch-all KB + `vouch adopt`** (global vouch, phase 3):
  `vouch hub init-personal` creates and registers a personal KB at
  `~/.local/share/vouch/personal` (`XDG_DATA_HOME` honoured;
  `VOUCH_PERSONAL_KB` overrides). with its opt-in flag on
  (`personal.fallback_capture` in the KB's own config — one question at
  `install-mcp --global`, or `--personal-fallback`, or `vouch hub
  fallback on`), sessions in folders WITHOUT a project KB capture into
  it instead of nowhere: every captured source records the folder it
  came from (`metadata.origin_path`), the session-start banner announces
  the routing, and per-prompt recall in those folders reads the same KB
  back. strictly double-opt-in (registry role `personal` + the KB's own
  config flag) and fail-closed: no personal KB, no flag, a corrupt
  registry, or a guard refusal (discovery landing on a personal KB from
  below — the hijack shape) all mean capture stays off exactly as
  before. `vouch adopt`, run inside a project that now has its own KB,
  drains those captures home THROUGH the project's review gate: sources
  copy byte-identically (content-addressed ids are stable across KBs),
  each live personal claim is re-proposed against the copied source, its
  byte-offset receipt re-verifies mechanically, and the project's own
  review config decides durability — auto-approve on receipt where
  enabled, pending for a human otherwise; adoption never bypasses
  review. idempotent in both directions (a claim already durable *or*
  already queued in the project is skipped, so re-running never doubles
  the review queue); `--dry-run` previews against the project's real
  gate; `--from-path` adopts a moved project's captures; `--retire`
  archives only the personal copies that actually landed durable —
  retiring a merely-pending one would strand it if the proposal is
  later rejected or expires; session rollups are reported, not moved
  (an unreviewed summary is not knowledge yet, so it stays where it was
  filed); both KBs log a `kb.adopt` audit event carrying the other
  side's id. the personal KB is ONE store shared by every KB-less
  folder — recall there reads all of it, and the digest header, the
  per-prompt block, the session banner, `vouch status` and the opt-in
  question all say so rather than calling it "this repo's" knowledge.

### Fixed
- `sync_vault` catches the `ProposalError` that `propose_page` raises for a
  deleted citation (missing claim/entity/source id, with
  `ArtifactNotFoundError` as cause), not just the raw
  `ArtifactNotFoundError`. other proposal failures (empty title, page-kind
  validation) surface as a neutral `vault edit rejected` `VaultSyncError`
  rather than being mislabelled as an unknown artifact. the CLI's
  `except VaultSyncError` renderer then prints a one-line `Error:` instead
  of an uncaught traceback (#547).
- `clear` reads a naive `before` as utc instead of raising. a date-only
  cutoff — `2026-07-01`, the shape the cli help, the console's own error
  text, and the `kb.clear` docs all advertise — parses naive, and comparing
  it against a claim's aware `created_at` raised `TypeError`: a traceback
  from `vouch claims-clear --before`, an error response over mcp/jsonl, and
  an unhandled 500 on the review console's `/clear-claims`. normalised at
  the `lifecycle.clear_claims` chokepoint, so all four surfaces are fixed
  at once; the audit event records the normalised cutoff.
### Added
- **ingest selection knob (`vouch ingest --max-claims / --budget-chars`).**
  capture used to file every substantive sentence of a source — complete,
  but a restatement of the whole document rather than the facts worth a
  claim. `extract.select_spans` ranks candidate spans by information density
  (sum over distinct content words of `1 / document-frequency`, so rare
  specific terms outweigh stopword-heavy filler) and keeps the best under a
  claim-count or character budget. it is deterministic and llm-free — and it
  only ever returns a *subset* of the verbatim spans, never a paraphrase, so
  every kept claim's receipt still verifies by construction. unset, ingest
  keeps every span exactly as before (the unbudgeted baseline is unchanged).
  this is the selection step the compiler thesis needs: fewer, denser claims
  are what move accuracy-per-token against the grep baseline.

### Fixed
- **extraction no longer fractures dotted numbers.** `segment_source` split
  on every `.`, so a version or decimal (`6.8.3`, `3.14`) was broken across
  segment boundaries and its answer atom fell out of every span — measured at
  ~11% of the ground-truth facts lost on a synthetic lookup corpus *before any
  budget was applied*. a `.` flanked by digits is now kept inside the span
  (sentence-ending periods are unaffected), lifting the recall ceiling of the
  whole ingest pipeline from 89% to 100% of facts on that corpus.

## [1.5.0] — 2026-07-20

### Added
- **`vouch install-mcp claude-code --global`** — install once for the
  whole machine. writes user-level hooks, `/vouch-*` commands, and a
  fenced CLAUDE.md snippet under `~/.claude/`, and registers vouch as a
  *user-scope* MCP server (top-level `mcpServers` in `~/.claude.json`),
  so every claude session in every folder gets capture + per-prompt
  recall into that folder's **own** project `.vouch/` — the data stays
  per project; run `vouch init` once per project. a folder without a KB
  never captures anywhere: its session opens with a one-line "run
  `vouch init`" note (the session-start banner), and `vouch serve` now
  starts without a KB for the stdio transport (per-tool-call errors
  instead of a machine-wide failed server in every non-vouch folder).
  declared by a manifest `global:` block, so other hosts opt in as pure
  manifest work; the user-level CLAUDE.md snippet is machine-wide-worded.
  safe next to existing per-project installs, guarded three ways: the
  global settings template is byte-for-byte the project one (claude code
  collapses duplicate hook commands; a sync test freezes them), capture
  dedups on the event's `tool_use_id` (exact, window-free — catches
  drifted wiring too), and the hook commands (capture
  observe/answer/finalize/finalize-all/banner, context-hook, recall,
  ingest-codex) resolve the KB from the hook payload's `cwd` — with
  `VOUCH_PROJECT_DIR` keeping precedence, and a payload naming a
  nonexistent cwd refusing capture rather than falling back to the
  process cwd. a malformed existing settings.json now reports as
  *failed* (vouch is not wired) instead of "already present".
- **KB instance identity**: `vouch init` mints a durable id (uuid, stored
  in `config.yaml` under `kb:` next to a display name) and stamps it onto
  every new audit event and bundle manifest, so history and exported
  artifacts stay attributable to the KB that produced them once knowledge
  starts moving between KBs. existing KBs are backfilled on re-init or
  `vouch hub register` (an additive `kb.identity` audit event — never a
  history rewrite); pre-identity audit chains still verify. bundle
  imports move settings, never identity: the destination's `kb:` block
  survives even an overwrite-import, and same-settings configs no longer
  read as conflicts just because ids differ (config.yaml is compared
  structurally, modulo `kb:`, on both sides). compat note: a *pre-identity*
  vouch importing a new bundle uses the old byte-compare and may report
  config.yaml as a conflict — its default skip mode leaves the file
  untouched, so nothing breaks; upgrade the importer to converge.
- **machine registry** (`vouch hub register / list / unregister`): a
  machine-local list of known KBs at `~/.config/vouch/registry.yaml`
  (honours `XDG_CONFIG_HOME`; override with `VOUCH_REGISTRY_PATH`) with a
  role per KB — `project`, `personal`, or `team`. advisory routing state
  only: authority stays in each KB's own `.vouch/`, and a missing or
  corrupt registry degrades to per-project behaviour. this is the
  substrate for global (install-once) vouch and the local seed of the
  vouchhub registry of connected KBs.
- **scope stamped at write time**: every new claim and page proposal — and
  every captured session-answer source — records the KB's own project scope
  at the propose gate, so knowledge knows which project it belongs to
  before KBs ever start sharing artifacts (scope cannot be retrofitted
  later). the stamp and the read-side viewer resolve through ONE chain
  (`VOUCH_PROJECT` > `retrieval.scope` > the durable `kb.id`), so what a KB
  writes it can always read back — the mutable `kb.name` is never
  load-bearing for visibility, and a rename cannot orphan stamped
  knowledge. pages join claims and sources as scoped kinds, closing a
  cross-KB leak channel (vault edits carry the durable page's scope
  through, never a restamp; hand-edited legacy `scope:` frontmatter
  degrades to unscoped instead of breaking the page). malformed explicit
  scopes are refused at the gate, and a malformed scope already on disk
  degrades to unscoped instead of crashing the audit read path. the
  SessionStart digest (`vouch recall`) is viewer-filtered like every other
  retrieval surface — it used to inject every live claim regardless of
  scope — and reports on stderr how many artifacts scope filtering hid,
  never filtering silently; the salience sidebar honours the same filter.
  existing unscoped artifacts behave exactly as before. explicit `scope=`
  overrides are accepted by `propose_claim` / `propose_quoted_claim` /
  `propose_page`.
- **hijack-proof KB resolution**: the upward `.vouch` walk never ascends
  past `$HOME` any more, so a stray home-directory KB can no longer
  silently capture every project below it (a recorded incident class).
  starting *in* `$HOME` still resolves its KB; `VOUCH_KB_PATH` and a
  `global: {allow_home_kb: true}` opt-in in the home KB's config remain
  deliberate escape hatches. a registry entry with role `personal` adds a
  second belt: ambient capture into it from another directory is refused
  (reads warn). host adapters can now pin the walk's start with
  `VOUCH_PROJECT_DIR`; `vouch discover` and `vouch status` report the
  resolution chain (`why`) and the KB's id/name.
- the per-prompt recall hook (`vouch context-hook`) is now instructional
  and always visible: with relevant approved items it tells the model to
  open its reply with **"From vouch memory:"** and ground in the cited
  items; with no relevant items it says so explicitly ("Nothing in vouch
  on this.") instead of injecting nothing — so recall can never be
  silently mistaken for "vouch did nothing". an opt-in confidence
  short-circuit (`retrieval.short_circuit.{enabled,min_confidence}`)
  lets a high-confidence non-action lookup collapse to a verbatim
  vouched answer; "do work" prompts never short-circuit.
- `vouch install-mcp claude-code` now registers vouch as a **local-scope**
  MCP server in `~/.claude.json` (`projects[<abs project>].mcpServers`),
  the same thing `claude mcp add` does. a committed `.mcp.json` is a
  *project*-scope server that Claude Code loads only after a per-user
  approval — and the **VS Code extension never surfaces that approval
  prompt**, so `.mcp.json` alone left the `kb_*` tools stuck at "pending
  approval" in the extension while the hooks quietly ran (reads as
  connected, isn't). the local-scope entry is trusted on sight, so a fresh
  install now connects after a window reload with no manual step. verified
  end-to-end: fresh project → `install-mcp` → `claude mcp list` reports
  `✔ Connected` (was `⏸ Pending approval`). declared by a manifest
  `user_mcp:` block (host-neutral core; only claude-code opts in);
  idempotent and never clobbers a server you added yourself; opt out with
  `--no-approve`.

### Changed
- the starter config now ships `review.auto_approve_on_receipt: true`
  (and `require_human_approval: false`, an advisory key no code path
  reads): a fresh KB auto-approves captured claims whose byte-offset
  receipts verify against their source, so recall works out of the box
  with no `vouch review` pass. the gate is unchanged for everything
  the receipt check cannot vouch for — pages (session summaries
  included), entities, relations, and claims that cannot quote their
  source still wait for a human. existing KBs keep whatever their
  `.vouch/config.yaml` says; set `auto_approve_on_receipt: false` to
  restore the fully human gate.
- the receipt drain now runs at every session start (`capture
  finalize-all`), so verifiable claims left pending while the gate was
  off are approved instead of stranded, and it is duplicate-safe: a
  claim re-deriving text that is already durable is mechanically
  rejected ("duplicate: identical claim already durable") rather than
  crashing the drain or piling up in the review queue. a claim id held
  by *different* text is a real conflict and stays pending for a human.
  the same resolution now backs `capture answer`, which previously left
  re-captured duplicates pending forever.

## [1.4.0] — 2026-07-17

### Added
- `vouch install-mcp <host>` now bootstraps the KB when no `.vouch/` is
  discoverable at or above the target, making install a one-command
  setup. previously it exited 0 with "0 failed" in a fresh project while
  every installed hook silently no-oped forever (`|| true`) and the mcp
  server exited 2 — with nothing ever telling the user to run
  `vouch init`. opt out with `--no-init` (a loud stderr warning then
  names the remedy); an ancestor KB is reported ("Using existing KB
  at …"), never shadowed by a second one; unknown hosts still fail
  cleanly without planting a KB. staging-dir hosts opt out via a new
  `kb_bootstrap: false` manifest key (claude-desktop does — its target
  is a paste-ready staging dir, and a KB planted at an arbitrary cwd
  would ambiently capture every child project). the preflight ignores
  a shell-exported `VOUCH_KB_PATH` (it answers "what does this tree
  resolve to", and notes the override on stderr instead). `vouch init`
  and the auto-init share one code path so they cannot drift.
- cli mirrors for the last five `kb.*` methods that had none —
  `vouch propose-delete`, `vouch source list`, `vouch session list`,
  `vouch session transcript`, `vouch session summarize` — and the
  capabilities test now enforces cli parity alongside the existing
  jsonl + mcp checks, so a new method cannot land without its cli
  command (#475).
- `retrieval.recency`: a half-life decay (default 90 days, whole-day
  quantized) blended into fused context-pack scores so fresher knowledge
  outranks equally-relevant stale knowledge. rescoring-only — an old
  artifact loses at most half its score and never vanishes. enabled in
  the starter config for new kbs; existing kbs keep byte-identical
  ordering until they add the key (#476).
- a `retrieval` block on `kb.search` and `kb.context` responses reports
  the configured vs actually-used backend, whether semantic search is
  available, and a `degraded` flag — a base install serving lexical hits
  under a semantic-capable backend name now says so instead of labelling
  them "hybrid" (#476).
- ai auto-merge bot: the owner arms auto-merge on a pr with the `auto-merge`
  label or by commenting `/auto-merge`; a non-core pr then merges once ci is
  green and CodeRabbit approves. core paths always require owner review via
  `.github/CODEOWNERS`; ui prs opened without before/after screenshots are
  auto-closed. review is the gate, not just advice: CodeRabbit
  (`.coderabbit.yaml`, free for this public repo) submits a formal review and
  the `coderabbit-gate` workflow turns its verdict into the required
  `coderabbit-approved` status check — so a fresh push voids a prior approval,
  and a pr CodeRabbit requests changes on 3 times is auto-closed (owner and
  bots exempt). a daily `stale-pr-reaper` also closes a pr whose author left a
  CodeRabbit change request unaddressed (no new commit) for 2 days. no paid
  model in the loop. deterministic decision logic lives in
  `src/vouch/pr_bot.py` (pure stdlib). repo guards (branch ruleset + labels)
  are configured by `scripts/setup_repo_guards.sh`. workflow security is linted
  in ci with zizmor + actionlint.

### Changed
- `kb.search` is one implementation (`context.search_kb`) across mcp and
  jsonl instead of three drifting copies; `auto` now fuses embedding +
  fts5 via rrf with a substring fallback (it used to waterfall
  embedding-first), and an omitted backend defers to `retrieval.backend`
  in config.yaml (#476).

### Fixed
- secret masking now catches JSON/quoted-key credentials
  (`"password": "..."`, `'api_key': '...'`). the key's closing quote sat
  between the name and the `:` and broke the assignment regex, so the most
  common structured secret shape — and exactly the file family vouch writes
  (settings.json, quoted yaml) — slipped through `capture.observe` into the
  gitignored buffer that rolls into a committed session page and the
  append-only audit log. the value is masked and the key/quotes kept legible (#549).
- `propose-claim`, `propose-relation`, and `propose-entity` now validate
  the payload against the Claim/Relation/Entity model at propose time
  instead of only at approve. an out-of-range `--confidence` or an
  invalid entity/relation type used to file a proposal that could never
  pass `approve()`, sitting stuck in the pending queue with no clear way
  to fix it; it is now rejected immediately with the same error message
  approve would have raised.
- `pr_bot` core-path classification: `trust-gate.yml`, `auto-merge.yml`,
  and `comment-command.yml` now source the changed-file list from the REST
  `pulls/{n}/files` endpoint instead of `gh pr view --json files`, and feed
  both the new and previous filename into classification. the GraphQL-backed
  shortcut carries no rename metadata, so a `git mv` of a `CORE_GLOBS` path
  (e.g. `http_server.py`) made the file invisible to the trust gate and
  auto-merge arm check. (#505)
- `vouch lint` no longer flags retired claims as stale. a
  superseded/archived/redacted claim past the freshness window was
  reported as a `stale_claim` warning even though it is terminal and not
  expected to be refreshed — non-actionable noise in the sweep documented
  as the user-actionable subset. lint now exempts retired statuses,
  matching the exemption `vouch metrics` and `vouch digest` already made
  (#478, #484).
- approve/reject/expire record the audit event *before* moving the
  proposal to decided/. a crash between the two used to leave a durable
  decision with no authoritative history; it now leaves a pending
  proposal with its decision event, which the approve retry path already
  guards (#475).
- demo image build: `hatch_build.py` (the build hook pyproject.toml
  declares for console bundling) is copied into the docker build context;
  the image had been unbuildable since the hook landed (#474).

## [1.3.0] — 2026-07-14

### Added
- `vouch console`: serve the vendored React web console straight from the
  installed package — a same-origin `/proxy` bridge (loopback-guarded) to
  `vouch serve --transport http` backends, reimplementing the vite dev-proxy
  in python. the built SPA is bundled into the wheel as `vouch/web/console`
  (conditionally, via a hatch build hook), so `pip install 'vouch-kb[web]'`
  then `vouch console` needs no node and no repo clone.
- `kb.activity` read method (+ `vouch activity` CLI mirror): audit-log
  activity buckets for dashboards — per-day counts with proposal/decision
  breakdowns, an hour-of-week matrix, and actor/event histograms. windowed
  in viewer-local calendar days (IANA `tz` or a fixed utc offset), scope-
  filtered like `kb.audit`.
- console Dashboard view: 12-month activity calendar, last-30-days bars,
  hour-of-week heatmap, top actors and event mix, driven by `kb.activity`.
- `kb.synthesize` llm backend: `llm=true` drafts the answer with the
  deployment-configured `compile.llm_cmd`, grounded in retrieved kb pages
  and approved claims. code still verifies every `[id]` citation against
  the offered sources — invented ids are stripped, and a draft left with no
  verifiable citation returns an empty answer rather than a guess. the wire
  shape is unchanged plus additive `pages` and `_meta.synthesis_backend`
  fields. cli mirror: `vouch synthesize --llm`; the jsonl/http surface
  already forwarded the flag.
- console Chat: llm answers activated — the chat asks `kb.synthesize` with
  `llm: true` and falls back to deterministic claim synthesis when no
  `compile.llm_cmd` is configured. page citations open the page drawer, and
  llm answers carry an `llm` badge next to the confidence grade.
- session transcript viewer: the console Review view opens the full
  transcript of a captured session — thinking, per-tool call rendering
  with diffs, code blocks, and subagent drill-down — via the new
  `kb.session_transcript` read method. it locates the raw claude code
  jsonl session file or codex rollout by id, parses it into normalized
  blocks, and degrades to the observation buffer when the raw file is
  gone.
- review-gated artifact delete: `kb.propose_delete` files a delete
  proposal for a claim, page, entity, or relation. execution happens only
  through `proposals.approve()`, which checks the referenced-by matrix,
  removes the file, deindexes it, and records exactly what was removed in
  the decided proposal and audit event.
- `kb.clear_claims` / `vouch claims-clear`: bulk-remove auto-approved
  claims (`--auto-only`, `--before`, `--dry-run`, and a confirm gate) for
  cleaning up capture noise in one pass (#433).
- session-split summaries: large captured sessions are split into topical
  summary pages by a deployment-configured llm (`capture.split` in
  `config.yaml`; the split `llm_cmd` falls back to `compile.llm_cmd`),
  each filed as its own pending summary. `kb.summarize_session` runs the
  pass on demand, `kb.list_sessions` lists captured sessions for the
  review pipeline, and a filed mechanical summary can be llm-narrated in
  place.
- codex adapter: `vouch install-mcp codex` now wires the full tier — a
  `toml_merge` install strategy for `.codex/config.toml`, an agents.md
  fenced snippet, skills mirroring the vouch slash commands, and hook
  wiring for automatic session capture. codex session rollouts are
  ingested into review-gated summaries.
- codex: wired `UserPromptSubmit` to `vouch context-hook` for the first
  time, reusing the existing command unmodified — codex's hook
  payload/response shape matches claude-code's exactly. (#425)
- `kb.list_skills` / `kb.get_skill` — agents can enumerate the Claude Code
  slash-command and `SKILL.md` catalogue visible at `<kb_root>/.claude/` and
  `~/.claude/` over MCP, then fetch the full body of one by name (project-local
  entries override user-global on collision). Exposed across MCP (`kb_list_skills`
  / `kb_get_skill`), JSONL, and the CLI (`vouch list-skills` / `vouch get-skill`).
- `mcp.publish_skills` config flag (default `true`) — gates the skill catalogue
  for "company-brain" deployments where the catalogue itself is sensitive. When
  `false`, `kb.list_skills` returns an empty list and `kb.get_skill` errors with
  `permission_denied`; the flag is read fresh on every call so flipping it hides
  the catalogue without restarting the server, and is surfaced on
  `kb.capabilities.mcp.publish_skills` so clients can detect the gate. An
  existing KB with no `mcp:` block stays default-on (#235).
- mcp serves a **minimal tool profile by default** (8 core tools) instead
  of the full method surface; widen with `VOUCH_TOOL_PROFILE=standard|full`
  or `mcp.tool_profile` in `config.yaml`. approve/reject and maintenance
  tools live in `standard`/`full` — they stay human/cli actions, not agent
  defaults. the jsonl and cli surfaces are unaffected.
- per-prompt auto-recall: the claude-code adapter's `UserPromptSubmit`
  hook (`vouch context-hook`) injects relevant kb context on every
  prompt, so recall no longer depends on the agent remembering to ask.
- `kb.experts` — rank the entities carrying the most matched evidence on
  a free-text topic (count, recency, and citation weightings). read-only;
  answers "who/what does this kb actually know about X" (#315).
- `kb.triage_pending` — advisory triage scoring over the pending-review
  queue. scores each pending proposal on fit, citation quality,
  duplication risk, and contradiction risk, then attaches a
  `_meta.vouch_triage` block to help a reviewer prioritize a long
  `kb.list_pending`. read-only and advisory only: it never approves,
  rejects, or moves a proposal — a human still decides. degrades to a
  `difflib` heuristic without the `[embeddings]` extra. opt-in via
  `triage.enabled: true`; `vouch triage` mirrors it on the cli (#322).
- opt-in cross-encoder rerank of the context pack: enable with
  `retrieval.rerank.enabled: true` in `config.yaml`; `retrieval.rerank.top_k`
  bounds the rerank window. off by default (#436).
- `kb.diff` is registered at all four `kb.*` surface sites (mcp tool,
  jsonl handler, capabilities, cli) instead of cli-only (#327).
- dual-solve web ui: file-changes tree view in the candidate panes — a compact
  folders-first file tree drives a per-file diff pane, replacing the flat
  changed-files list and the stacked all-files diff. selection is
  per-candidate, so inspecting claude's diff never moves the codex pane.
  (#294)
- ``_meta.vouch_hot_memory`` on every primary read-side ``kb.*`` response
  (``kb.search``, ``kb.context``, ``kb.read_*``, ``kb.list_*``): a TTL-cached
  sidebar of recently approved claims, query-biased where the tool has a
  natural anchor (entity name/aliases, page title/tags, claim text, search
  query). ``kb.list_pending`` uses recency only. Meta-tools, write paths, and
  lifecycle ops are excluded by design (#225).
- demo: dual-path llm configuration — compile & summarize run through
  session-capture replay or directly against the api via a stdlib shim
  wired as `compile.llm_cmd` with a byo `ANTHROPIC_API_KEY`.
- `vouch contradict-scan` — an offline scanner that groups approved claims
  by shared entity and heuristically flags same-topic pairs that disagree
  in polarity. `--dry-run` (default) only prints candidates; without it,
  each surviving pair files a pending `contradicts` relation proposal via
  `proposals.propose_relation` for a human `vouch approve` — the scanner
  itself never writes a `Relation` or a `CONTESTED` status. `--threshold`,
  `--entity`, and `--limit` tune the scan. scoring lives in the new
  `src/vouch/contradictions.py`. (#314)

### Changed
- ``kb.list_*`` JSONL/MCP responses now use a dict envelope
  ``{"items": [...], "_meta": {...}}`` instead of a bare list. A one-release
  deprecation note lives at ``_meta.deprecation``; read ``result.items`` instead
  of treating ``result`` as the list. When the KB has recent approved claims,
  ``_meta.vouch_hot_memory`` carries the same recency sidebar as other read
  tools (#225).
- ``kb.capabilities`` advertises the hot-memory contract under ``hot_memory``
  (sidebar key, list-envelope flag, covered method list).
- retrieval `auto`/`hybrid` now **fuses embedding + fts5** results via
  reciprocal rank fusion instead of a waterfall (embedding-first,
  fts5-fallback), with near-duplicate suppression over the fused list —
  the highest-scored near-duplicate wins, caller order is preserved for
  the context pack.
- mcp serves one-line tool descriptions under non-full profiles, keeping
  the default surface cheap in agent context windows.

### Fixed
- audit: `log_event` serialises appends with a file lock, so concurrent
  writers cannot fork the hash chain (#263).
- `lifecycle.contradict()` no longer lets a claim contradict itself. calling
  it with the same claim id on both sides previously wrote a self-loop
  `contradicts` reference and flipped the claim to `contested` with no
  actual counterparty; it now raises `LifecycleError`, mirroring the
  existing guard in `supersede()`.
- rpc internal errors no longer leak tracebacks over the wire; they log
  server-side and return a clean error envelope.
- models reject empty `text`/`name`/`title` on claim, entity, and page at
  validation time instead of filing empty artifacts.
- `kb.crystallize` retries are idempotent for summary pages — a re-run
  after a partial failure no longer files a duplicate page proposal.
- `session.crystallize`: retrying on a session that hasn't been ended rewrote
  the `session-<id>` summary page with a fresh wall-clock `Ended:` stamp each
  time (and needlessly re-embedded it), so the "idempotent retry" wasn't. an
  open session now renders a stable marker, so retries produce an identical
  body.
- claude-code: the `UserPromptSubmit` context hook computed retrieval but
  never fed the entity-salience reflex (#223) — `salience.record_query`
  was never called from the hook path, leaving the reflex permanently
  dormant for every claude-code session. OpenClaw's context engine already
  called it correctly; cursor's `beforeSubmitPrompt` hook cannot accept
  injected context at all, so it is not wired. (#425)
- `vouch search` tolerates fts index errors instead of crashing — a
  broken or stale fts table degrades to the substring path (#438).
- `list_pages` skips corrupt page files instead of failing the whole
  listing, so one bad yaml no longer takes down every kb-wide caller
  (#360).
- context: the `require_citations` gate is computed after the `max_chars`
  budget is applied, so a claim trimmed out by the budget can no longer
  satisfy (or fail) the citation requirement on the pack (#268).
- `vouch digest --limit` now caps the followups-due section like the
  pending, decisions, and stale sections — it previously returned every
  due followup regardless of the limit, contradicting the `--limit` help.
- `kb.capabilities.host_compat` always reported `{}`: `_load_host_compat`
  (#237) read `openclaw.compat` from `openclaw.plugin.json`, but that
  manifest may not carry a top-level `openclaw` key at all (enforced by
  `test_manifest_carries_no_dead_dialect_fields`) — `openclaw.compat.pluginApi`
  has only ever lived in `package.json`. Repointed `_load_host_compat` (now
  reading `_PACKAGE_JSON_PATH`) at `package.json`, where the value has been
  present all along.
- the dual-solve diff renderer dropped added/removed lines whose content
  starts with `++`/`--` (e.g. an added `++counter` line) by treating them as
  `+++`/`---` file headers; the header skip now requires the trailing
  space-and-path form. (#294)
- `compile_kb()` could file two page proposals for the same title when the
  LLM's batch drafted the same topic (or same slug, e.g. "Retry Policy" vs
  "retry policy") twice — `taken_names` was only seeded from on-disk pages
  and pending proposals, never updated as drafts were accepted within the
  batch. Approving the second proposal would silently route through
  `update_page()` and overwrite the first. (#439)
- volunteer scoring treats hybrid relevance as rank-relative instead of
  assuming pre-normalized scores.
- `kb.capabilities` reads `openclaw.compat` from `package.json` instead
  of a stale manifest field (#417).
- the context hook never raises on a non-dict payload or a missing kb —
  a broken hook environment degrades to no injection instead of failing
  the host prompt.
- `context` and `lifecycle` catch only the exceptions they can handle
  (sqlite errors on fts5 fallback, missing-artifact on citation lookup)
  instead of blanket `except Exception`.
- `vouch install-mcp <host>` (codex `toml_merge`): a `config.toml` the minimal
  serializer couldn't faithfully re-emit (a non-BMP string value, a `nan`/`inf`
  float) was bucketed as `skipped` and printed as `(already present)` with a
  clean `Done`, so the user believed vouch was wired into codex when it wasn't.
  serializer-failure now lands in a distinct `failed` bucket, is reported as
  such, and the command exits non-zero — "already installed" and "install
  failed" no longer look the same.
- `vouch install-mcp <host>`: a manifest `dst` that escaped the target tree
  (via `..` or an absolute path) is now refused with an `AdapterError` instead
  of writing outside `target` (defense in depth for the manifest file writer;
  shipped adapters are unaffected).
- dual-solve review-ui: the recommendation hint rendered "neither engine
  produced a usable diff" for the entire duration of a still-running job — the
  hint was computed over the not-yet-populated candidate list on every poll.
  it is now omitted until candidates exist.
- `vouch capture ingest-codex`: rollout parsing had no size cap and could read
  an oversized (or newline-free blob) rollout whole into memory. the file is
  now bounded to 64 MiB up front, mirroring the byte caps on other untrusted
  reads.

### Security
- `kb.register_source_from_path` blocks path traversal: the path is
  resolved (following symlinks) and must land inside the kb root before
  reading, with `O_NOFOLLOW` + `fstat` closing the toctou window between
  the containment check and the read. previously any file the process
  could access was registrable as a "source" and retrievable via
  `kb.cite` / `kb.list_sources` (#421).

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
- `build_context_pack` now evaluates the `require_citations` gate (and
  `quality.uncited_items`) after the `max_chars` budget drops tail items, so
  the pack is never failed for uncited claims the caller did not receive.
  Fixes #174.
- `audit.log_event` now holds an exclusive cross-process lock around read-prev-hash → derive → append, closing a TOCTOU race where two concurrent writers observed the same `prev_hash` and forked the chain — `verify_chain` then reported "previous hash mismatch" at the second concurrent event forever, breaking the tamper-evidence guarantee from #244 under ordinary multi-writer usage (`vouch serve` + concurrent CLI, multiple agents on JSONL, scripted backgrounded approvals). Uses `fcntl.flock` on POSIX and `msvcrt.locking` on Windows against a sibling `audit.log.jsonl.lock` file so the audit log itself is never opened in a mode that could truncate it. Fixes #262.
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
