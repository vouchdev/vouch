# Export the provenance DAG and rebuild its cache

Render where every durable claim came from as a Graphviz `dot` graph or a
mermaid flowchart, slice it to one agent session's subgraph, then rebuild the
derived `prov_edges` cache that keeps graph queries fast.

vouch tracks the provenance of every durable claim: which **source** it cites,
which **session** proposed it, which **decision** approved it. Those links form
the provenance DAG. `kb.graph_export` renders that DAG; `kb.provenance_rebuild`
regenerates the `prov_edges` cache — derived state that is a pure acceleration
of what the durable yaml/md files already say, so it is always safe to rebuild.

## Run it:

```bash
bash run.sh
# or against a specific binary:
VOUCH=/path/to/vouch bash run.sh
```

## Steps

1. `vouch init` a fresh KB and register a file as a **source** (`vouch source add`).
2. Start an agent **session** so the proposals it raises are stamped with its id.
3. Propose two connected claims inside that session over the jsonl transport
   (so `session_id` can be passed); both cite the same source.
4. Approve both as a **different actor** — the gate refuses self-approval. The
   two claims now share a source and a session, forming one connected subgraph.
5. `vouch graph --format mermaid` and `vouch graph --format dot` render the
   whole DAG. Edge kinds: `cites`, `approvedBy`, `proposedIn`, `embeds`.
6. `kb.graph_export{format:dot, session:<id>}` over jsonl exports just that
   session's subgraph — the slice the agent run touched.
7. `vouch provenance rebuild --json` regenerates `prov_edges` and reports the
   edge count. `prov_edges` is derived state, never a source of truth.

## A real slice of the output

The whole-DAG mermaid render, with both claims hanging off the one source and
session:

```
flowchart LR
  n0["9d821d93…b07c16f (source)"]
  n1["acme-example-auth-uses-jwt (claim)"]
  n6["jwt-is-signed-rs256 (claim)"]
  n7["sess-20260630-022904-7a9a2e (session)"]
  n1 -->|cites| n0
  n1 -->|approvedBy| n3
  n1 -->|proposedIn| n7
  n6 -->|cites| n0
  n6 -->|approvedBy| n5
  n6 -->|proposedIn| n7
```

The session-scoped `dot` export drops everything outside bob-example's run,
and the rebuild confirms the cache:

```
=== rebuild the derived prov_edges cache ===
{
  "edges": 8
}

provenance-graph-export example passed
```

## Methods demonstrated:

- `kb.graph_export` — render the provenance DAG as `dot` or mermaid, optionally
  scoped to one session's subgraph.
- `kb.provenance_rebuild` — regenerate the derived `prov_edges` cache from the
  durable files in a single pass.
