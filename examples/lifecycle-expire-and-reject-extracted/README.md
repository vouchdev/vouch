# Garbage-collecting the queue: expire and reject-extracted

Two bulk queue-hygiene operations for the review gate, both with the same
dry-run-then-apply safety shape. `vouch expire` runs the configured
staleness GC over pending proposals — proposals nobody reviewed in time.
`vouch reject-extracted` mass-rejects the typed edge proposals the
auto-extractor (`vouch-extractor`) files whenever a page is approved, so a
reviewer can clear them in one call instead of one at a time.

Neither operation bypasses the review gate: an expired proposal is a
terminal reject recorded in the audit log, and reject-extracted is an
ordinary reject scoped to `vouch-extractor`-authored relations.

## Run it

```bash
VOUCH=/path/to/vouch ./run.sh        # or just ./run.sh if vouch is on PATH
```

The script builds a throwaway KB in a tempdir, runs the real CLI, prints
each step under a header, asserts the end state, and cleans up.

## The rules

1. **expire is dry-run by default.** `vouch expire` lists what *would*
   expire (`would_expire`) and mutates nothing; `vouch expire --apply`
   commits. Eligibility is `review.expire_pending_after_days` — a positive
   threshold; `0` disables the GC entirely.
2. **Approving a page auto-extracts edges.** A page that links entities
   (`[[wiki-links]]` in the body, or the `entities` / `sources`
   frontmatter) makes `vouch-extractor` file `mentions` / `relates_to` /
   `derived_from` relation proposals on approve. They land in the queue and
   need review like any other write.
3. **reject-extracted is scoped.** It only rejects pending relations whose
   author is `vouch-extractor`; `--page <id>` narrows further to edges
   extracted from one page. A hand-filed relation is never touched.
4. **Two actors.** vouch forbids self-approval, so the example proposes as
   `example-agent` and reviews as `example-reviewer`.

## Steps

1. `vouch init`, then set `review.expire_pending_after_days: 7` in
   `.vouch/config.yaml`.
2. File two claim proposals; backdate one past the 7-day threshold (a
   stand-in for time passing — the proposal stays pending and un-approved).
3. `vouch expire --json` (dry-run) → the stale proposal shows in
   `would_expire`. `vouch expire --apply` → it expires; the fresh one
   survives.
4. Approve an entity, then approve a page that links it — `vouch-extractor`
   queues two edge proposals.
5. `vouch reject-extracted --page acme-example-deploy --reason 'bulk reject'`
   clears both in one call.
6. Assert: `proposal.expire` and the two rejects are in the audit log, and
   only the one fresh claim is left pending.

## Real output (excerpt)

```
-- vouch expire (dry-run) --
{
  "dry_run": true,
  "enabled": true,
  "expired": [],
  "threshold_days": 7,
  "would_expire": [
    {
      "id": "20260630-022846-bbba43eb",
      "kind": "claim",
      "proposed_at": "2026-05-31T02:28:46.725558+00:00",
      "proposed_by": "example-agent"
    }
  ]
}

-- vouch expire --apply --
expired 1 proposal(s) (threshold: 7 days)
  20260630-022846-bbba43eb  [claim]

-- pending after page approve (vouch-extractor edges queued) --
• 20260630-022846-7024dbec  [claim]  by example-agent
    acme-example ships on fridays
• 20260630-022849-4bb1393a  [relation]  by vouch-extractor
    —
• 20260630-022849-b9ea45cd  [relation]  by vouch-extractor
    —

-- vouch reject-extracted --page acme-example-deploy --
Rejected 2 auto-extracted edge proposal(s)
```

## Methods demonstrated

- `kb.expire` — staleness GC over pending proposals (`vouch expire`,
  `--apply`, `--days`, `--json`).
- `kb.reject_extracted` — bulk-reject auto-extracted edges
  (`vouch reject-extracted --page --reason`).
- `kb.list_pending` — the queue before/after each operation (`vouch
  pending`).

Supporting: `kb.propose_claim`, `kb.propose_entity`, `kb.propose_page`,
`kb.approve`, `source add`.
