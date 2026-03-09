"""SQLite FTS5-backed search index at .vouch/state.db.

The index is rebuilt from the durable yaml/md files — the files are the
source of truth, the DB is a derived cache. That means losing state.db is
never fatal: `vouch index` rebuilds it from disk.

FTS5 gives proper tokenisation, prefix queries, and BM25 ranking without
pulling in an embedding stack. Vector search can be layered later as a
second `backend` in the ContextItem response.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

DB_FILENAME = "state.db"


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    id UNINDEXED,
    text,
    type UNINDEXED,
    status UNINDEXED,
    tags
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    id UNINDEXED,
    title,
    body,
    type UNINDEXED,
    tags
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    id UNINDEXED,
    name,
    description,
    type UNINDEXED,
    aliases
);

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    """Drop everything; the rebuild caller re-populates."""
    with open_db(kb_dir) as conn:
        conn.executescript(
            "DELETE FROM claims_fts; DELETE FROM pages_fts; DELETE FROM entities_fts;"
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
