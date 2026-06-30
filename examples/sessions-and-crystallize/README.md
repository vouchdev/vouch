# Agent sessions: start, volunteer, end, crystallize

A full agent run over the JSONL transport. `session_start` opens a run, the
agent files two proposals tagged to the session, `volunteer_context` drains the
relevance-ranked context vouch pushes mid-run, `session_end` closes the run and
reports its proposal ids, and `crystallize` approves every pending proposal in
the session at once and writes a summary page. Mirrors AKBP's
`session-start-harness` plus a batch-approve finish.

## Run it

```bash
./examples/sessions-and-crystallize/run.sh
```

The example is self-contained: it builds a throwaway KB in a `mktemp -d`
directory, runs against it, and cleans up on exit. To verify against a specific
binary, override `VOUCH`:

```bash
VOUCH=/path/to/vouch ./examples/sessions-and-crystallize/run.sh
```

## What it does

1. `vouch init` creates a `.vouch/` KB, then `cd`s in so the JSONL server
   discovers the root from cwd.
2. Two config opt-outs keep this a readable single-agent demo:
   - `volunteer.threshold` is lowered so the seeded claim qualifies under the
     fts5 backend (this throwaway KB has no embeddings);
   - `review.approver_role: trusted-agent` lets `example-agent` approve its own
     session proposals at crystallize time. The default gate forbids
     self-approval — see the lifecycle examples for the two-agent flow.
3. Seeds one approved claim about an `acme-example` deploy so
   `volunteer_context` has something relevant to offer. Ids are generated
   server-side, so each is captured from one response envelope before being
   referenced in the next.
4. Runs the session flow against **one persistent** `vouch serve --transport
   jsonl` process. The volunteer queue lives in process memory, so
   `session_start` and `volunteer_context` must share a connection. A small
   python driver writes one request, reads one response envelope, and prints
   the parts that matter:

   - `kb.session_start{task}` → a stable `session_id`; opening the session also
     kicks off volunteer evaluation against the task.
   - `kb.propose_claim` ×2 with `session_id` → two pending proposals tagged to
     the run.
   - `kb.volunteer_context{session_id}` → the `volunteers` array: claims whose
     normalized relevance to the task exceeds the threshold, each with a `why`.
   - `kb.list_pending` → `2` (the two session proposals).
   - `kb.session_end{session_id}` → backfills and reports `proposal_ids`.
   - `kb.crystallize{session_id, write_summary_page:true}` → `approved[]` plus a
     `summary_page_id`; one call approves every still-pending proposal in the
     session and writes a session-summary page linking the approved claims.
   - `kb.list_pending` → `0`.
5. Asserts the offer cleared the threshold, that pending went `2 → 0`, that
   `session_end` reported exactly the two proposal ids, and that `crystallize`
   approved both with no failures and wrote a summary page.

The request/response envelope is the whole contract a host mirrors:

```text
request   {"id": "...", "method": "kb.<name>", "params": {...}}
success   {"id": "...", "ok": true,  "result": {...}}
failure   {"id": "...", "ok": false, "error": {"code": "...", "message": "..."}}
```

`crystallize` is the batch-approve convenience: instead of approving each
proposal by id, an agent closes a clean run by approving everything it filed in
the session in a single reviewed step, leaving a readable summary page as the
next session's entry point.

## Expected output

```text
== vouch sessions + crystallize example ==

-- session flow (one persistent jsonl connection) --
session_start  -> sess-20260630-022853-486bd8
propose_claim  -> 20260630-022853-d21c5968
propose_claim  -> 20260630-022853-19192fc2
volunteer_context -> 1 offer(s)
    claim=acme-example-deploys-via-blue-green-with-a-ten-minute-soak-b relevance=1.00
list_pending (before crystallize) -> 2 pending
session_end    -> proposal_ids=['20260630-022853-19192fc2', '20260630-022853-d21c5968']
crystallize    -> approved=['acme-example-rollbacks-must-drain-the-blue-pool-before-promo', 'deploy-soak-window-should-be-raised-to-fifteen-minutes-for-a']
               -> summary_page_id=session-sess-20260630-022853-486bd8
list_pending (after crystallize)  -> 0 pending
assertions ok

== vouch sessions + crystallize example passed ==
```

## Methods demonstrated

- `kb.session_start` — open a run with a task; returns a stable `session_id`
  and starts volunteer evaluation against the task.
- `kb.volunteer_context` — drain the relevance-ranked claims vouch queued for
  the session mid-run, each with a `relevance` score and a `why`.
- `kb.session_end` — close the run; backfills and reports the `proposal_ids`
  filed under the session.
- `kb.crystallize` — approve every still-pending proposal in the session in one
  reviewed step and (optionally) write a session-summary page.
- `kb.list_pending` — the pending-proposal queue, shown before (`2`) and after
  (`0`) crystallize.
