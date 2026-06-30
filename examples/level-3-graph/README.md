# Conformance level 3: a full claim+entity+relation graph

The top-tier `vouch/` snapshot: claims, pages, entities, relations and a
populated provenance graph — the shape a mature KB takes. Mirrors AKBP's
level-3. It shows that the same files-on-disk model scales from a markdown
wiki (see [`../tiny`](../tiny)) up to a typed knowledge graph, and that the
graph and provenance queries have real edges to walk.

The corpus is a small, generic billing platform: a `billing-service` that
depends on `postgres-billing`, owned by the `ledger-api` project and the
`platform-team`, plus an `incident-247` postmortem that introduces
`idempotency-keys`. Every claim cites a registered source; every entity and
relation went through the review gate.

```
vouch/
├── config.yaml
├── schema_version
├── audit.log.jsonl
├── claims/        (5 claims, each with an evidence citation)
├── pages/         (3 pages — narrative over claims + entities)
├── entities/      (6 typed entities: system / project / team / concept / incident)
├── relations/     (6 typed edges: depends_on / owned_by / caused_by / implements)
├── sources/       (registered source documents, by sha256)
└── decided/       (the approved proposals that produced every artifact)
```

`decided/` and `audit.log.jsonl` are the authoritative history — one decided
proposal and a cluster of audit lines stand behind each durable file. The
`state.db` index and `proposed/` queue are derived/gitignored, so they're not
in the snapshot; rebuild the index with `vouch index` after copying in.

## Copy it in and look around

```bash
cp -r examples/level-3-graph/vouch /some/project/.vouch
cd /some/project
vouch index            # rebuild the derived state.db
vouch status
```

`vouch status` reports every kind nonzero — the level-3 fingerprint:

```text
KB at /some/project/.vouch
  durable: 5 claims  •  3 pages  •  4 sources  •  6 entities  •  6 relations
  pending: 0 proposals
  audit:   48 events  •  index: present
```

## Walk the typed entity graph

`vouch neighbors` walks relations and structural edges from any node. Two
hops out from `billing-service` reaches the team that owns the project, the
incident, and the claims embedded in the linked page:

```bash
vouch neighbors billing-service --depth 2
```

```text
postgres-billing   (entity, distance 1, via billing-service:depends_on)
ledger-api         (entity, distance 1, via billing-service:owned_by)
idempotency-keys   (page,   distance 1, via billing-service:implements)
platform-team      (entity, distance 2, via ledger-api:owned_by)
incident-247       (entity, distance 2, via idempotency-keys:caused_by)
... + the two claims embedded in the idempotency-keys page
```

## Export the provenance DAG

`vouch graph` renders the provenance DAG (claims → cited sources → approvals →
embedding pages) as Graphviz dot or a mermaid flowchart:

```bash
vouch graph --format mermaid
```

```text
flowchart LR
  ...
  n8["billing-platform-overview (page)"]
  n9["billing-service-depends-on-postgres-billing-for-all-durable- (claim)"]
  ...
  n8 -->|embeds| n9
  n9 -->|approvedBy| n2
  n9 -->|cites| n4
  n12 -->|embeds| n11
  n13 -->|cites| n3
```

## Over the JSONL transport

The same graph reads are available to any host through
`vouch serve --transport jsonl`. Pipe a request sequence at the KB's cwd:

```bash
printf '%s\n' \
  '{"id":"e","method":"kb.list_entities","params":{}}' \
  '{"id":"r","method":"kb.list_relations","params":{"node_id":"billing-service"}}' \
  '{"id":"g","method":"kb.graph_export","params":{"fmt":"mermaid"}}' \
  | vouch serve --transport jsonl
```

Real responses (abbreviated):

```text
kb.list_entities  -> billing-service, idempotency-keys, incident-247,
                     ledger-api, platform-team, postgres-billing
kb.list_relations -> billing-service depends_on postgres-billing
                     billing-service owned_by   ledger-api
                     idempotency-keys implements billing-service
kb.graph_export   -> {"format":"mermaid","graph":"flowchart LR ..."}
```

`kb.list_relations` with `node_id` returns every edge touching that node;
omit `node_id` for the whole edge set. `kb.graph_export` takes `fmt` =
`mermaid` or `dot`.

## What to look at first

1. [vouch/entities/billing-service.yaml](vouch/entities/billing-service.yaml)
   — the canonical shape of an Entity: id, `type`, `aliases`, `description`.
2. [vouch/relations/billing-service-depends-on-postgres-billing.yaml](vouch/relations/billing-service-depends-on-postgres-billing.yaml)
   — a typed edge: `source` / `relation` / `target` with a confidence.
3. [vouch/pages/billing-platform-overview.md](vouch/pages/billing-platform-overview.md)
   — a Page that references both claims and entities, tying the narrative to
   the graph.
4. [vouch/audit.log.jsonl](vouch/audit.log.jsonl) — the full event stream;
   every entity, relation, claim and page traces back to a proposal here.

## Methods demonstrated

- `kb.list_entities` — enumerate the typed entities (`vouch` exposes these
  through the graph reads and the JSONL transport).
- `kb.list_relations` — enumerate typed edges, optionally filtered to one node.
- `kb.neighbors` — walk the graph N hops from any node (`vouch neighbors`).
- `kb.graph_export` — render the provenance DAG as dot/mermaid (`vouch graph`).

## What this example is *not*

- It isn't pre-loaded with `proposed/` items — proposals are gitignored in
  real KBs, so an example shipping a pending queue would be a lie. Everything
  here is already approved.
- It doesn't ship the derived `state.db`. Run `vouch index` after copying the
  tree in to rebuild it from the durable files.
- It carries the two starter artifacts (`vouch-starter-reviewed-knowledge`
  claim, `edit-in-obsidian` page) that `vouch init` seeds — they're part of a
  real freshly-initialised KB, and the audit log is authoritative, so they
  stay rather than being hand-deleted.
