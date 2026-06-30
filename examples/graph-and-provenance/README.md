# Walk the graph: neighbors, why, trace, impact

The provenance and graph-query surface. A vouch KB isn't just a list of
claims — the durable files form a typed graph: entities link through approved
relations, claims cite the source they came from, pages embed claims, newer
claims supersede older ones, and every approval is an event in the audit log.
This example builds a small connected graph and then walks it four ways.

- **`neighbors`** — list the graph neighbors of any node (entity, claim, page,
  or source).
- **`why`** — explain why a claim exists: the source it cites, the session it
  was proposed in, the supersede chain behind it, and the approval event.
- **`trace`** — find the shortest typed-edge path between two artifacts. Exits
  non-zero with `no path` when they're disconnected.
- **`impact`** — show what depends on a claim, and what breaks under a
  hypothetical lifecycle op (`--if archive`).

## Run it

```bash
./run.sh
# or against a specific binary:
VOUCH=/path/to/vouch ./run.sh
```

The script builds a fresh KB in a temp dir, then tears it down on exit.

## What it builds

A connected entity/relation/claim graph, so every command has real edges to
walk:

1. two entities — `alice-example` (person) and `acme-example` (company) — and
   an approved `owned_by` relation between them.
2. two release claims, each citing its own source note, both proposed inside
   one agent session (over the JSONL transport, so they carry a `session_id`).
3. `v2 supersedes v1`, and a page (`acme-example-release-log`) that embeds v1.
4. one unrelated claim (`the-launch-day-weather-was-sunny`) that shares no
   edges — used to demonstrate the `no path` case.

Writes go through the review gate the whole way: `example-agent` proposes,
`example-reviewer` approves (the gate refuses self-approval).

## What each command shows

`neighbors alice-example --depth 2` follows approved relations out from the
entity:

```json
{
  "depth": 2,
  "edges": [
    { "relation": "owned_by", "relation_id": "alice-example-owned-by-acme-example",
      "source": "alice-example", "target": "acme-example" }
  ],
  "nodes": [
    { "distance": 1, "id": "acme-example", "kind": "entity",
      "relation": "owned_by", "summary": "acme-example", "via": "alice-example" }
  ]
}
```

`why alice-example-shipped-acme-example-v2 --depth 4` expands provenance,
including the recursive supersede chain:

```text
why alice-example-shipped-acme-example-v2 (claim)
  approvedBy -> fd3faf1f… (event)
  cites -> adcd2e72… (source)
  proposedIn -> sess-20260630-023051-64329a (session)
  supersedes -> alice-example-shipped-acme-example-v1 (claim)
    approvedBy -> be289f08… (event)
    cites -> bbf91a86… (source)
    proposedIn -> sess-20260630-023051-64329a (session)
```

`trace` finds the shortest typed-edge path, and reports `no path` (exit 1) for
a disconnected pair:

```text
alice-example-shipped-acme-example-v2 -> acme-example-release-log (2 hops)
  alice-example-shipped-acme-example-v2 ->[supersedes] alice-example-shipped-acme-example-v1
  alice-example-shipped-acme-example-v1 <-[embeds] acme-example-release-log

no path: alice-example-shipped-acme-example-v2 -> the-launch-day-weather-was-sunny
```

`impact … --if archive` lists dependents and dry-runs the lifecycle op:

```text
impact alice-example-shipped-acme-example-v1 (claim)
  embeds -> acme-example-release-log (page)
  supersededBy -> alice-example-shipped-acme-example-v2 (claim)
no breakage on archive
```

Breakage is the set of **active** pages embedding the claim. Pages created via
the CLI start as drafts, so archiving here is non-blocking (exit 0). Were the
page active, `--if archive` would list it as breakage and exit 1 — that's the
signal CI can gate on before a destructive lifecycle op.

## Methods demonstrated

- `kb.neighbors`
- `kb.why`
- `kb.trace`
- `kb.impact`

(plus `kb.propose_entity`, `kb.propose_relation`, `kb.propose_claim`,
`kb.propose_page`, `kb.approve`, and `kb.supersede` to build the graph.)
