# Viewer scoping: project and agent filters

vouch's retrieval surfaces — `search`, `context`, and `audit` — are thin
viewports over the storage layer. Each accepts a **viewer** (`--project` /
`--agent`) and filters what that caller may see based on each artifact's
scope. The same query returns different hits for different viewers, and the
audit log narrows the same way. The files on disk never change — scope is a
read-time concern, not a storage one.

## Run it

```bash
VOUCH=/path/to/vouch ./run.sh      # or just ./run.sh if vouch is on PATH
```

The script builds a throwaway KB in `$(mktemp -d)`, seeds four claims at
different scopes, runs the same query as three viewers, and cleans up.

## What it sets up

A claim's `scope` is a metadata field: a `visibility` tier
(`public | team | project | private`) plus an optional `project` / `agent`
binding. Artifacts are plaintext yaml, so the script writes the scope field
directly onto each durable claim and rebuilds the derived index.

| claim | scope | who sees it |
|---|---|---|
| `deploy-database-backups…` | default (`project`, no project bound) | everyone |
| `acme-example-prod-database…` | `project: acme-example` | acme viewers only |
| `other-example-prod-database…` | `project: other-example` | other viewers only |
| `alice-example-secret-rotation-token…` | `private`, `agent: alice-example` | alice only |

The visibility rules (`src/vouch/scoping.py`): `public` / `team` are visible
to all; a `project`-scoped artifact is visible only when the viewer's project
matches (an unbound `project` claim stays public); `private` fails closed —
it needs an exact `agent` match.

## What it shows

The same `search database`, three different viewers:

```text
viewer --project acme-example  (public + acme):
  ['deploy-database-backups-nightly-across-all-regions', 'acme-example-prod-database-lives-in-us-east-1']
viewer --project other-example (public + other):
  ['deploy-database-backups-nightly-across-all-regions', 'other-example-prod-database-lives-in-eu-west-1']
no viewer                      (public only; project/private hidden):
  ['deploy-database-backups-nightly-across-all-regions']
```

A private, agent-scoped claim — visible to its agent, invisible to a project
viewer:

```text
viewer --agent alice-example   (SHOULD see the private claim):
  ['alice-example-secret-rotation-token-refreshes-hourly']
viewer --project acme-example  (must NOT see it):
  []
```

`context` applies the identical filter and echoes the resolved viewer:

```text
viewer {'agent': 'alice-example', 'project': None} items ['deploy-database-backups-nightly-across-all-regions']
```

`audit` narrows per viewer too — it prints the resolved viewer on stderr and
drops events whose artifacts fall outside scope. The acme viewer never sees
other-example's approval:

```text
audit --project acme-example  (other-example's approval is hidden):
  viewer: project='acme-example' agent=None
  2026-…  proposal.claim.approve   by reviewer  objects=[…, 'deploy-database-backups-nightly-across-all-regions']
  2026-…  proposal.claim.approve   by reviewer  objects=[…, 'acme-example-prod-database-lives-in-us-east-1']
```

`status` is the unfiltered baseline — it counts everything on disk, because
scope is a viewer concern, not a storage one (the `5 claims` includes the
starter claim `vouch init` seeds):

```text
  durable: 5 claims  •  1 pages  •  2 sources  •  0 entities  •  0 relations
  pending: 0 proposals
  audit:   10 events  •  index: present
```

## Methods demonstrated

- `kb.search` — viewer-scoped retrieval (`--project` / `--agent`)
- `kb.context` — the same scope filter on a context pack, with the resolved
  viewer echoed back
- `kb.audit` — scope-aware audit reads; events outside the viewer's scope are
  dropped
- `kb.status` — the unfiltered baseline counts, for contrast
