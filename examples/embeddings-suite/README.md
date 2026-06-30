# Embeddings: reindex, dedup-scan, eval, stats

The four embedding-extra methods, designed to skip gracefully. They are
the only `kb.*` methods that depend on the optional `[embeddings]` extra
(sentence-transformers + numpy), so each leg detects the extra and prints
an install hint instead of failing when it is absent.

- **`kb.reindex_embeddings`** backfills vectors for every artifact under
  the current model (`vouch reindex --embeddings` → `touched=N`).
- **`kb.dedup_scan`** finds cross-artifact near-duplicates above a cosine
  threshold (`vouch dedup --threshold 0.95`).
- **`kb.eval_embeddings`** scores retrieval quality (recall@k / mrr / ndcg)
  over a labeled JSONL query set
  (`vouch eval embedding --queries queries.jsonl --metric recall@10,mrr,ndcg`).
- **`kb.embeddings_stats`** reports model identity, per-kind counts, and
  cache hit rate (`vouch embeddings stats`).

## Run it:

```bash
./run.sh
# or against a specific build:
VOUCH=/path/to/vouch ./run.sh
```

The script builds a fresh KB in a temp dir, registers a source, and
approves three claims (two near-identical acme-example claims plus a
distinct alice-example one — so `dedup_scan` has a real near-duplicate
pair to find). It then probes for the `[embeddings]` extra:

- **extra present** → runs all four legs, in both CLI and JSONL-tool-server
  form, and prints the real output.
- **extra absent** → prints the exact commands it *would* run and exits 0.
  This is the load-bearing behavior: the embedding methods are optional, so
  the example degrades to a printed playbook rather than a hard failure.

## Steps

1. `vouch init` a fresh KB; `cd` in; `export VOUCH_AGENT=example-agent`.
2. Register a source, propose three claims, and approve them as a
   *different* actor (`VOUCH_AGENT=reviewer-example`) — vouch forbids
   self-approval, that is the review gate.
3. Write a labeled `queries.jsonl` (query + relevant ids) for the eval leg.
4. Guard: detect the extra via the interpreter backing `$VOUCH`
   (`python -c 'import sentence_transformers, numpy'`). When absent, print
   the intended commands and exit 0.
5. When present: `reindex --embeddings`, `dedup --threshold 0.95`,
   `eval embedding --queries … --metric recall@10,mrr,ndcg`,
   `embeddings stats` — then the same four over `serve --transport jsonl`.

## JSONL forms

The four methods over the JSONL tool server
(`"$VOUCH" serve --transport jsonl`, one request per line):

```jsonl
{"id":"r","method":"kb.reindex_embeddings","params":{"force":false}}
{"id":"d","method":"kb.dedup_scan","params":{"threshold":0.95,"dry_run":false}}
{"id":"e","method":"kb.eval_embeddings","params":{"queries_path":"/abs/queries.jsonl","k":10}}
{"id":"s","method":"kb.embeddings_stats","params":{}}
```

`kb.reindex_embeddings` returns `{"touched": N}`; `kb.dedup_scan` returns
`{"duplicates": [...]}`; `kb.eval_embeddings` returns the metric map;
`kb.embeddings_stats` returns `{"model", "counts", "query_cache"}`.

## Example output

Real run with the `[embeddings]` extra **not** installed (the graceful-skip
path):

```text
=== approve a few claims (so there is something to embed and dedup) ===
  Approved → claim/acme-example-deploys-the-api-service-via-blue-green-rollout
  Approved → claim/the-acme-example-api-service-ships-using-a-blue-green-rollou
  Approved → claim/alice-example-owns-the-on-call-rotation-for-the-billing-serv
  KB at /tmp/tmp.PV1m1fyYr0/.vouch
    durable: 4 claims  •  1 pages  •  2 sources  •  0 entities  •  0 relations
    pending: 0 proposals
    audit:   8 events  •  index: present

=== embeddings extra NOT installed — printing intended commands only ===
the [embeddings] extra (sentence-transformers + numpy) is not installed in
this environment, so the four vector methods would raise ImportError. install
it with:

    pip install 'vouch[embeddings]'
    ...
    vouch reindex --embeddings
    vouch dedup --threshold 0.95
    vouch eval embedding --queries queries.jsonl --metric recall@10,mrr,ndcg
    vouch embeddings stats

=== done (skipped: embeddings extra absent) ===
```

With the extra installed, the four `=== N/4 ... ===` sections run the real
commands and print `reindex: embeddings backfilled = N`, the near-duplicate
pair, the recall/mrr/ndcg scores, and the model/cache stats.

## Methods demonstrated

- `kb.reindex_embeddings`
- `kb.dedup_scan`
- `kb.eval_embeddings`
- `kb.embeddings_stats`
