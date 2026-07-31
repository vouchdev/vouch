# Provenance — `vouch why` / `trace` / `impact`

vouch already records everything needed to explain a claim's existence — the
session that proposed it, the source it cites, the supersedes chain it sits on,
the contradiction that demoted it, the page that embeds it as evidence — but it
is scattered across `audit.log.jsonl`, `relations/`, `evidence/`, `sessions/`
and the claim file itself. The provenance layer reconstructs a single typed
directed graph from those artifacts so a reviewer in front of a 500-item queue
can ask the two obvious questions: *why is this claim here, and what depends on
it?*

Provenance is **derived state**. Nothing here is a source of truth: every edge
is rebuilt from durable files, and the `prov_edges` table in `state.db` is a
disposable cache that `vouch provenance rebuild` reconstructs byte-for-byte. All
mutations still flow through the existing proposal + lifecycle code paths.

## Edge kinds

The graph is typed. An edge `A --kind--> B` means *A is explained by / depends
on B*, so `why` walks outward and `impact` walks inward.

| Kind | From → To | Source artifact |
|------|-----------|-----------------|
| `cites` | claim → source/evidence | `claim.evidence` |
| `derivedFrom` | evidence span → source | `evidence.source_id` |
| `supersedes` | newer claim → older claim | `claim.supersedes` |
| `supersededBy` | older claim → newer claim | reverse view (query-time) |
| `contradicts` | claim → claim | `claim.contradicts` |
| `contradictedBy` | claim → claim | reverse view (query-time) |
| `embeds` | page → claim | `page.claims` |
| `proposedIn` | claim → session | approved proposal `session_id` |
| `approvedBy` | claim → audit event | `proposal.*.approve` log entry |
| `targets` | pending delete → artifact | delete proposal payload |

The two `*By` mirrors are computed at query time by walking inbound, so the
`prov_edges` cache stays free of duplicate rows — only the canonical kinds are
persisted.

## The pending frontier

Pending proposals are nodes too. A proposal is keyed on its own id rather than
on the artifact id its payload would create — until approval that artifact does
not exist, and the prospective id may already be taken — and it hangs off the
graph by the same edge kinds a durable artifact uses: `cites` to the sources it
quotes, `embeds` to the claims a proposed page would collect, `proposedIn` to
the session that filed it, and `targets` to the artifact a delete would remove.

The only thing separating a pending node from an approved one is its `status`.
That is the point: the review gate is the product, and a picture of the KB that
shows only what has already been approved hides the part a reviewer is there to
look at.

## Commands

```bash
vouch why <claim_id>                    # backward: cites, session, supersedes, approval
vouch why <claim_id> --depth 5 --json   # machine-readable provenance tree
vouch trace <a> --to <b>                # shortest typed path between two artifacts
vouch impact <claim_id>                 # forward: pages, downstream claims that depend on it
vouch impact <claim_id> --if archive    # dry-run a lifecycle op; exits non-zero if it breaks something
vouch graph --session <session_id>      # render the DAG for one agent run as dot/mermaid
vouch graph --format json               # nodes + edges for a renderer, status included
vouch provenance rebuild                # rebuild the prov_edges cache from durable files
```

- `vouch why` walks edges outward from a claim and prints a tree grouped by edge
  kind, each leaf carrying its citation target and originating audit timestamp;
  `--json` emits a stable shape (`schema_version` pinned) suitable for tooling.
- `vouch impact` walks inward and lists every artifact that points at the
  target. `--if <archive|contradict|supersede>` dry-runs the lifecycle op
  against the in-memory graph and reports the breakage list — the **active**
  pages that would carry a stale reference — without writing. It exits non-zero
  when the list is non-empty, so it composes into a pre-flight check.
- `vouch trace` finds the shortest typed-edge path between two artifacts
  (edges are crossable either way) and prints it, or exits non-zero with
  `no path` when they are disconnected.

Reviewer output is bare prose + indentation — no curses, no colours by default —
so it diffs cleanly into a `gh pr comment` or a session log.

## Graph export formats

`dot` (default) and `mermaid` render diagram text. `json` returns the graph
itself for a renderer that lays it out:

```json
{
  "nodes": [{"id": "c-new", "kind": "claim", "label": "the newer fact",
             "status": "working"}],
  "edges": [{"src": "page-alpha", "dst": "c-new", "kind": "embeds"}]
}
```

`kind`, `status` and `label` are captured while the graph is built and carried
on it, so serialising costs no file reads — `json` is exactly as cheap as `dot`.
Structural nodes (sources, sessions, audit events) have no review status of
their own and report `""`. The console's Memory view is the first consumer;
`webapp/src/views/MemoryNetworkView.tsx` colours each node by `status`.

## `kb.*` methods

The same surface is reachable over every transport (MCP stdio, JSONL, HTTP):

| Method | Params |
|--------|--------|
| `kb.why` | `claim_id`, `depth` |
| `kb.trace` | `from`, `to` |
| `kb.impact` | `claim_id`, `depth`, `op` |
| `kb.graph_export` | `session`, `format` |
| `kb.provenance_rebuild` | — |

They appear in `kb.capabilities` and pass the JSONL capabilities cross-check.

## The cache

The `prov_edges(src_id, dst_id, kind, event_ts, session_id)` and
`prov_nodes(id, kind, status, label)` tables in `state.db` are a derived index,
gitignored alongside the rest of the cache. A freshness stamp (claim count +
page count + audit-event count + pending-proposal count) lets a cold query
decide whether the cache can be trusted; when stale, `load_graph` rebuilds it
transparently. Correctness never depends on the cache — a rebuild is always an
exact reconstruction of the live in-memory build, which a CI test asserts, and
a cache-loaded graph reports the same kind, status and label as a fresh one.

## Out of scope

- Mutating the graph directly; provenance is derived state.
- Cross-KB / federated provenance.
- Embedding-based "semantic neighbors" — provenance edges are strictly the
  typed, audit-grounded ones.
- Auto-blocking lifecycle ops based on impact size — `vouch impact` advises, the
  human still decides.
