# company brain for vouch — design

date: 2026-07-04
status: draft, awaiting review
reference: [agno-agi/scout](https://github.com/agno-agi/scout) ("company intelligence agent")

## 1. what scout is, and the one-line verdict

scout is a single Agno agent with per-source **context providers** — each source
(web, slack, gdrive, workspace, CRM, wiki, arbitrary MCP servers) is exposed to
the main agent as two natural-language tools (`query_<source>` /
`update_<source>`), with an LLM sub-agent behind each tool that owns the
source's quirks. it favors **navigation over search** (no vector-ingest
pipeline; sub-agents `ls`/`grep`/read/paginate), and it accumulates its own
memory as a markdown **wiki** plus a Postgres **CRM** with schema-on-demand
agentic SQL. interfaces: AgentOS web UI + a Slack bot. proactivity: planned
scheduled tasks (morning followup digests). quality: three eval tiers (wiring /
behavioral / LLM judges).

**verdict from the gap analysis: scout and vouch are duals, not competitors.**
scout is an *agent* with ungated stores whose trust model is "trust the writing
agent, audit post-hoc via git" (its own code says wiki writes "auto-pass").
vouch is a *substrate* whose trust model is "distrust the writer, review
pre-write, hash-chained audit is authoritative." every scout idea ports to
vouch by one uniform transformation:

- any write terminates at `proposals.propose_*`, never at the store
  (the in-tree precedent trio: `capture.py`, `vault_sync.py`, `dual_solve.py`)
- anything recurring is a one-shot CLI in operator cron, never a resident daemon
- anything with an LLM in it lives host-side (adapters/), never in `src/vouch`
- live-source access: **the host agent is the connector, vouch is the evidence
  locker** — content enters as content-addressed source snapshots (intake is
  deliberately ungated), knowledge enters only through the gate

"navigation over search" needs no porting at all: the plaintext KB is literally
greppable and state.db is an explicitly derived, rebuildable cache.

## 2. what vouch already has that scout lacks

worth stating because it means the direction is "add company-brain shapes to
vouch," not "catch up to scout": a review gate on every write; mandatory
evidence citations enforced at the pydantic model level; a hash-chained,
tamper-evident audit log (`verify_chain`); claim lifecycle
(supersede/contradict/confirm/expire); trust stamping on every result; FTS5 +
optional embeddings over a derived index; salience + context packs;
multi-host adapters (claude-code, cursor, codex, openclaw context engine).

## 3. gap analysis (scout capability → vouch status)

| capability | status | port |
|---|---|---|
| typed structured records (CRM: contacts, projects, followups) | **missing convention, machinery exists** | page-kind preset via existing `page_kinds.py` `PageKindSpec` + `Entity`/`Relation` types |
| schema-on-demand DDL | conflicts → reshape | a reviewed `config.yaml` `page_kinds:` diff (yaml+pydantic, no DSL) |
| structured queries over records | **missing** | frontmatter indexing in derived state.db; `kind=`/`meta=` filters on `kb.list_pages` ([server.py:287](../../../src/vouch/server.py) is filterless today) |
| followups closed loop (due_at + status + morning surface) | **missing** | `followup` page kind + digest; status flips via `propose_page` with `slug_hint` (the `vault_sync.py` gated-edit precedent — not a direct lifecycle op) |
| scheduled tasks / proactivity | **missing** | `vouch digest` one-shot for operator cron — backlog draft **#20** |
| external intake (web/URL/dropped files) | **missing** | `vouch source add --url` snapshot + `vouch inbox scan` propose-only importer — backlog drafts **#15/#16/#17** |
| reviewer nudges | **missing** | notify webhooks off audit events — backlog draft **#21** |
| NL ask/remember UX (`query_*`/`update_*`) | partial | host-side slash-commands/skills in adapters/ that translate NL → `kb.search/context/synthesize` and `kb.propose_*` |
| slack presence | missing → reshape | v1: digest + webhooks posted via the host agent's own slack tools; full chat bridge is an optional separate repo, later |
| sub-agent-per-source | missing → reshape | lives in the calling host (claude code subagents / openclaw skills), not in vouch |
| voice/style guide (write=False wiki) | partial | `protected: true` page kinds enforced in the approval-block check — gate-*tightening* |
| behavioral evals for prompt flows | **missing** | injected-fixture tests for the adapter pack |
| ungated wiki/SQL writes, hosted control plane, Postgres store, resident scheduler | **conflicts — do not port** | these violate the review gate, local-first, plaintext, and no-daemon invariants respectively |

## 4. the design: "brain pack" — typed conventions + viewports on the review gate

vouch does not need to become an agent to be a company brain. it needs
company-brain **shapes** expressible in machinery it already has, plus
**viewports** that make a human-paced review gate feel alive. three
architectures were generated and judged (see §7); this one won both judge
lenses (maintainer acceptance; value per unit effort), largely because most
milestones are "implement the maintainer's own drafted backlog item," not
"argue for a new architecture."

### components

1. **brain-pack schema conventions** (config only, zero core code).
   a shipped `page_kinds:` stanza — `contact`, `org`, `project-record`,
   `meeting-notes`, `followup` (frontmatter: `due_at`, `status`, `owner`),
   `decision-record`, `voice` (`required_citations: true`) — consumed by
   `vouch init --preset company-brain` (a general `--preset` mechanism, not a
   one-off flag). a CRM "row" = `Entity` (PERSON/COMPANY/PROJECT exist in
   `models.py`) + typed page + `Relation` edges, all filed via existing
   `propose_entity/propose_page/propose_relation`. validation already runs at
   both propose and approve.

2. **adapter brain skills** (host-side NL layer; vouch stays zero-LLM).
   `/vouch-ask` (search → context → synthesize, always cited; refuses uncited),
   `/vouch-remember` (register MESSAGE source → propose claim citing it —
   scout's `update_knowledge` with the gate kept), `/vouch-record`
   (entity + typed page pair), `/vouch-followup`, `/vouch-standup` (narrate
   `vouch digest`). shipped in `adapters/claude-code/.claude/commands/`,
   mirrored to `adapters/openclaw/skills/` + `openclaw.plugin.json` skills
   list (sync tests enforce parity), installed via `install_adapter.py`.

3. **frontmatter-aware structured queries in the derived index.**
   `page_meta(page_id, key, value_text, value_num)` table in
   `index_db.py` SCHEMA, populated at approve time and by `kb.index_rebuild`.
   extend `kb.list_pages` with `kind=` and `meta=` params (equality +
   `due_before/due_after` date-range **only**, validated against the kind's
   declared `frontmatter_schema` — explicitly not a query language). parameter
   extension on an existing method avoids a new kb.* method; if a dedicated
   method is preferred it must hit all four registration sites.

4. **`vouch digest`** (backlog #20 + a followups-due section).
   new `src/vouch/digest.py` composing `metrics.compute`,
   `store.list_proposals(PENDING)`, `audit.read_events`, and the new
   frontmatter query (`kind=followup, status=pending, due_at <= today`).
   read-only by construction, `--format text|json|markdown`, run from operator
   cron. kind-aware exclusion added to `recall.py`'s digest at the same time
   (its 12k-char guard will otherwise truncate once hundreds of
   contact/followup pages exist).

5. **snapshot-to-source intake, propose-only** (backlog #15/#16/#17 reshaped).
   (a) `vouch source add --url` actually fetches and snapshots bytes into the
   content-addressed store (`SourceType.URL`) — with the **untrusted-content
   envelope**: per-snapshot byte cap, domain allowlist, redirect/private-range
   (SSRF) blocking, explicit utf-8 decode (known latin-1 locale hazard),
   `fetched_at` + locator + fetch params in `Source.metadata`, content run
   through `sanitize_for_prompt`, never indexed while unreviewed.
   (b) `vouch inbox scan <dir>` one-shot importer: registers dropped files via
   the hardened `register_source_from_path` and files stub proposals — the
   exact `capture.py` pattern. tests assert intake never calls `approve()`
   (a structural import-guard test: fetch modules never import
   `proposals.py`/`lifecycle.py`; proposal-filing modules never call approve).
   slack/gdrive/web **live connectors are explicitly not built** — the host
   agent already owns those MCP tools and exports into the inbox or registers
   sources directly.

6. **notify webhooks** (backlog #21 as written): `src/vouch/notify.py` +
   `vouch notify sweep|test`, config-declared `notify.webhooks` with env-ref
   secrets, fired off proposal-created audit events plus a cron-driven aged
   backlog sweep. read-and-notify only. combined with digest, this is the
   "slack-adjacent presence" v1: the host posts digest/notification output to
   chat with its own tools.

7. **protected page kinds** (gate-tightening): `protected: true` on a
   `PageKindSpec`, enforced inside the approval-block check in `proposals.py`
   (alongside the existing self-approval block), so `voice`/`decision-record`
   cannot be approved under `review.approver_role: trusted-agent`. the
   structural version of scout's write=False voice wiki.

8. **behavioral eval tier for the adapter pack**: injected-fixture tests that
   an agent following the pack actually registers-then-proposes, never
   attempts approve, refuses uncited answers, and does not act on hostile
   instructions embedded in registered sources. live-credential tests skip
   (not fail) without credentials, mirroring
   `tests/test_openclaw_plugin_load_real.py`.

9. **staleness surfacing**: a lint/doctor check flagging claims whose cited
   snapshot sources have old `fetched_at`, so reviewers don't approve against
   stale evidence.

### milestones (each independently shippable)

- **M1 — config + prompts, zero core code (ships immediately):** page-kinds
  preset + `vouch init --preset company-brain` + adapter brain skills
  (`/vouch-ask`, `/vouch-remember`, `/vouch-record`, `/vouch-followup`) +
  `docs/company-brain.md`. usable today with existing propose/approve;
  validates the record model with real users before any core code lands.
- **M2 — structured queries:** `page_meta` derived index + `kind`/`meta`
  filters on `kb.list_pages` (all surfaces) + kind-aware recall exclusion +
  `tests/test_page_meta_index.py`.
- **M3 — digest:** `src/vouch/digest.py` + `vouch digest` (+ optional
  `kb.digest` through the four sites) + crontab recipe + `/vouch-standup`
  skill + `tests/test_digest.py`.
- **M4 — intake:** `vouch source add --url` (untrusted envelope, SSRF/byte
  guards) + `vouch inbox scan` propose-only importer + never-approves and
  import-guard tests.
- **M5 — hardening:** notify webhooks + protected page kinds + staleness
  lint.
- **M6 — evals + identity:** behavioral eval tier for the adapter pack;
  per-user bearer-token→actor mapping via `trust.py` as a hard precondition
  before any approve action ever ships outside review-ui/CLI.
- **optional, later, separate repo:** a self-hosted chat bridge (thread↔
  `kb.session_*` mapping, messages registered as MESSAGE sources, cited
  answers, review-ui deep links) over `vouch serve --transport http`. only if
  real conversational slack presence proves wanted; not part of this design's
  commitment.

### rules that keep it invariant-safe

- nothing new can reach `proposals.approve()`; the one gate change (protected
  kinds) tightens it.
- `page_meta` stays rebuildable-only via `kb.index_rebuild`; a PR that makes
  an index-only field authoritative gets rejected.
- `meta=` filters stay equality + date-range on declared frontmatter fields;
  anything richer belongs in the caller.
- unreviewed/pending items may be *presented* explicitly labeled UNREVIEWED
  (host-side), but are never mixed into `kb.search`/`kb.context` retrieval.
- no resident scheduler, webhook listener, or LLM client inside `src/vouch`.

## 5. risks

- **review-queue throughput is the real product bottleneck.** a company brain
  multiplies small writes; if the human gate backs up, the brain feels
  amnesiac ("remember X" is invisible to retrieval until approved). digest +
  notify + batch review (`vouch approve a b c`, crystallize, review-ui)
  mitigate; docs must state expected reviews/day honestly. this is the core
  tension of a gated brain and it is a *feature* being traded on purpose.
- frontmatter querying is a slippery slope toward the forbidden SQL-CRM shape
  — held by the derived-only and no-DSL rules above.
- followup status flips via full page-edit proposals may feel heavy daily; the
  tempting fallback (direct lifecycle-style op) opens a gate bypass and should
  be rejected — `slug_hint` gated edits are the path.
- `source add --url` is vouch's first outbound HTTP in the intake path: SSRF,
  size bombs, secret-bearing URLs in the committed store, and committed
  `.vouch/sources/` growth (needs per-snapshot caps + retention guidance).
- each new skill is a three-file change (claude-code command, openclaw skill,
  plugin manifest); the sync tests catch drift but the loader quarantines
  silently on manifest mismatch.
- NL-layer quality is host-model-dependent and untested until M6's eval tier.
- multi-user: one shared team brain is fine; per-person isolation is a real
  limit and deliberately out of scope.

## 6. explicitly not doing

- slack/gdrive/web **connectors inside `src/vouch`** (role inversion: vouch is
  substrate, hosts are the connectors; duplicates host capabilities; credential
  + API-drift surface in an offline-complete core).
- a resident scheduler/daemon, a hosted control plane, ungated "trusted agent"
  writes, Postgres/SQL as an authoritative store — all conflict with CLAUDE.md
  invariants.
- a full chat bridge as a commitment (kept as an optional later sidecar).

## 7. alternatives considered

three designs were generated independently and ranked by two judges
(maintainer-acceptance lens; value-per-effort lens). both ranked identically:

1. **brain pack** (this design) — wins: maps ~1:1 onto drafted backlog items
   #15–17/#20/#21/#26, minimal kb.* surface growth, one repo/CI, every
   milestone independently useful.
2. **steward** — vouch untouched + separate agent sidecar/bridge repo. nearly
   identical on the vouch-side PRs; loses because its distinctive value (real
   chat presence) is the largest, riskiest work item and lives in a second
   repo the maintainer never merges. its best ideas (per-user actor mapping,
   behavioral evals, review-latency honesty, recall exclusion, `--preset`
   spelling) are grafted in above.
3. **providers-in-vouch** — deterministic connector layer + `kb.query_source`/
   `kb.snapshot_source` in core. invariant-safe write story, but inverts
   vouch's role, duplicates host capabilities, and its own risk list names
   maintainer pushback; likely rejected at PR. its best ideas (untrusted
   envelope, import-guard test, snapshot hygiene, staleness lint) are grafted
   in above.
