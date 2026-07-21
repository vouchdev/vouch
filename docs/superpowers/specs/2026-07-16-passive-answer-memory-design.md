# passive answer-memory — design

## goal

make the zero-friction memory loop real, end to end:

1. in a session, a user asks a knowledge question ("what's vouch roadmap?")
   and the answer is saved automatically — no command, no human approval.
2. a fresh session answers the same question from vouch memory, without
   re-reading the project files.

this is phase-d's "the human leaves the loop" applied to the *session* path,
not just to `vouch ingest <file>`.

## what already exists (do not rebuild)

- **receipt-gated auto-approve** (#485): a claim whose byte-offset receipt
  verifies (`receipts.evaluate_claim_receipts`) is auto-approved with no human
  when `review.auto_approve_on_receipt` is set. `proposals.approve` also clears
  self-approval under `review.approver_role: trusted-agent`.
- **source → receipt-backed claims** (`extract.segment_source`,
  `extract.extract_receipt_claims`, `proposals.propose_quoted_claim`): split a
  document into verbatim spans, file each as a claim that quotes itself, so its
  receipt verifies by construction.
- **read path**: `recall` (SessionStart digest) and `context-hook`
  (UserPromptSubmit per-prompt injection, via `context.build_context_pack`)
  both surface *approved* claims. verified in an isolated KB: `vouch ingest`
  → claims auto-approved, `recall`/`search` surface them.

the only missing link is a **passive session trigger** that turns a session's
Q&A into an ingested source. that is this feature.

## approach

add a passive answer-capture that fires when the assistant finishes a turn,
extracts the exchange from the transcript, ingests the *answer* as a source,
and lets the existing receipt gate auto-approve the resulting claims.

chosen over the alternative (self-approved mechanical session *page* under
trusted-agent) because it reuses #485's blessed path, yields atomic recallable
claims (the "distill to claims" the user wanted, mechanically, no LLM), and is
less new code. the existing tool-activity page capture is untouched — it is a
separate concern (session audit rollup, still human-reviewed).

## components

### `capture.last_exchange(transcript_path) -> (question, answer) | None`

pure transcript extraction, mirroring the existing `first_user_prompt`. returns
the most recent genuine user prompt and the most recent assistant text turn.
skips host wrapper messages (`<command-name>…`, caveats, meta). no LLM.

### `capture.capture_answer(store, session_id, transcript_path, *, config)`

the passive glue. steps:

1. respect `capture.enabled` and `VOUCH_CAPTURE_DISABLE` (vouch's own LLM
   subprocesses must not capture themselves).
2. `last_exchange` → (question, answer). skip if no answer.
3. **noise guard**: skip answers shorter than `min_answer_chars` (default 160)
   — acknowledgements ("done", "ok") are not knowledge.
4. **dedup**: `sid = sha256_hex(answer_bytes)`; if `store.get_source(sid)`
   already exists, this answer was captured already — skip (idempotent under a
   Stop hook that fires every turn / re-runs).
5. `store.put_source(answer_bytes, title=question, source_type="message",
   tags=["session-answer"], metadata={"session_id", "question"})`.
6. `extract.extract_receipt_claims(store, sid, proposed_by="vouch-capture",
   limit=max_claims)` (default 12) — receipt-backed claim proposals.
7. approve each: `proposals.approve(store, pid, approved_by="vouch-capture")`,
   catching `ProposalError` — succeeds under `trusted-agent` OR
   `auto_approve_on_receipt` (receipts verify by construction); if neither gate
   is on, claims stay **pending** (the review gate is honoured, never bypassed).
8. return `{source, filed, approved}` counts.

### `vouch capture answer` (CLI)

reads a `{session_id, transcript_path}` JSON payload on stdin (same shape the
host Stop hook emits), calls `capture_answer`, always exits 0 — a capture
failure must never break the turn (same contract as `capture observe`).

### Stop hook (claude-code adapter)

wire `vouch capture answer` as a `Stop` hook in
`adapters/claude-code/.claude/settings.json`. the Stop event fires reliably when
the assistant finishes a turn (no dependence on the unreliable window-close
`SessionEnd`), so the answer is durable immediately — session 2 recalls it even
if session 1 is still open.

## config

the loop needs a human-out-of-loop gate on. recommend
`review.auto_approve_on_receipt: true` (the receipt is the reviewer — principled,
mechanical) over blanket `approver_role: trusted-agent`. capture_answer works
under either; with neither, it degrades to filing pending claims.

## known limits (honest, first cut)

- **selection is coarse**: every substantive answer becomes claims, and
  `segment_source` emits one claim per sentence (no compression yet — same
  stance as #485's own commit). `min_answer_chars` + `max_claims` bound the
  noise; smarter selection is a later quality knob.
- claims quote the *assistant's answer*, so provenance is "the session
  concluded this," not "the ground-truth file says this." that is the correct
  semantics for "remember what I answered."

## testing

- unit: `last_exchange` on a synthetic transcript; `capture_answer` →
  approves under receipt gate, leaves pending with gate off, skips short
  answers, dedups a repeat.
- verify (runtime, not unit): drive `vouch capture answer` with a realistic
  session-1 transcript, then in a fresh process run `recall` / `context-hook`
  and confirm the answer is surfaced — the actual two-session demo.

## scope

`src/vouch/capture.py`, `src/vouch/cli.py`,
`adapters/claude-code/.claude/settings.json`, tests. branch `feat/passive-
answer-memory` off `test`. no parallel write path — approval routes through
`proposals.approve`.
