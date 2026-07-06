# distill reviewed decision claims from captured sessions

- status: draft, awaiting review
- date: 2026-07-04
- scope: one implementation plan
- depends on: 2026-07-01-vouch-session-autocapture-design.md (shipped as
  `src/vouch/capture.py`)

## goal

auto-capture (capture.py) reliably records *what a session did* — prompts,
files, commands, diff — but nothing turns that record into *what the session
decided*. the review queue fills with activity reports a human must distill
by hand, and in practice doesn't (33 pending summaries at time of writing).

this design adds the missing semantic step: **decision claims drafted from
the captured record, filed as pending proposals, never self-approved.** it
adopts the useful half of akbp's session-crystallize idea (an llm interprets
what was decided) while fixing the three places akbp's version leaks:

| akbp leak | fix here |
|---|---|
| end-of-session ritual often never runs | harness-fired `Stop` hook nudge + review-time drafting backstop |
| self-report has no ground truth to check against | every drafted claim must cite the captured session record (content-hashed source) |
| the same agent approves its own write | drafts land `PENDING` from a distinct actor; only `proposals.approve()` moves them |

## north-star fit

the load-bearing invariant stays intact: distill only ever *proposes*. no
code path in this design calls `approve()`. the llm moves to the reviewer's
side of the gate — it drafts candidates from committed evidence for a human
to judge, instead of writing memory from an agent's unverifiable
self-report. capture.py stays zero-llm and unchanged in trust posture.

## design overview

three components, independently useful, shipped in this order:

```
capture finalize ──▶ summary body gains "## decision candidates"     (c1: regex quote, no llm)
                      (verbatim prompt lines that look decision-shaped)

Stop hook ─────────▶ vouch capture nudge ──▶ {"decision":"block",     (c2: hot-context self-report)
  (per turn)          reads buffer, gates     "reason": "propose kb
                      on new activity         claims for decisions
                                              made this turn, cite
                                              sources"}

vouch review ──────▶ approve session page ──▶ offer distill ──▶       (c3: cold-record drafting)
  (human present)     page becomes source     llm drafts claims
                      (content-hashed)        citing the page
                                              ──▶ propose_claim(PENDING)
```

c1 is mechanical and helps even with no llm configured. c2 catches decisions
while the model's context is hot. c3 is the backstop that works on any
captured session, including ones that died mid-task — and it is the only
component that runs an llm, always on the human's side of the gate.

## components

### c1. decision candidates in the summary (capture.py, mechanical)

at `finalize`, scan the buffered **user prompts** (already stored verbatim)
for decision-shaped lines and quote them under a `## decision candidates`
section in the summary body. patterns (case-insensitive, tuned in one
module-level tuple): `use X instead of`, `decided`, `let's use`, `switch
to`, `always`, `never`, `don't ... anymore`, `from now on`, `prefer`.

- pure quoting of the user's own words — no interpretation, no llm, so it
  does not violate capture's zero-llm rule.
- user prompts are the highest-signal channel: in practice the human states
  the decision ("use yaml, not json") more often than the model does.
- serves both readers: the human reviewer sees candidate decisions at the
  top of the page; c3's drafting prompt gets them as anchors.
- cap at 10 lines; sessions with zero matches simply omit the section.

### c2. hot-context nudge (`Stop` hook + `vouch capture nudge`)

a new claude code `Stop` hook calls `vouch capture nudge`. the command:

- reads the hook payload from stdin. if `stop_hook_active` is true, exit 0
  immediately (never loop — claude code sets this flag when the turn is
  already a continuation from a stop-hook block).
- reads the session buffer; counts observations since the last nudge
  (watermark stored as a `nudged_at_count` line in the buffer itself, so no
  new state file).
- if new substantive observations `< distill.nudge_min_observations`
  (default 8) or an edit/write isn't among them, exit 0 (allow stop).
- otherwise emit `{"decision": "block", "reason": "..."}` where the reason
  instructs: *if this turn produced a durable decision (tool choice,
  approach ruled out, constraint discovered), file it now with
  `kb.propose_claim`, citing the files or sources that evidence it; if
  nothing durable was decided, just finish.* then write the watermark.
- at most `distill.nudge_max_per_session` nudges per session (default 2) —
  the nudge is a prompt for the existing voluntary `kb.propose_claim` flow,
  harness-fired so it cannot be forgotten, but it must not become nagging.
- same startup-cost constraint as `observe`: minimal import fast path; the
  hook runs at every turn end.

claims proposed this way go through the normal mcp propose path — pending,
cited, attributed to the session — nothing new server-side.

### c3. review-time drafting (`src/vouch/distill.py` + `vouch distill`)

the backstop for every session c2 missed (crashes, ignored nudges, sessions
before this feature). runs only when a human is present.

**trigger.** two entry points, same code path:

- inside `vouch review`: after the reviewer **approves** a session-summary
  page (`proposed_by == "vouch-capture"`, `page_type == "session"`), offer
  `draft decision claims from this session? [y/N]`.
- standalone: `vouch distill <page-id>` for any already-approved session
  page (covers the existing backlog: approve summaries first, distill
  after, in bulk via `vouch distill --all-sessions`).

drafting deliberately happens **after** the summary page is approved: the
page is then a durable kb artifact that can be registered as a source, and
the reviewer has just read it — the approval is also the freshness check.

**evidence mechanics.** distill registers the approved session page as a
content-hashed source (reusing the existing register-source path,
`type=source-summary`). every drafted claim carries at least one
`EvidenceRef` with `source_id` = that source — satisfying the claims-must-
cite-sources validator structurally, and giving the reviewer a one-click
path from claim to the record it was derived from. drafts may add refs to
repo files, subject to the cross-check below.

**llm invocation.** `distill.llm_cmd` in config.yaml — a shell command
template that receives the drafting prompt on stdin and must print json on
stdout (default suggestion in starter config, commented out:
`claude -p --output-format json`). empty/unset ⇒ distill is disabled and
both entry points no-op with a hint. vouch never embeds an api key or picks
a vendor; the llm is deployment config, same philosophy as the mcp server
being deployment config for openclaw.

**drafting prompt.** built from: the approved summary body (which includes
c1's decision candidates, the prompt list, files, commands, git stat) and a
fixed instruction: *draft at most `distill.max_claims_per_session` (default
5) decision/workflow/warning claims strictly supported by this record;
each must quote its supporting line(s); return json
`[{title, body, claim_type, supporting_lines, files}]`; return `[]` if the
record supports no durable claim.*

**validation before filing (mechanical, in distill.py):**

- stdout must parse as json matching the expected shape; anything else is
  logged and dropped — malformed llm output must never write.
- each draft's `supporting_lines` must actually appear in the summary body
  (substring match after whitespace normalization); non-matching drafts are
  dropped, not repaired.
- each draft's `files` must be a subset of the session's files-modified
  list; violations demote the ref (claim keeps only the session-page
  evidence) and add a `flagged: file not in session record` note to the
  rationale.
- surviving drafts are filed via `proposals.propose_claim(...,
  proposed_by="vouch-distill", session_id=<claude-sid>,
  rationale="drafted from approved session page <id>; supporting lines
  quoted in body")` → `PENDING`.

**review loop.** when triggered from `vouch review`, the freshly filed
drafts are appended to the current review pass so the human judges them
immediately, evidence in hand. `vouch review` already renders pending
claims; no new ui.

## review-gate compliance

- distill and nudge never call `approve()`; drafts are ordinary pending
  proposals from distinct actors (`vouch-distill`, and the agent itself for
  c2) — auditable, rejectable, and subject to the same queue as any write.
- no "high-confidence fast lane": a draft that looks perfect still waits
  for a human. any future auto-approve idea is explicitly rejected here —
  it would rebuild akbp's leak 3 with extra steps.
- the llm runs only at review time on the human's machine, reading material
  the human just approved. it never sees the live conversation and cannot
  be steered by in-session prompt injection into writing memory.

## registration / parity

`vouch distill` and `vouch capture nudge` are cli-only plumbing (human-side
and hook-side respectively) — not `kb.*` methods, no mcp/jsonl/capabilities
registration, `test_capabilities` untouched. same reasoning as capture
observe/finalize: an agent must not be able to invoke drafting, and the
nudge is harness infrastructure.

## config (`distill.*`, read defensively, template: capture.load_config)

- `distill.enabled` — default `true` (gates c1's section and c2's nudge).
- `distill.llm_cmd` — default empty ⇒ c3 disabled.
- `distill.max_claims_per_session` — default `5`.
- `distill.nudge_min_observations` — default `8`.
- `distill.nudge_max_per_session` — default `2`; `0` disables the nudge.

added to `storage.py` `_starter_config` with the llm_cmd suggestion
commented out.

## validation experiment (before full implementation)

the 33 pending summaries are a free evaluation set. step 1 of the plan is a
throwaway run of the c3 drafting prompt over 5 of them (manually, no code):
if fewer than half the drafted claims are ones the user would approve, the
capture record is too thin for drafting and c3's prompt (or capture's
retention) needs rework before any code is written. the experiment's
fixtures become `tests/test_distill.py` fixtures.

## explicitly out of scope (yagni)

- **no auto-approve**, no trusted-agent shortcut, no confidence thresholds
  that skip review.
- **no transcript_path parsing.** drafting reads only the approved summary;
  the full transcript stays outside vouch (privacy, size, and the summary
  is the reviewed artifact). revisit only if the experiment shows the
  record is too thin.
- **no llm at capture time.** capture.py's zero-llm rule is untouched; c1
  is quoting, not interpretation.
- **no embedding dedupe of drafts** against existing claims — `kb.dedup_scan`
  exists for reviewers; wiring it into the distill flow is a follow-up.
- **no cursor/codex nudge wiring** — c2 is claude-code-specific; c3 is
  host-agnostic and covers other hosts' sessions via their capture path
  when they get one.

## testing (`tests/test_distill.py`, plus test_capture.py additions)

- c1: prompts with decision-shaped lines produce the section (verbatim,
  capped); no matches ⇒ no section.
- c2: nudge exits 0 when `stop_hook_active`, below threshold, over
  per-session max, or `distill.enabled=false`; blocks with instruction json
  exactly when gated conditions are met; watermark advances.
- c3: llm stubbed with a fixture script (`distill.llm_cmd` pointing at a
  test helper). drafts land `PENDING`, `proposed_by="vouch-distill"`, each
  citing the registered session-page source. malformed json ⇒ nothing
  written. fabricated supporting_lines ⇒ draft dropped. file outside
  session record ⇒ ref demoted + rationale flagged. `llm_cmd` unset ⇒
  no-op with hint. distill never changes any proposal's status.
- fully offline; the stub replaces any real llm.

## files touched

- new `src/vouch/distill.py` — prompt build, llm_cmd invocation, json
  validation, evidence registration, propose_claim calls.
- `src/vouch/capture.py` — c1 section in `build_summary_body`; nudge
  watermark helpers.
- `src/vouch/cli.py` — `vouch capture nudge`, `vouch distill`, review-flow
  offer hook-in.
- `src/vouch/storage.py` — `distill.*` starter config.
- `adapters/claude-code/.claude/settings.json` — `Stop` hook entry.
- `adapters/claude-code/install.yaml` — document the new hook.
- new `tests/test_distill.py`; additions to `tests/test_capture.py`.
- `CHANGELOG.md` `[Unreleased]` (at ship time).

## open questions / risks

1. **nudge token cost.** c2 adds a model continuation up to twice per
   session. gated hard by thresholds, but if it annoys in practice the
   default for `nudge_max_per_session` drops to 0 (opt-in) — cheap to flip.
2. **drafting quality on thin records.** basenames + first-lines may not
   support good claims; the validation experiment answers this before code
   is written. fallback: enrich capture (e.g. retain failed→passed command
   pairs) rather than reach for the transcript.
3. **backlog ordering.** `vouch distill --all-sessions` drafts only from
   *approved* session pages; the 33 pending ones must be approved (or
   rejected) first. acceptable, or does the backlog need a
   bulk-approve-then-distill helper? current answer: `vouch review` is the
   helper; revisit after the experiment.
4. **llm_cmd contract.** one shell template receiving prompt on stdin,
   emitting json on stdout, is the whole interface. confirm this is enough
   for the deployments that matter (claude -p, api scripts, local models)
   before freezing the config key.

## phase 1 amendment (2026-07-05, post-review)

owner review re-scoped phase 1 and moved one decision: the llm narrative is
generated **at session end**, not only at review time. rationale: sessions
close unexpectedly, so generation must be post-session from the session
record — never dependent on the in-session agent's cooperation. in-session
self-report (c2) and review-time claim drafting (c3) move to later phases;
c1 (decision candidates) also deferred.

shipped in phase 1:

- **`capture.summary_mode: auto | manual | off`** (default `auto`) +
  **`capture.summary_llm_cmd`** (default empty ⇒ mechanical-only). the llm
  is deployment config: any shell command reading the record on stdin and
  printing markdown on stdout — `claude -p` reuses claude code's own auth;
  codex/cursor deployments point at their own cli or an api-key script.
- **auto**: `SessionEnd` → `vouch capture finalize` builds the mechanical
  rollup, then `src/vouch/summarize.py` generates a narrative from it plus
  a transcript excerpt (`transcript_path` from the hook payload; prose
  turns only, tail-truncated). the narrative lands as an `## ai summary`
  section inside the same PENDING page. any llm failure/timeout degrades
  to mechanical-only; the proposal always files. adapter sets the
  SessionEnd hook timeout to 120s to cover generation.
- **manual / backlog**: `vouch summarize <proposal-id>` and
  `vouch summarize --all [--force]` enrich pending captured-session pages
  after the fact — covers sessions swept by `finalize-all` (which skips
  llm calls by design) and the pre-existing backlog.
- **review gate unchanged**: summarize mutates only PENDING proposal
  bodies (pre-review scratch), logs a `capture.summarize` audit event, and
  never touches proposal status. review ui needs no changes — summaries
  remain ordinary pending page proposals.
- **/vouch-recall upgraded** (claude-code command + openclaw skill mirror):
  hybrid retrieval via `kb_context` (embedding → fts5 → substring) with
  query reformulation, `kb_neighbors` expansion, and a weave-into-answer
  protocol — recalled claims are used as the authoritative baseline and
  cited by claim id + source ids, with an explicit empty-recall statement
  instead of guesses.

## amendment (2026-07-06): sessions are claims, two-stage review

user re-scope: captured sessions must flow through the *claims* lifecycle,
not the pages one, and summarization becomes an explicit human-triggered
step for sessions that end unexpectedly.

- **capture files claim proposals.** `finalize` registers the pre-llm
  mechanical rollup as an immutable `transcript` source
  (locator = transcript path or `claude-code-session:<id>`) and files a
  CLAIM proposal (`type: session`, evidence = that source id) — the
  "claims must cite sources" invariant now holds for captures. legacy
  page-kind captures stay recognized everywhere (`is_capture_proposal`,
  `proposal_body` in capture.py).
- **two-stage review surface.** new methods `kb.list_sessions`
  (buffers + pending capture proposals, each with a `summarized` flag)
  and `kb.summarize_session` (finalize-if-open, llm-enrich the pending
  proposal), registered at all four sites. ui flow: *review page* lists
  unsummarized sessions with a summarize button; once summarized the item
  moves to the *pending page* (the renamed review queue) for
  approve/reject; approved session claims appear on the claims page.
- **sweep is mechanical-only by default.** `vouch capture sweep` no longer
  llm-enriches what it files; a swept tab-close session waits on the
  review page for a human summarize (`--summarize` / `--backlog` restore
  auto-enrichment for timers that want it). clean session ends
  (`SessionEnd` hook) still auto-summarize under `summary_mode: auto`.
- **merge generalized.** `kb.merge_pending` now merges pending proposals
  of a single kind — pages as before, claims by concatenating texts
  (headings demoted) and uniting evidence/tags/entities. audit event
  `proposal.claim.merge`.
- **start-from generalized.** `vouch session start-from <ref>` (and the
  web route) resolves proposal ids, approved claim ids, and legacy page
  ids. the new `/vouch-start <ref>` skill wraps it for agents; vouch-ui's
  claims page shows a copyable `claude "/vouch-start <id>"` command.
