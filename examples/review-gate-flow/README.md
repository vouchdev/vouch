# review-gate-flow/

The review gate, end to end.

The load-bearing invariant of vouch: every durable write passes through a
human review gate. This example registers a source, proposes two claims,
shows the pending queue, rejects one with a reason, approves the other
into a durable claim, and confirms it now appears in search — all from the
CLI.

It mirrors AKBP's `tool-server-approval-flow`, but uses vouch's CLI verbs
(`source add`, `propose-claim`, `pending`, `reject`, `approve`, `status`,
`search`) instead of a JSONL tool server.

## Run it

```bash
VOUCH=/path/to/vouch bash run.sh
```

`run.sh` builds a throwaway KB under `$(mktemp -d)`, runs the whole flow,
and cleans up after itself. `VOUCH` defaults to whatever `vouch` is on
your `PATH`; set it to point at a specific binary.

## The flow

1. **register a source.** Every claim must cite evidence, so a file is
   registered as a `Source` first. `vouch source add` prints the source's
   sha256 id; that id is the citation the claims will hang off of.
2. **propose two claims.** `vouch propose-claim --text ... --source <sha>`
   files a proposal and prints its id. Proposals are **not** durable
   writes — they sit in a pending queue. Nothing is in the KB yet.
3. **show the queue.** `vouch pending` lists both proposals awaiting
   review.
4. **reject one, with a reason.** `vouch reject <pid> --reason '...'`
   refuses the unverified claim. The reason is mandatory — it becomes
   future-agent context and is recorded in the audit log.
5. **approve the other.** `vouch approve <pid>` converts the good proposal
   into a durable `claim/`. The gate refuses *self-approval*: the agent
   that proposed a claim cannot approve it, so a different actor (a human
   reviewer, `alice-example`) approves. `VOUCH_AGENT` is the actor each
   surface attributes the action to.
6. **confirm.** `vouch status` shows the rejected claim never landed, and
   `vouch search jwt` finds the approved one — only durable claims are
   indexed for retrieval.

## What you'll see

```
=== the review queue (2 pending) ===
• 20260630-022513-16ea1fc6  [claim]  by example-agent
    sessions live forever
• 20260630-022513-81c38485  [claim]  by example-agent
    auth uses jwt

=== reject the unverified claim, with a reason ===
Rejected 20260630-022513-16ea1fc6

=== approve the good claim into a durable artifact ===
Approved → claim/auth-uses-jwt

=== status: 1 durable claim, 0 pending ===
KB at /tmp/tmp.3NqjLeLFIR/.vouch
  durable: 2 claims  •  1 pages  •  2 sources  •  0 entities  •  0 relations
  pending: 0 proposals
  audit:   6 events  •  index: present

=== search finds the approved claim ===
claim/auth-uses-jwt	auth uses «jwt»  (fts5)
```

(`vouch init` seeds one starter claim, so `status` reports 2 durable
claims total — the starter plus `auth-uses-jwt`. The rejected
`sessions live forever` proposal is absent from both `status` and
`search`, which is the whole point of the gate.)

## Methods demonstrated

- `kb.register_source` — `vouch source add`
- `kb.propose_claim` — `vouch propose-claim`
- `kb.list_pending` — `vouch pending`
- `kb.reject` — `vouch reject`
- `kb.approve` — `vouch approve`
- `kb.status` — `vouch status`
- `kb.search` — `vouch search`
