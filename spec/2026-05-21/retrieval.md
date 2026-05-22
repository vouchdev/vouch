# Retrieval

How `kb.search` and `kb.context` find things.

vouch ships with FTS5 (SQLite full-text) as the only retrieval backend
in 0.x. Vector embeddings are on the roadmap as an *additional*,
optional backend — they don't replace FTS5.

---

## 1. Storage

`.vouch/state.db` is a SQLite database with FTS5 enabled. It contains:

- `fts_claim(claim_id, text, tags, entities)` — content indexed,
  metadata columns prefixed `+` so they don't participate in ranking
- `fts_page(page_id, title, body, tags)` — title weighted higher
- `fts_entity(entity_id, name, aliases, description)`
- `fts_source(source_id, title, locator, content_preview)`

A `meta` table holds the schema version and the last full-rebuild
timestamp.

The database is **derived state**: deleting it loses no information.
`kb.index_rebuild` reproduces it from the YAML/markdown files on disk.
`vouch doctor` checks rebuild parity (counts must match files).

---

## 2. `kb.search`

```json
{
  "method": "kb.search",
  "params": {"query": "jwt", "limit": 10, "kinds": ["claim", "page"]}
}
```

### Tokenization

vouch uses FTS5 with the `porter` tokenizer when
`retrieval.fts5_porter: true` (default) and `unicode61` otherwise.

### Ranking

FTS5 BM25, weighted per kind:

- **Claim**: text=3.0, tags=1.5, entities=1.0
- **Page**: title=4.0, body=1.0, tags=2.0
- **Entity**: name=4.0, aliases=2.0, description=1.0
- **Source**: title=3.0, locator=1.0, content_preview=0.5

Scores from different kinds are normalized so they're comparable.

### Substring fallback

When `retrieval.backend: substring`, vouch skips FTS5 entirely and does
case-insensitive substring scoring. Slower, but useful for repos where
the SQLite extension isn't available or for testing without the index.

### Result shape

```json
[
  {"kind": "claim", "id": "auth-uses-jwt", "snippet": "…<mark>jwt</mark>…", "score": 1.42, "backend": "fts5"},
  ...
]
```

Snippets use FTS5 `snippet()` with `<mark>`/`</mark>` highlighting.

---

## 3. `kb.context`

Higher-level retrieval for filling an agent's working set. Builds a
`ContextPack` with quality metadata.

```json
{
  "method": "kb.context",
  "params": {
    "task": "implement password reset flow",
    "max_chars": 4000,
    "min_items": 3,
    "require_citations": true
  }
}
```

### How it works (0.x)

1. Extract a small set of keywords from `task` (stopword removal +
   noun-ish heuristic; no embedding call).
2. Run `kb.search` for the keywords across all kinds.
3. Sort by score, dedupe by id.
4. Walk down the result list, adding each item to the pack until
   `max_chars` budget is exhausted.
5. Each kept item carries `freshness` (`fresh` if confirmed within 30
   days; `stale` if `updated_at` > 180 days; `unknown` otherwise).
6. Build `ContextQuality`: how many items, how many uncited claims,
   whether the budget truncated, whether minimums were met.

### Quality gate

If `require_citations: true`, uncited claims are excluded from
`items` and listed in `quality.uncited_items`. If `min_items` isn't
met after filtering, `quality.ok: false` — the caller is expected to
back off and either widen the search or proceed with a documented
warning.

### Why not embeddings (yet)

- FTS5 is good enough for KBs in the 0-10k claim range, which is
  where vouch is positioned.
- Embeddings add a model dependency, a cold-start cost, and a vendor
  decision. Making this opt-in in 0.1 (see [ROADMAP.md](../ROADMAP.md))
  is the right ordering.
- When embeddings land, they'll be a *parallel* backend — the result
  shape stays the same, only `backend` shifts from `fts5` to
  `embedding`. Callers don't have to care.

---

## 4. Indexing semantics

- On every approved write, the corresponding FTS row is upserted
  synchronously in the same transaction as the file write. If the file
  write succeeds but the index update fails, the transaction is logged
  as a `data.index_dirty: true` flag on the audit event and `vouch
  doctor` will surface it.
- `kb.index_rebuild` truncates `fts_*` tables and re-walks the files.
- Pages with `status: archived` are still indexed but ranked lower
  (factor 0.5 by default).
