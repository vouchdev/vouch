# Preview a write before it touches the queue

Adapter-contract example: show that a write can be *previewed* without
filing anything, that a real propose lands a PENDING proposal, and that
only `kb.approve` makes it durable. The review gate cannot be bypassed.

This mirrors AKBP's `dry_run:true` / `approval_required` / `approved:true`
triad, expressed in vouch's own vocabulary over the JSONL transport:

- `kb.propose_claim` with `dry_run:true` **previews** — the response shows
  `dry_run:true, status:pending`, but nothing reaches the queue.
- `kb.propose_claim` without `dry_run` files a **PENDING proposal**.
- `kb.approve` (run by a *different* actor — vouch forbids self-approval)
  turns that proposal into a **durable claim**.

A dry-run never reaches `list_pending`; a pending proposal is not readable
as a claim via `read_claim` until it is approved.

## Run it:

```bash
VOUCH=/path/to/vouch ./run.sh
# or, with vouch on PATH:
./run.sh
```

The script builds a throwaway KB in `$(mktemp -d)`, feeds a JSONL request
sequence to `vouch serve --transport jsonl`, asserts the gate held at every
step, and cleans up.

## The sequence

1. `kb.register_source` — a claim must cite a source, so register one first.
2. `kb.propose_claim` with `dry_run:true` → `result.dry_run:true`, no queue entry.
3. `kb.list_pending` → `[]` (the preview filed nothing).
4. `kb.propose_claim` (no `dry_run`) → a real `proposal_id`.
5. `kb.list_pending` → exactly one pending proposal.
6. `kb.approve` (as `alice-example`, not the proposing `example-agent`) → a durable claim id.
7. `kb.read_claim` → the now-durable claim, `status:working`, `approved_by:alice-example`.

The proposing agent and the approving agent differ on purpose: vouch
rejects self-approval (`forbidden_self_approval`) unless you opt in with
`review.approver_role: trusted-agent`. That separation *is* the gate.

## Real output excerpt

```
--- dry_run:true preview, then list_pending ---
{"id": "preview", "ok": true, "result": {"proposal_id": "...", "status": "pending", "kind": "claim", "dry_run": true, ...}}
{"id": "pending-after-preview", "ok": true, "result": []}

--- propose_claim (filed), then list_pending ---
{"id": "file", "ok": true, "result": {"proposal_id": "20260630-...-8e88ce73", "status": "pending", "kind": "claim", "dry_run": false, ...}}
{"id": "pending-after-file", "ok": true, "result": [{"id": "20260630-...-8e88ce73", "kind": "claim", "proposed_by": "example-agent", ...}]}

--- approve, then read_claim ---
{"id": "approve", "ok": true, "result": {"kind": "claim", "id": "acme-example-requires-a-rollback-path-before-any-production-", ...}}
{"id": "read", "ok": true, "result": {"id": "acme-example-...", "status": "working", "approved_by": "alice-example", ...}}

=== assert the gate held at every step ===
ok  dry_run:true returned a preview (status=pending, dry_run=true)
ok  list_pending empty after the dry-run preview
ok  propose_claim filed 1 pending proposal: 20260630-...-8e88ce73
ok  kb.approve minted durable claim: acme-example-requires-a-rollback-path-before-any-production-
ok  read_claim returned the durable claim (status=working)

the gate held: nothing durable existed until kb.approve ran.
```

## Methods demonstrated:

- `kb.register_source`
- `kb.propose_claim` (with and without `dry_run:true`)
- `kb.list_pending`
- `kb.approve`
- `kb.read_claim`
