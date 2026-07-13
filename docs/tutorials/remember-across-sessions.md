# Carry what one Claude Code session learned into the next

By the end of this tutorial you'll have watched one Claude Code session's
hard-won debugging knowledge survive into a brand-new session — through
nothing but normal prompts typed into the Claude Code window. Monday, you
ask Claude Code to fix a production bug; it fixes it **and files what it
learned** into vouch on its own. Tuesday, a fresh session with an empty
context window and zero chat history answers a follow-up question **from
the approved claims, automatically, in 2 turns and 15 seconds** — while
the same question without vouch gets a slower, generic answer that misses
your project's actual history.

You never drive vouch yourself. The agent proposes knowledge over MCP
while it works, hooks auto-capture the session, and every new session
gets the approved claims injected before your first message. The only
vouch command you ever type is the 30-second review — which is the point:
nothing becomes "what the team knows" without a human approving it.

- **Time:** about 15 minutes
- **You'll need:** Python 3.11+, git, Claude Code, and vouch
  (`pipx install vouch-kb`)

Every transcript below is from a real Claude Code session
(`claude-sonnet-5`), captured end-to-end against the current build and
condensed only for length. The demo project is `acme-billing`, a
31-file billing service.

## 0. One-time setup: two commands

From your project root:

```bash
$ vouch init
$ vouch install-mcp claude-code
  + .mcp.json
  + CLAUDE.md
  + .claude/commands/vouch-recall.md
  + .claude/commands/vouch-status.md
  + .claude/commands/vouch-resolve-issue.md
  + .claude/commands/vouch-propose-from-pr.md
  + .claude/settings.json
Done — 7 written, 0 appended, 0 merged, 0 skipped
```

That's the whole integration: `.mcp.json` gives Claude Code the `kb_*`
tools, `CLAUDE.md` teaches it the propose-with-evidence protocol, and
`.claude/settings.json` wires the hooks — `PostToolUse` harvests the
session into a scratch buffer, `SessionEnd` rolls it into one pending
summary, and `SessionStart` injects all approved knowledge
(`vouch recall`) into every new session before your first message. From
here on, you just talk to Claude Code.

## 1. Monday: you type a bug report into Claude Code

The webhook handler is double-fulfilling payments in staging. You open
Claude Code and type what you'd type anyway:

```text
> Customers in staging are being fulfilled twice for a single payment,
  and CI shows a failing test. Find the root cause and fix it, then
  verify with pytest. Also make sure whatever you figure out doesn't get
  lost — remember it for future sessions.

⏺ This looks like a payments/fulfilment codebase with a "double
  fulfilment" bug. Let me first check vouch KB for existing related
  knowledge, then run tests to find the failure.

⏺ Bash(python -m pytest -x -q)
  ⎿ FAILED tests/test_webhooks.py::test_redelivered_event_fulfils_once

⏺ Read(tests/test_webhooks.py)
⏺ Read(src/webhooks.py)
⏺ Read(src/fulfilment.py)
  ⎿ (reads 6 files while narrowing down)

⏺ Root cause found: src/webhooks.py:16 builds the dedup key from
  event.type + event.created instead of the event's unique id. When the
  payment provider redelivers the same webhook (same id, but a new
  created timestamp on retry), the key changes, dedup misses it, and
  fulfil() runs twice — causing the double-fulfilment bug in staging.

⏺ Edit(src/webhooks.py)
⏺ Bash(python -m pytest -q)
  ⎿ 9 passed
```

Note the first line of its plan: with the adapter installed, checking
the vouch KB *before* exploring is the agent's default move — on Monday
the KB is still empty, so it pays the full exploration cost. That's the
cost vouch is about to delete.

## 2. …and it remembers, on its own

You said "remember it for future sessions" — `CLAUDE.md` tells the agent
what that means here: evidence first, then a claim, through the review
gate. Same session, seconds later:

```text
⏺ All tests pass. Now let's persist this via vouch KB per CLAUDE.md's
  workflow (register evidence, propose claim).

⏺ vouch - kb_register_source_from_path (MCP)(src/webhooks.py,
    title: "webhooks.py after dedup-key fix (uses event.id)")
⏺ vouch - kb_propose_claim (MCP)(text: "Double-fulfilment bug (staging,
    root cause): src/webhooks.py deduplicated inbound provider webhooks
    using key = f\"{event.type}:{event.created}\" instead of the event's
    unique event.id…", claim_type: "observation")

⏺ Claim proposed as 20260702-093537-c541041d, pending human approval
  via vouch approve.
```

When you close the window, the `SessionEnd` hook also rolls the
auto-captured activity buffer into a pending session-summary page.
Nobody asked it to. The queue now holds both kinds of memory — what the
session *learned* (the claim, proposed by the agent) and what the
session *did* (the summary, proposed by the capture hook):

```bash
$ vouch pending
• 20260702-093537-c541041d  [claim]  by claude-code
    Double-fulfilment bug (staging, root cause): src/webhooks.py dedupl…
• 20260702-093544-371dff03  [page]  by vouch-capture
    session summary: acme (d0cfc6f2-5b23-4f23-951b-515329a40e63)
```

Nothing is durable yet. Which brings up the obvious question —

## 3. What if the agent just approves its own claim? It can't.

```bash
$ VOUCH_AGENT=claude-code vouch approve 20260702-093537-c541041d
✗ 20260702-093537-c541041d: forbidden_self_approval: claude-code cannot
  approve their own proposal (set review.approver_role: trusted-agent in
  config.yaml to opt out)
```

This is the difference between vouch and an agent "memory" file: an agent
can silently write a hallucination into a memory file and every future
session inherits it as truth. Here, nothing the agent believes becomes
context for future sessions until a different actor signs off. So you
review — the claim matches the failing-then-passing test and the diff —
and approve. Thirty seconds:

```bash
$ vouch approve 20260702-093537-c541041d 20260702-093544-371dff03 \
    --reason "matches the failing-then-passing test and the fix diff"
Approved → claim/double-fulfilment-bug-staging-root-cause-src-webhooks-py-ded
Approved → page/session-summary-acme-d0cfc6f2-5b23-4f23-951b-515329a40e63
```

## 4. Tuesday: you type a question — Claude already checked the claims

Next morning a teammate opens a PR. You start a **fresh** Claude Code
session — empty context, no chat history. As the window opens, the
`SessionStart` hook has already run `vouch recall` and injected the
approved claims; you see nothing, you just type your question:

```text
> Quick check before I approve a teammate's PR: it changes the webhook
  dedupe key from event.id to f"{event.type}:{event.created}" so the
  keys are human-readable in the logs. Any reason not to merge it?

⏺ That change would reintroduce a bug that was already fixed here. Let
  me verify current state.

⏺ Bash(grep -n "key = " src/webhooks.py)
  ⎿ 16:    key = event.id

⏺ Do not merge this. src/webhooks.py:16 currently uses key = event.id
  on purpose — that's a fix for a prior double-fulfilment bug, not an
  oversight.

  Per the vouch KB (claim
  double-fulfilment-bug-staging-root-cause-src-webhooks-py-ded):

  - The original code used key = f"{event.type}:{event.created}" for
    the _seen dedup guard.
  - When the payment provider redelivers/retries the same webhook
    event, it can carry a different created timestamp while keeping the
    same id. That changes the dedup key, so _seen doesn't catch the
    retry, and fulfil(charge) runs twice — double-charging the customer.
  - Fix was switching the key to event.id (the actually-unique
    identifier).
  - Regression coverage:
    tests/test_webhooks.py::test_redelivered_event_fulfils_once —
    redelivers the same event id with a different created timestamp and
    asserts fulfilment only happens once.
```

**2 turns. 15 seconds. One grep instead of Monday's exploration.** No
file spelunking, no re-deriving the failure mode: the approved claim was
already in context (about 260 tokens of digest), Claude cited it by id,
confirmed the code still matches with a single grep, and named the exact
regression test the PR should trip. For anything deeper it can pull
details on demand with `kb_search` / `kb_read_page` — but here the
digest alone carried the answer.

## 5. The same question, without vouch

We asked the identical question in an identical copy of the repo with no
vouch — no KB, no MCP, no hooks. The answer arrived in 22.8 seconds
(33% slower, 47% more output tokens) and *sounds* fine:

```text
> Quick check before I approve a teammate's PR: it changes the webhook
  dedupe key from event.id to f"{event.type}:{event.created}" so the
  keys are human-readable in the logs. Any reason not to merge it?

⏺ Yes — don't merge as-is. f"{event.type}:{event.created}" isn't a safe
  dedupe key:
  - Not guaranteed unique. created is typically second-level
    granularity. Any burst of same-type events in the same second …
    collide and get silently dropped as "duplicates" …
  - event.id exists specifically for this. Providers guarantee event.id
    is unique per delivery …
```

Same recommendation — but look at what's missing. It never read a single
file. It doesn't know this exact change already double-charged customers
in staging. Its stated failure mode (same-second *collisions* dropping
distinct events) is not the one that actually bit this project
(*redeliveries* with fresh timestamps evading dedupe). And it can't warn
the reviewer that `test_redelivered_event_fulfils_once` exists and should
have failed in the PR's CI. A reviewer armed with the generic answer
argues hypotheticals; a reviewer armed with the vouch answer points at
the incident, the commit, and the test.

That's the honest shape of the benefit: vouch doesn't make the model
smarter — it makes the answer *grounded in your project's reviewed
history*, and faster to arrive.

## 6. Proof, not vibes

Six months later, "why do we dedupe by event id?" has a paper trail —
who proposed it (the agent), who approved it (you, with a reason), and
the evidence it cites:

```bash
$ vouch why double-fulfilment-bug-staging-root-cause-src-webhooks-py-ded
why double-fulfilment-bug-staging-root-cause-src-webhooks-py-ded (claim)
  approvedBy -> 10f145bf9b8e4995accfbbe0cf27316e (event)  [2026-07-02T09:37:34+00:00]
  cites -> 51d1379fa833644d97f67b0c5b0f7edade92eb8dafaff32b90d5b1a29d3485db (source)  [2026-07-02T09:37:34+00:00]
```

The KB is plaintext under `.vouch/` and commits with the code, so every
clone and every teammate inherits it. (The capture scratch buffer and the
derived index are gitignored — only reviewed knowledge lands in history.)

## Measured: how much the injected knowledge saves

We benchmarked the payoff on the *fix* task itself. Same 31-file repo
with the dedupe bug, same natural-language prompt ("customers in staging
are fulfilled twice — diagnose, fix, verify"), same model
(`claude-sonnet-5`), same flags, run as headless Claude Code sessions
(`claude -p --output-format json`), four runs per arm. The only
difference: arm B got the `vouch recall` digest — about 260 tokens —
injected the way the `SessionStart` hook does. Every run in both arms
fixed the bug and passed the full test suite, so this compares
equally-successful sessions.

| Metric (mean of 4 runs) | Without vouch | With vouch | Saved |
|---|---|---|---|
| Wall-clock time | 111.9 s | 92.9 s | **17%** |
| Agent turns | 16.5 | 11.3 | **32%** |
| Output tokens generated | 3,274 | 2,259 | **31%** |
| Total tokens processed | 335,745 | 276,068 | **18%** |
| API cost per task | $0.402 | $0.330 | **18%** |

Two details the means understate:

- **The bad-exploration tail disappears.** The worst no-vouch run
  wandered for 23 turns and 436,701 tokens ($0.52); with vouch all four
  runs landed in a tight 11–12 turns. Worst case vs worst case: 48%
  fewer turns, 35% fewer tokens.
- **The slowest with-vouch run still beat the fastest no-vouch run** on
  both time and turns — the digest doesn't just help on average, it
  moves the whole distribution.

And on the Tuesday Q&A above, the vouch session answered 33% faster
(15.2 s vs 22.8 s) with 47% fewer output tokens (802 vs 1,505) — while
being the only one grounded in the project's actual incident.

Treat these numbers as a floor, not a ceiling. In this benchmark the
root-cause fact was still discoverable in a buried ops note, so the
no-vouch sessions could eventually dig it out. On a real codebase —
thousands of files, and knowledge that only ever existed in last Monday's
debugging session — the no-vouch arm doesn't just spend more tokens, it
risks landing on the wrong fix entirely. A ~260-token digest is cheap
insurance against re-buying knowledge you already paid for.

## What you got

| Without vouch | With vouch |
|---|---|
| Every session starts at zero; Monday's debugging is re-done on Tuesday | The next session opens with Monday's root cause injected on turn one |
| Review questions get generic best-practice answers | Answers cite your incidents, your commits, your regression tests |
| Agent memory files accumulate unreviewed hallucinations as "truth" | Nothing enters context without a human approving it — self-approval is refused |
| "Why is it built this way?" → archaeology through old chats | `vouch why` / `vouch synthesize` → cited answers with an audit trail |
| Knowledge lives in one person's chat history | It's committed next to the code — every clone and teammate inherits it |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| The agent fixes things but never proposes claims | `CLAUDE.md` from `vouch install-mcp` is missing or was overwritten — re-run the install; check `.mcp.json` lists the `vouch` server. |
| New sessions don't seem to know anything | Nothing approved yet (`vouch pending` shows the queue), hooks not active (project not trusted, or `.claude/settings.json` missing), or `recall.enabled: false` in `.vouch/config.yaml`. The digest only ever contains approved knowledge. |
| No session summary appears at session end | The session had fewer than `capture.min_observations` (default 3) observations — quick Q&A sessions intentionally don't clutter the queue. |
| `forbidden_self_approval` when *you* approve | Your `VOUCH_AGENT` (or username) matches the proposer. Approve as a different actor. |

## Next steps

- Per-host wiring details (Cursor, Codex, and seven other MCP hosts):
  [`connect-coding-agent.md`](connect-coding-agent.md).
- The auto-capture mechanics in depth:
  [`../example-session.md`](../example-session.md).
- Share the reviewed KB with your team:
  [`share-a-knowledge-base.md`](share-a-knowledge-base.md).
