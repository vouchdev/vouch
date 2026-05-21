"""SQLite FTS5-backed search index at .vouch/state.db.

The index is rebuilt from the durable yaml/md files — the files are the
source of truth, the DB is a derived cache. That means losing state.db is
never fatal: `vouch index` rebuilds it from disk.

FTS5 gives proper tokenisation, prefix queries, and BM25 ranking without
pulling in an embedding stack. Vector search can be layered later as a
second `backend` in the ContextItem response.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager, suppress
from pathlib import Path

DB_FILENAME = "state.db"


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    id UNINDEXED, text, type UNINDEXED, status UNINDEXED, tags
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    id UNINDEXED, title, body, type UNINDEXED, tags
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    id UNINDEXED, name, description, type UNINDEXED, aliases
);

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_index (
    kind            TEXT NOT NULL,
    id              TEXT NOT NULL,
    vec             BLOB NOT NULL,
    content_hash    TEXT NOT NULL,
    model           TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);

CREATE INDEX IF NOT EXISTS embedding_index_kind ON embedding_index(kind);

CREATE TABLE IF NOT EXISTS query_embedding_cache (
    query_hash      TEXT PRIMARY KEY,
    vec             BLOB NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 1,
    last_used_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_dupes (
    kind            TEXT NOT NULL,
    id              TEXT NOT NULL,
    near_id         TEXT NOT NULL,
    cosine          REAL NOT NULL,
    detected_at     TEXT NOT NULL
);
"""


def _db_path(kb_dir: Path) -> Path:
    return kb_dir / DB_FILENAME


@contextmanager
def open_db(kb_dir: Path):
    """Yield a sqlite connection with schema applied. Always commits on exit."""
    path = _db_path(kb_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def reset(kb_dir: Path) -> None:
    """Drop everything; the rebuild caller re-populates.

    Clears every derived table — FTS indexes, embedding vectors, the
    query embedding cache, dedup detections, and embedding metadata.
    `vouch index` rebuilds the FTS rows from disk; the embedding write
    hook backfills `embedding_index` and `index_meta` as artifacts are
    re-read. Leaving stale rows here means semantic search can return
    orphaned hits after a reindex.
    """
    with open_db(kb_dir) as conn:
        conn.executescript(
            "DELETE FROM claims_fts;"
            "DELETE FROM pages_fts;"
            "DELETE FROM entities_fts;"
            "DELETE FROM embedding_index;"
            "DELETE FROM query_embedding_cache;"
            "DELETE FROM embedding_dupes;"
            "DELETE FROM index_meta WHERE key LIKE 'embedding_%';"
        )


def index_claim(conn: sqlite3.Connection, *, id: str, text: str,
                type: str, status: str, tags: Iterable[str]) -> None:
    conn.execute("DELETE FROM claims_fts WHERE id = ?", (id,))
    conn.execute(
        "INSERT INTO claims_fts (id, text, type, status, tags) VALUES (?, ?, ?, ?, ?)",
        (id, text, type, status, " ".join(tags)),
    )


def index_page(conn: sqlite3.Connection, *, id: str, title: str, body: str,
               type: str, tags: Iterable[str]) -> None:
    conn.execute("DELETE FROM pages_fts WHERE id = ?", (id,))
    conn.execute(
        "INSERT INTO pages_fts (id, title, body, type, tags) VALUES (?, ?, ?, ?, ?)",
        (id, title, body, type, " ".join(tags)),
    )


def index_entity(conn: sqlite3.Connection, *, id: str, name: str,
                 description: str | None, type: str, aliases: Iterable[str]) -> None:
    conn.execute("DELETE FROM entities_fts WHERE id = ?", (id,))
    conn.execute(
        "INSERT INTO entities_fts (id, name, description, type, aliases) VALUES (?, ?, ?, ?, ?)",
        (id, name, description or "", type, " ".join(aliases)),
    )


# --- search ---------------------------------------------------------------


def _quote_match(query: str) -> str:
    """Safely build an FTS5 MATCH expression.

    FTS5 has its own little query language with special characters (quotes,
    parens, dashes, AND/OR/NOT). Easiest safe-by-default path: wrap each
    token in double quotes and OR them. Trades precision for never crashing
    on user-supplied queries.
    """
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def search(kb_dir: Path, query: str, *, limit: int = 10
           ) -> list[tuple[str, str, str, float]]:
    """Return (kind, id, snippet, score). Score = -bm25 (so higher = better)."""
    q = _quote_match(query)
    out: list[tuple[str, str, str, float]] = []
    with open_db(kb_dir) as conn:
        cur = conn.execute(
            "SELECT id, snippet(claims_fts, 1, '«', '»', '…', 16), bm25(claims_fts) "
            "FROM claims_fts WHERE claims_fts MATCH ? ORDER BY bm25(claims_fts) LIMIT ?",
            (q, limit),
        )
        for row_id, snip, score in cur.fetchall():
            out.append(("claim", row_id, snip, -float(score)))
        cur = conn.execute(
            "SELECT id, title, bm25(pages_fts) FROM pages_fts "
            "WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ?",
            (q, limit),
        )
        for row_id, title, score in cur.fetchall():
            out.append(("page", row_id, title, -float(score)))
        cur = conn.execute(
            "SELECT id, name, bm25(entities_fts) FROM entities_fts "
            "WHERE entities_fts MATCH ? ORDER BY bm25(entities_fts) LIMIT ?",
            (q, limit),
        )
        for row_id, name, score in cur.fetchall():
            out.append(("entity", row_id, name, -float(score)))
    out.sort(key=lambda h: h[3], reverse=True)
    return out[:limit]


def stats(kb_dir: Path) -> dict[str, int]:
    """Counts in the index — used by `vouch status` and `kb_status`."""
    with open_db(kb_dir) as conn:
        return {
            "claims": conn.execute("SELECT COUNT(*) FROM claims_fts").fetchone()[0],
            "pages": conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0],
            "entities": conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0],
        }


# --- embeddings storage --------------------------------------------------


def _vec_to_blob(vec):  # type: ignore[no-untyped-def]
    import numpy as np
    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes, dim: int):  # type: ignore[no-untyped-def]
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32, count=dim).copy()


def put_embedding(
    conn: sqlite3.Connection, *,
    kind: str, id: str,
    vec,  # type: ignore[no-untyped-def]
    content_hash: str,
    model: str, model_version: str, dim: int,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO embedding_index "
        "(kind, id, vec, content_hash, model, model_version, dim, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind, id, _vec_to_blob(vec), content_hash,
            model, model_version, dim,
            _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        ),
    )


def get_embedding(kb_dir: Path, *, kind: str, id: str):  # type: ignore[no-untyped-def]
    """Return (vec, content_hash, model) or None."""
    with open_db(kb_dir) as conn:
        row = conn.execute(
            "SELECT vec, content_hash, model, dim FROM embedding_index "
            "WHERE kind = ? AND id = ?",
            (kind, id),
        ).fetchone()
    if not row:
        return None
    blob, ch, model, dim = row
    return _blob_to_vec(blob, dim), ch, model


def set_embedding_meta(kb_dir: Path, *, model: str, version: str, dim: int) -> None:
    with open_db(kb_dir) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            [
                ("embedding_model", model),
                ("embedding_model_version", version),
                ("embedding_dim", str(dim)),
            ],
        )


def get_embedding_meta(kb_dir: Path) -> dict[str, str]:
    with open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT key, value FROM index_meta WHERE key LIKE 'embedding_%'"
        ).fetchall()
    return {k: v for k, v in rows}


_sqlite_vec_loaded: set[int] = set()


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the sqlite-vec extension."""
    if id(conn) in _sqlite_vec_loaded:
        return True
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.OperationalError):
        return False
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        sqlite_vec.load(conn)
    except sqlite3.OperationalError:
        return False
    finally:
        with suppress(sqlite3.OperationalError):
            conn.enable_load_extension(False)
    _sqlite_vec_loaded.add(id(conn))
    return True


def search_embedding(
    kb_dir: Path,
    *,
    query_vec,  # type: ignore[no-untyped-def]
    kinds: tuple[str, ...] = (
        "claim", "page", "source", "entity", "relation", "evidence",
    ),
    limit: int = 10,
    min_score: float = 0.0,
) -> list[tuple[str, str, str, float]]:
    import numpy as np
    q = np.asarray(query_vec, dtype=np.float32)
    qnorm = float(np.linalg.norm(q))
    if qnorm > 0:
        q = q / qnorm
    placeholders = ",".join("?" for _ in kinds)
    with open_db(kb_dir) as conn:
        have_vec = _load_sqlite_vec(conn)
        if have_vec:
            try:
                # SQLite doesn't allow a column alias to be referenced in the
                # WHERE clause of the same SELECT, so the min_score filter
                # goes in an outer query against the inner alias.
                rows = conn.execute(
                    f"SELECT kind, id, score FROM ("
                    f"  SELECT kind, id, 1.0 - vec_distance_cosine(vec, ?) AS score "
                    f"  FROM embedding_index "
                    f"  WHERE kind IN ({placeholders})"
                    f") WHERE score >= ? "
                    f"ORDER BY score DESC LIMIT ?",
                    (q.tobytes(), *kinds, min_score, limit),
                ).fetchall()
                return [(k, i, "", float(s)) for k, i, s in rows]
            except sqlite3.OperationalError:
                pass
        rows = conn.execute(
            f"SELECT kind, id, vec, dim FROM embedding_index "
            f"WHERE kind IN ({placeholders})",
            kinds,
        ).fetchall()
    scored: list[tuple[str, str, str, float]] = []
    for kind, id_, blob, dim in rows:
        vec = _blob_to_vec(blob, dim)
        # Cosine similarity: q is already unit-normalized above; normalize
        # the stored vec here too so rankings match the sqlite-vec path
        # (which uses `1 - vec_distance_cosine`, i.e. true cosine).
        vnorm = float(np.linalg.norm(vec))
        if vnorm == 0:
            continue
        score = float(q @ vec) / vnorm
        if score >= min_score:
            scored.append((kind, id_, "", score))
    scored.sort(key=lambda r: r[3], reverse=True)
    return scored[:limit]


def search_semantic(
    kb_dir: Path,
    query: str,
    *,
    limit: int = 10,
    kinds: tuple[str, ...] = (
        "claim", "page", "source", "entity", "relation", "evidence",
    ),
    min_score: float = 0.0,
) -> list[tuple[str, str, str, float]]:
    """Encode query (cached) -> ANN/cosine search."""
    try:
        from .embeddings import get_embedder
        from .embeddings.cache import cache_query_vec, lookup_query_vec
    except ImportError:
        return []
    try:
        embedder = get_embedder()
    except KeyError:
        return []
    qvec = lookup_query_vec(kb_dir, query=query)
    # The query cache is keyed by text only; if the embedder model or
    # vector dimension has changed since the entry was cached, the stored
    # vector lives in a different space than the indexed document
    # embeddings. Drop the stale entry and re-encode in that case.
    if qvec is not None:
        cached_dim = int(getattr(qvec, "shape", [0])[0] or 0)
        if cached_dim != embedder.dim:
            qvec = None
    if qvec is None:
        qvec = embedder.encode(query)
        cache_query_vec(kb_dir, query=query, vec=qvec)
    return search_embedding(
        kb_dir, query_vec=qvec, kinds=kinds, limit=limit, min_score=min_score,
    )
