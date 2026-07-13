# auto-capture claude code sessions into review-gated summaries

- status: draft, awaiting review
- date: 2026-07-01
- scope: one implementation plan

## goal

once vouch is installed in a workspace, a claude code session that starts and
ends should be captured automatically, rolled up into a single human-readable
summary, and filed as a **pending proposal** so a human approves it before it
becomes durable knowledge. no per-session setup, no agent discipline required,
and no bypass of the review gate.

## north-star fit

vouch's load-bearing invariant is "every write goes through a review gate."
this feature adds an automatic *capture* path but deliberately keeps the
*write* path gated: the captured summary lands as a `PENDING` page proposal via
`proposals.propose_page`, exactly like any hand-filed write. nothing is
auto-approved. the automatic part is the harvesting and the nudge, never the
commit.

## background: the load-bearing constraint

vouch's servers (mcp, jsonl) never see the conversation transcript — they only
see discrete tool calls a client makes (`kb.propose_claim`, `kb.search`, …).
there is no code path that persists agent/user messages to vouch storage. so
"auto-capture a session" cannot mean "vouch reads the chat and extracts facts";
vouch has no chat to read.

the transcript *is* reachable, but only from the **client side**: claude code
hooks receive a stdin payload that includes `session_id`, `cwd`, and
`transcript_path`. that is the seam this design uses.

### how memvid/claude-brain resolves the same feature (reference)

memvid ships a claude code plugin with three hooks (`hooks/hooks.json`):

- `SessionStart` → injects a memory banner into context.
- `PostToolUse` (matcher `*`) → after every tool call, scrapes the tool
  input/output into an "observation" and writes it straight to a single
  `.claude/mind.mv2` file.
- `Stop` → rolls the session's observations up into a summary.

two facts from their code drive our design:

1. **capture is fully passive.** the `PostToolUse` hook harvests tool i/o; the
   agent never decides to record anything. this sidesteps the "agent forgot to
   propose" failure mode.
2. **summarization uses no llm.** `generateSessionSummary` is pure heuristics —
   count observation types, keyword-match "chose"/"decided" as decisions,
   regex file paths out of the transcript, emit a template string ("added 2
   feature(s). fixed 1 bug(s)."). the `Stop` hook also runs `git diff` +
   `find -mmin -30` to catch file edits (`PostToolUse` doesn't fire for `Edit`
   in claude code).

memvid has **no review gate** — everything auto-writes and auto-injects. that
is the one thing vouch must not copy. the adaptation: harvest passively like
memvid, roll up mechanically like memvid, but file the result as **one pending
proposal** instead of a direct write, and **once per session** (not per `Stop`
turn) so the review queue is not flooded.

## design overview

two halves.

**half a — adapter wiring (claude code hooks).** the vouch claude code adapter
registers hooks that harvest tool-use into an ephemeral, gitignored scratch
buffer during the session, and roll it up at session end.

**half b — server-side capture support.** a new `src/vouch/capture.py` module
plus two cli subcommands do the buffer i/o and the mechanical rollup, and file
the summary through the existing `proposals.propose_page` gate.

```
SessionStart hook ─▶ vouch capture banner   (nudge: N summaries awaiting review)
                     (+ existing `vouch status --json`)

PostToolUse  hook ─▶ vouch capture observe   ─▶ append one line to
   (every tool)                                  .vouch/captures/<claude-sid>.jsonl
                                                 (scratch, gitignored, NOT the kb)

SessionEnd   hook ─▶ vouch capture finalize  ─▶ read buffer + git diff
                                                 ─▶ mechanical rollup (no llm)
                                                 ─▶ proposals.propose_page(PENDING)
                                                 ─▶ delete buffer
```

## components

### 1. scratch buffer (pre-review working material)

- path: `.vouch/captures/<claude-session-id>.jsonl`, one json object per line.
- each line is a compact observation: `{ts, tool, summary, files?, cmd?}`.
  keep it minimal — a one-line summary and file names, **not** full tool
  output, to avoid buffering secrets or large blobs.
- this is scratch, held **outside** the kb. it is the raw material the rollup
  reads; it never becomes durable on its own. it must be gitignored (add
  `.vouch/captures/` to the ignore set).
- correlation key is claude code's own `session_id` (from the hook payload).
  no vouch `Session` object is required for the passive path — the summary
  proposal simply carries `session_id=<claude-sid>` for traceability.

### 2. `vouch capture observe` (cli, called by PostToolUse)

- reads the hook payload from stdin (`tool_name`, `tool_input`,
  `tool_response`, `session_id`, `cwd`).
- skips tools not worth capturing and skips vouch's own mcp tools + the capture
  command itself (no self-capture / recursion).
- dedups within a short window (memvid uses 60s) so repeated identical calls
  don't spam the buffer.
- appends one observation line. must be fast and never block (short timeout,
  swallow errors, always exit 0) — a capture failure must never break the
  user's tool call.
- **startup-cost constraint:** this runs on *every* tool call, so a full
  `import vouch` (pydantic models, index_db, etc.) on each invocation would tax
  every tool. `observe` must take a minimal fast path — read stdin, dedup,
  append a line — importing as little as possible (ideally not the full model
  stack). memvid affords a per-call node process because node starts in
  milliseconds; python's heavier startup makes keeping `observe`'s import
  surface small a real requirement, not a nicety.

### 3. `vouch capture finalize` (cli, called by SessionEnd)

- reads the buffer for `<claude-sid>`.
- backstop harvest for edits `PostToolUse` missed: `git diff --name-only` +
  `git diff --stat` (short timeouts) and recently-modified files.
- if total observations `< capture.min_observations` (default 3), file nothing
  and exit — trivial sessions don't clutter the queue.
- otherwise build the summary markdown body **mechanically, no llm**:
  - header: project, claude session id, time range.
  - "files modified this session": from git diff + recent files.
  - "git changes": `git diff --stat` (truncated).
  - "activity": type counts (reads / edits / writes / commands / searches).
  - "notable commands": a few bash commands run.
  - "observations": the compact one-line summaries, capped.
- file it: `proposals.propose_page(store, title=…, body=…, page_type="session",
  proposed_by="vouch-capture", session_id=<claude-sid>,
  rationale="auto-captured session summary")` → lands `PENDING`.
  - pages do not require citations (only claims do — `proposals.py:122` vs
    `propose_page`), so the summary is a citation-free markdown index.
  - the session's harvested items are referenced **textually** in the body, not
    as `claim_ids` — they were never approved claims, and `propose_page` rejects
    unknown ids (`proposals.py:188`).
- on success, delete the buffer file.

### 4. notification

**adjustment from the earlier "return-value nudge relayed by the agent"
decision — please confirm at review.** that idea assumed capture fires during a
live agent turn (an in-conversation `kb.session_end` call whose response the
agent relays). with hook-driven capture, the `SessionEnd` hook runs *after* the
agent turn is over, so there is no agent to relay anything. the equivalent that
actually works is the memvid pattern:

- **primary — next-session banner.** extend the adapter's existing
  `SessionStart` hook to also check for pending captured summaries and inject a
  line via `hookSpecificOutput.additionalContext`: e.g. "🔔 3 auto-captured
  session summaries awaiting review — run `vouch review`." this surfaces the
  nudge at the start of the next session in that workspace.
- **passive — the queue.** captured summaries are ordinary pending proposals,
  so `vouch pending`, `vouch review`, and the review-ui show them with no extra
  work. note: the review-ui's websocket broadcast fires on mutations made
  *through the web server*; a proposal filed by the cli hook writes a yaml file
  directly, so it appears in the review-ui on next load/refresh, not
  necessarily as a live push (unless the web server grows a filesystem watch —
  out of scope here).
- **secondary — return-value sidebar.** if capture is *also* triggered by an
  in-conversation `kb.session_end` call (not just the hook), attach
  `_meta.vouch_capture = {session_id, summary_proposal_id, hint}` to the
  response so an agent can relay it mid-chat. optional; the banner is the
  path that always works.

### 5. config (`capture.*`, read defensively)

follow the `volunteer_context.load_config` template — `yaml.safe_load` in
try/except, `isinstance(dict)` per level, explicit coercion, hardcoded
defaults. add the namespace to `storage.py` `_starter_config`.

- `capture.enabled` — default `true`. when false, `observe`/`finalize` no-op.
- `capture.min_observations` — default `3`.
- `capture.buffer_dir` — default `.vouch/captures/`.

### 6. adapter changes

- `adapters/claude-code/.claude/settings.json`: add a `PostToolUse` hook
  (`vouch capture observe`) and a `SessionEnd` hook (`vouch capture finalize`);
  extend the existing `SessionStart` hook to inject the review banner alongside
  `vouch status --json`. hooks call the `vouch` console script, consistent with
  the `vouch status --json` hook already shipped (path availability already
  assumed).
- `adapters/claude-code/install.yaml`: document the expanded T4 (hooks now
  cover session capture, not just SessionStart status).

## review-gate compliance

- the only durable artifact is a `PENDING` page proposal; a human approves it
  via `vouch review` / review-ui. no `approve()` is ever called by capture.
- observations live in `.vouch/captures/` (scratch, gitignored) until the
  reviewer approves the rollup — they never enter the kb on their own.
- storage.py stays pure i/o; all rollup/business logic is in `capture.py`.

## registration / parity

`vouch capture observe|finalize` are **cli-only plumbing for the hooks** — an
agent never calls them. they are therefore **not** `kb.*` methods: no
`@mcp.tool()`, no jsonl handler, no `capabilities.METHODS` entry. this sidesteps
the four-site parity burden entirely and leaves `test_capabilities` untouched.
(`test_capabilities` asserts `METHODS == HANDLERS.keys()` — mcp/jsonl parity
only; cli commands are not in that set.)

## explicitly out of scope (yagni)

- **no llm anywhere in the capture path** — mechanical rollup only, matching
  vouch's zero-llm reflex philosophy and keeping the hook fast/offline.
- **no per-observation proposals** — one summary proposal per session, or the
  queue floods. individual claims the agent files via mcp during a session
  (the older "agent proposes as it works" flow) still coexist as their own
  pending items; the summary can mention them but does not depend on them.
- **no auto-approve / no trusted-agent shortcut.**
- **no out-of-band notifier** (email/desktop/slack) — outside vouch's
  local-first, in-process design.
- **stale-session sweep is a follow-up.** `SessionEnd` can be skipped on a hard
  crash (`kill -9`), orphaning a buffer. a later `vouch capture finalize
  --stale` (rolls up buffers with no recent activity) mops that up; not in the
  first cut. buffers left behind are harmless scratch.

## testing (`tests/test_capture.py`)

- `observe` appends an observation line; dedups within the window.
- `finalize` with ≥ `min_observations` files exactly one `PENDING` page
  proposal with `proposed_by="vouch-capture"` and `page_type="session"`.
- `finalize` with `< min_observations` files nothing.
- `capture.enabled=false` → both commands no-op.
- summary body contains the files-modified section and activity counts.
- the proposal stays `PENDING` (capture never approves).
- the buffer file is deleted after a successful `finalize`.
- fully offline: no llm, no network, git calls stubbed or run in a temp repo.

## files touched

- new `src/vouch/capture.py` — buffer i/o, mechanical rollup, summary-body
  builder, `propose_page` call.
- `src/vouch/cli.py` — `vouch capture` group (`observe`, `finalize`).
- `src/vouch/storage.py` — `capture.*` in `_starter_config`; ensure
  `.vouch/captures/` is gitignored.
- `adapters/claude-code/.claude/settings.json` — PostToolUse + SessionEnd hooks;
  SessionStart banner.
- `adapters/claude-code/install.yaml` — document expanded T4.
- new `tests/test_capture.py`.
- `CHANGELOG.md` `[Unreleased]` entry (follow-up at ship time).

## open questions / risks

1. **notification adjustment** (section 4) — confirm the next-session banner as
   the primary channel, replacing the "agent relays a return value" idea that
   the hook-driven lifecycle makes unworkable.
2. **secret hygiene** — `observe` stores one-line summaries + file names, not
   full command output, to avoid buffering credentials. confirm that's
   conservative enough, or add a redaction pass.
3. **`SessionEnd` vs `Stop`** — this design uses `SessionEnd` (fires once per
   session) rather than memvid's `Stop` (fires per turn) to keep one summary
   per session. confirm that's the desired granularity.
4. **cross-tool reach** — the hooks are claude-code-specific. cursor/codex would
   need their own adapter wiring later; the `capture.py` core is host-agnostic
   and reusable.
5. **per-tool-call process cost** — see the `observe` startup-cost constraint.
   if a minimal python fast path still adds noticeable per-tool latency, a
   fallback is a tiny standalone appender script shipped with the adapter
   (à la memvid's node hooks) instead of routing through the `vouch` console
   script.
