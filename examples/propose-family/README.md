# propose-family — propose every artifact kind

All four proposal entry points plus the path-based source registrar in one
runnable pass. It proposes a claim, a page, an entity, and a relation, and
registers a source from a file already under the KB root via
`register_source_from_path` (the safe sibling of inline `register_source`).
The three assertion-shaped writes are left PENDING to show the gate stays
closed until a reviewer acts.

## Run it

```bash
./examples/propose-family/run.sh
# or against a specific binary:
VOUCH=/path/to/vouch ./examples/propose-family/run.sh
```

The script builds a throwaway KB in `$(mktemp -d)`, runs as
`VOUCH_AGENT=example-agent`, and cleans up on exit. Source data is generic
(`acme-example`, `alice-example`).

## What it does

1. `vouch init` a fresh KB and `cd` into it.
2. Write `notes/acme-brief.md` under the KB root, then call
   **`kb.register_source_from_path`** over the JSONL transport. A source is
   content-addressed evidence, not an assertion, so it is durable on write —
   there is no proposal to approve.
3. **`kb.propose_entity`** twice (`acme-example` as `company`,
   `alice-example` as `person`).
4. Approve both entities **as a separate reviewer** so later writes can point
   at them (see the ordering note below).
5. **`kb.propose_relation`** `alice-example --works_at--> acme-example`.
6. **`kb.propose_claim`** citing the durable source.
7. **`kb.propose_page`** linking the two durable entities.
8. **`kb.list_pending`** (JSONL) and `vouch pending` (CLI) show the same
   three writes queued; `vouch status` confirms nothing new is durable yet.

## Ordering note (the part that bites)

A relation and a page reference their endpoints by id, and the proposal layer
checks those endpoints **already exist** — a dangling reference is rejected at
propose time, not silently at approve. So:

- a **relation** can only be proposed once both entity endpoints are approved;
- a **page** that links a claim/entity id needs that id durable first;
- a **claim** needs its cited **source** durable — and a source registered via
  `register_source_from_path` is durable immediately, so the claim proposes
  fine.

That is why this example approves the two entities before wiring the relation
and page to them. It also shows the review gate's second rule:
`forbidden_self_approval` — the proposer cannot approve their own write, so the
approve runs under a different `VOUCH_AGENT`.

## Real output (excerpt)

```text
== kb.list_pending (JSONL) — the gate is still closed on the three writes ==
{"id": "pend", "ok": true, "result": [{"id": "...-23eb2141", "kind": "relation", ...},
 {"id": "...-77d95337", "kind": "claim", ...}, {"id": "...-f3398537", "kind": "page", ...}]}

== vouch pending — human view of the same queue ==
• ...-23eb2141  [relation]  by example-agent
• ...-77d95337  [claim]  by example-agent
    alice-example works at acme-example.
• ...-f3398537  [page]  by example-agent
    overview

== status: 2 entities + 1 source durable; relation/claim/page still pending ==
  durable: 1 claims  •  1 pages  •  2 sources  •  2 entities  •  0 relations
  pending: 3 proposals

propose-family example passed
```

## Methods demonstrated

- `kb.register_source_from_path`
- `kb.propose_entity`
- `kb.propose_relation`
- `kb.propose_claim`
- `kb.propose_page`
- `kb.list_pending`
