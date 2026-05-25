# Embedding-based search

vouch's primary search backend is embedding-based semantic retrieval,
backed by `sentence-transformers/all-mpnet-base-v2` (768-dim) by default.
FTS5 remains as the deterministic fallback when embeddings are unavailable
or return no hits.

## Install

```bash
pip install vouch[embeddings]
```

Alternative (no torch):

```bash
pip install vouch[embeddings-fast]
```

## Usage

```bash
# default: semantic primary, FTS5 fallback
vouch search "how do we authenticate users"

# force a specific backend
vouch search "auth" --backend embedding
vouch search "auth" --backend fts5
vouch search "auth" --backend hybrid --rerank --hyde --explain

# maintenance
vouch reindex --embeddings --backfill
vouch dedup --threshold 0.95
vouch embeddings stats
vouch eval embedding --queries eval/queries.jsonl
```

See `docs/superpowers/specs/2026-05-20-semantic-search-design.md`
for the full architecture.
