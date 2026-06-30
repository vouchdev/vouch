# audit-timeline

the authoritative history: reading the audit log back as a live timeline.

`audit.log.jsonl` is the only authoritative event stream in a vouch KB —
one append-only line per mutation, hash-chained, never edited by hand.
`decided/` is a queryable summary; the audit log is the legally-authoritative
record. this example performs a register / propose / reject / approve /
supersede sequence and then reads the timeline back, showing how every gate
decision is attributed to an actor with the object ids it touched. it extends
the static log in [`tiny/`](../tiny) into a live walkthrough.

## Run it:

```bash
VOUCH=/path/to/vouch ./examples/audit-timeline/run.sh
```

`run.sh` builds a throwaway KB in `$(mktemp -d)`, runs the sequence, prints
both the human (`vouch audit --tail`) and structured (`vouch audit --json`)
views, and cleans up. `VOUCH` defaults to `vouch`; override it to point at a
specific build.

## What it does

two identities make the review gate real: `example-agent` proposes writes, and
`alice-example` (a human reviewer) decides them. vouch refuses self-approval by
default, so the agent cannot approve its own claim.

1. `vouch init` a fresh KB, then `cd` into it.
2. `vouch source add` — register an `acme-example` RFC file as a Source.
3. `vouch propose-claim` twice — a stale "30 minutes" claim and a bogus
   "never expire" claim, both citing the source.
4. `vouch reject` the bogus claim with a reason.
5. `vouch approve` the 30-minute claim (as the reviewer).
6. propose + approve a corrected "15 minutes" claim.
7. `vouch supersede` the stale claim with the corrected one.
8. read the timeline back: `vouch audit --tail 20`, then
   `vouch audit --json --tail 50`.

## Reading the timeline

each line is one mutation. note that:

- every event carries an **actor** — proposals are `by example-agent`,
  every decision is `by alice-example`. attribution is per-event.
- every event carries **object_ids** — the proposal id on create/reject,
  and on approve *both* the proposal id and the new claim id.
- the log is **append-only and hash-chained**: in the JSON form each event
  has a `hash` and a `prev_hash` pointing at the previous event, so any
  hand-edit breaks the chain.

real output excerpt (`vouch audit --tail 20`):

```
2026-06-30T02:27:54  kb.init                 by example-agent  objects=[]
2026-06-30T02:27:55  source.add              by example-agent  objects=['7242ebfd...']
2026-06-30T02:27:55  proposal.claim.create   by example-agent  objects=['20260630-022755-319ef00d']
2026-06-30T02:27:55  proposal.claim.create   by example-agent  objects=['20260630-022755-040964aa']
2026-06-30T02:27:56  proposal.claim.reject   by alice-example  objects=['20260630-022755-040964aa']
2026-06-30T02:27:56  proposal.claim.approve  by alice-example  objects=['20260630-022755-319ef00d', 'acme-example-access-tokens-expire-after-30-minutes']
2026-06-30T02:27:57  proposal.claim.create   by example-agent  objects=['20260630-022757-e47015a0']
2026-06-30T02:27:57  proposal.claim.approve  by alice-example  objects=['20260630-022757-e47015a0', 'acme-example-access-tokens-expire-after-15-minutes']
2026-06-30T02:27:57  claim.supersede         by alice-example  objects=['...-30-minutes', '...-15-minutes', '...--supersedes--...']
```

and the structured form, where the hash chain is explicit:

```json
{
  "actor": "alice-example",
  "event": "proposal.claim.reject",
  "object_ids": ["20260630-022755-040964aa"],
  "data": { "reason": "contradicts the rfc — tokens are short-lived" },
  "hash": "5ac73ff5...",
  "prev_hash": "79ceac40..."
}
```

## Methods demonstrated:

- `kb.register_source` — `vouch source add`
- `kb.propose_claim` — `vouch propose-claim`
- `kb.reject` — `vouch reject`
- `kb.approve` — `vouch approve`
- `kb.audit` — `vouch audit --tail` / `--json`

(`vouch supersede` also appears, to show a `claim.supersede` event in the
timeline.)
