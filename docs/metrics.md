# `vouch metrics` — observability

`vouch status` tells you the KB is *alive* (artifact counts). `vouch metrics`
tells you the **review gate** and **corpus** are *healthy*: how often proposals
get approved, how stale the corpus is, how long claims sit pending, and who is
doing the proposing and approving.

Everything is computed **read-only** from two sources that already exist on
disk — there is **no new state**, no schema migration, nothing to back up:

- `.vouch/audit.log.jsonl` — the append-only event stream (proposal
  create/approve/reject, claim lifecycle), each event carrying a timestamp and
  an actor.
- the artifact files (`claims/*.yaml`, `sources/*`, `evidence/*`), read through
  the normal `KBStore` API.

## Usage

```bash
vouch metrics                       # human-readable table, all of history
vouch metrics --json                # stable JSON schema (this document)
vouch metrics --prometheus          # Prometheus textfile-collector exposition
vouch metrics --since 30d           # only the last 30 days of the audit log
vouch metrics --since 2026-01-01 --until 2026-02-01
vouch metrics --stale-days 90       # tighten the "stale claim" threshold
vouch metrics --top 10              # show 10 actors in the leaderboard (0 = all)
```

### `--since` / `--until` formats

| Form | Example | Meaning |
|------|---------|---------|
| duration | `30d`, `12h`, `2w`, `90m`, `45s` | counted back from now |
| ISO date | `2026-01-01` | midnight UTC on that date |
| ISO datetime | `2026-01-01T06:30:00+00:00` | exact instant (naive → UTC) |
| `all` / omitted | — | no bound |

The window applies to **audit-derived** metrics (the review gate, lag,
actors). **Corpus** metrics (citation coverage, stale ratio, status histogram)
always reflect *current* on-disk state — a claim is stale now regardless of the
window you ask for.

## Metrics

| Metric | Meaning |
|--------|---------|
| `approval_rate` | `approvals / (approvals + rejections)` over the window. `null` when there were no decisions. |
| `approval_rate_by_kind` | the same ratio split per `ProposalKind` (`claim`, `page`, `entity`, `relation`). |
| `citation_coverage` | fraction of claims whose every `evidence` id resolves to a live Source or Evidence. |
| `citation_broken` | count of claims with at least one unresolved citation. |
| `stale_ratio` | `stale_claims / claims_active`, where a claim is stale if its freshness anchor (`last_confirmed_at`, else `updated_at`, else `created_at`) is older than `--stale-days` (default 180). Retired claims (superseded/archived/redacted) are exempt. |
| `proposal_lag_seconds` | latency from a proposal's `*.create` event to its matching `*.approve`, as `p50` / `p90` / `p99` / `mean` / `max` (nearest-rank percentiles, matching Prometheus `histogram_quantile`). |
| `actors` | top-N actors by total activity, each with `proposed` / `approved` / `rejected` / `confirmed` counts. |

A create event older than `--since` still pairs with an in-window approve, so
the left edge of the window does not systematically undercount lag.

## JSON schema (stable)

`--json` emits the following shape. Treat field renames or removals as
breaking; `schema_version` is bumped when that happens.

```json
{
  "schema_version": 1,
  "window": {
    "since": "2026-05-11T12:00:00+00:00",
    "until": null,
    "generated_at": "2026-06-10T12:00:00+00:00"
  },
  "review_gate": {
    "proposals_created": 4,
    "approvals": 3,
    "rejections": 1,
    "approval_rate": 0.75,
    "approval_rate_by_kind": { "claim": 0.6666666666666666, "page": 1.0 },
    "decisions_by_kind": {
      "claim": { "approve": 2, "reject": 1 },
      "page": { "approve": 1, "reject": 0 }
    },
    "pending_now": 0
  },
  "corpus": {
    "claims_total": 5,
    "claims_active": 4,
    "claims_cited": 4,
    "citation_coverage": 0.8,
    "citation_broken": 1,
    "stale_claims": 1,
    "stale_ratio": 0.25,
    "stale_after_days": 180,
    "claims_by_status": { "working": 4, "archived": 1 }
  },
  "proposal_lag_seconds": {
    "count": 3,
    "p50": 20.0,
    "p90": 30.0,
    "p99": 30.0,
    "mean": 20.0,
    "max": 30.0
  },
  "actors": [
    { "actor": "bob",   "proposed": 2, "approved": 2, "rejected": 0, "confirmed": 0, "total": 4 },
    { "actor": "alice", "proposed": 2, "approved": 1, "rejected": 1, "confirmed": 1, "total": 5 }
  ],
  "audit": { "events_total": 9, "events_in_window": 9 }
}
```

### `null` semantics

Ratios are `null`, **not** `0`, when their denominator is empty
(`approval_rate` with no decisions, `citation_coverage` with no claims,
`stale_ratio` with no active claims). This lets a consumer distinguish "no
data" from a genuine zero. The Prometheus exposition **omits** null gauges
entirely for the same reason — emitting `0` would lie about the denominator.

## Prometheus textfile collector

`--prometheus` emits gauges with `# HELP` / `# TYPE` headers, prefixed
`vouch_`. Wire it up with a cron sidecar writing to the node_exporter textfile
directory:

```bash
# /etc/cron.d/vouch-metrics — every 5 minutes
*/5 * * * *  app  cd /srv/project && vouch metrics --prometheus \
                    > /var/lib/node_exporter/textfile/vouch.prom.$$ \
                    && mv /var/lib/node_exporter/textfile/vouch.prom.$$ \
                          /var/lib/node_exporter/textfile/vouch.prom
```

(The temp-file-then-`mv` makes the write atomic so the collector never reads a
half-written file.)

Example exposition:

```text
# HELP vouch_approval_rate approve / (approve + reject) over window.
# TYPE vouch_approval_rate gauge
vouch_approval_rate 0.75
# HELP vouch_citation_coverage Fraction of claims fully cited.
# TYPE vouch_citation_coverage gauge
vouch_citation_coverage 0.8
# HELP vouch_claims_by_status Claim count per status.
# TYPE vouch_claims_by_status gauge
vouch_claims_by_status{status="archived"} 1
vouch_claims_by_status{status="working"} 4
```

## Out of scope

- **Pushing** to Prometheus/Datadog — use the `--json` or `--prometheus`
  output with a sidecar; `vouch metrics` never makes a network call.
- **Long-term retention / TSDB** — the audit log is the source of truth;
  windowing happens at read time.
