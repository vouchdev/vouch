# Retrieval — for users

How `kb.search` and `kb.context` find what's relevant.

For storage internals, see [../spec/retrieval.md](../spec/retrieval.md).
For *why* we picked FTS5 over embeddings (today), see
[../ROADMAP.md](../ROADMAP.md).

## `kb.search` — keyword

```bash
vouch search "jwt rotation"
# claim   refresh-tokens-rotate    0.82  Refresh tokens rotate on every use;…
# page    auth-design              0.61  Auth design
# source  9b1ac6d4…                0.43  Internal RFC 12 — Token rotation policy
```

What's happening:

- The query is tokenised (Porter stemmer by default — so "rotate"
  and "rotation" match).
- FTS5 BM25 ranks results, weighted per kind (claim text > tags;
  page title > body).
- Snippets highlight matched terms with `<mark>…</mark>` when used
  programmatically.

Useful flags:

```bash
vouch search jwt --limit 20
vouch search jwt --kinds claim,page
vouch search jwt --json     # for piping
```

## `kb.context` — task-shaped

When an agent says "before I touch the auth code, what does the KB
know?", `kb.search` returns a list but `kb.context` returns a
*bundle* ready to drop into a prompt.

```bash
vouch context "implement password reset" \
  --max-chars 4000 \
  --min-items 3 \
  --require-citations
```

What you get back:

```json
{
  "query": "implement password reset",
  "items": [
    {"id": "auth-uses-jwt", "type": "claim", "summary": "...", "score": 1.2, "freshness": "fresh"},
    ...
  ],
  "quality": {
    "ok": true,
    "items": 4,
    "uncited_items": [],
    "budget_truncated": false
  }
}
```

The `quality` block is the new part. It tells you (or the agent)
*whether to trust the bundle*: were minimums met, did citations
exist, did the byte budget force truncation?

If `--require-citations` is set, uncited claims are filtered out — the
agent gets a smaller pack with the promise that every item has at
least one source.

## Freshness

Each returned item gets a freshness label:

- `fresh` — confirmed within 30 days
- `stale` — `updated_at` is older than 180 days *and* never
  re-confirmed
- `unknown` — anything in between

You can run `vouch confirm <claim-id>` after re-using a claim to bump
its freshness. Or set `kb.confirm` as a habit in your agent's prompt.

## Why FTS5, not embeddings (yet)

The honest answer: keyword search is enough for the KB sizes vouch is
positioned for, and embeddings are a model dependency we don't want to
require by default.

When embeddings land (planned 0.1, see [ROADMAP](../ROADMAP.md)),
they'll be a parallel backend — the API stays the same, only the
`backend` field in results changes from `fts5` to `embedding`. Callers
won't need to care.

## Reindexing

`state.db` is derived. If you bulk-loaded files outside the CLI,
rebuild:

```bash
vouch index
# rebuilt: 142 claims, 12 pages, 7 entities, 4 relations, 9 sources
```

`vouch doctor` will tell you if the index is out of sync with files.

## Tips for getting better search

- **Tag aggressively.** Tags weight more than body text. A claim
  tagged `[auth, security]` finds itself when an agent searches
  "security".
- **Cite specific evidence.** Claims with attached Evidence (line
  ranges, quotes) rank better than claims that point at a whole
  source, because Evidence quotes become indexed text.
- **Use Entities.** Once you have entities, search "postgres" or
  "billing-service" hits both via the entity name and via every
  claim that lists the entity.

## Semantic retrieval

See [embeddings.md](./embeddings.md).
