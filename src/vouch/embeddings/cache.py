"""Query embedding LRU cache backed by `query_embedding_cache` table."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from .. import index_db


def _query_key(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def cache_query_vec(
    kb_dir: Path, *, query: str, vec: np.ndarray, max_entries: int = 1024,
) -> None:
    h = _query_key(query)
    blob = np.asarray(vec, dtype=np.float32).tobytes()
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with index_db.open_db(kb_dir) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO query_embedding_cache "
            "(query_hash, vec, hit_count, last_used_at) "
            "VALUES (?, ?, COALESCE("
            "  (SELECT hit_count FROM query_embedding_cache WHERE query_hash=?), 0"
            ") + 1, ?)",
            (h, blob, h, now),
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM query_embedding_cache"
        ).fetchone()[0]
        if n > max_entries:
            conn.execute(
                "DELETE FROM query_embedding_cache WHERE query_hash IN ("
                " SELECT query_hash FROM query_embedding_cache "
                " ORDER BY last_used_at ASC LIMIT ?)",
                (n - max_entries,),
            )


def lookup_query_vec(kb_dir: Path, *, query: str) -> np.ndarray | None:
    h = _query_key(query)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with index_db.open_db(kb_dir) as conn:
        row = conn.execute(
            "SELECT vec FROM query_embedding_cache WHERE query_hash = ?",
            (h,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE query_embedding_cache SET hit_count = hit_count + 1, "
            "last_used_at = ? WHERE query_hash = ?",
            (now, h),
        )
    return np.frombuffer(row[0], dtype=np.float32).copy()


def query_cache_size(kb_dir: Path) -> int:
    with index_db.open_db(kb_dir) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM query_embedding_cache"
        ).fetchone()[0])


def query_cache_clear(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        conn.execute("DELETE FROM query_embedding_cache")


def query_cache_stats(kb_dir: Path) -> dict[str, Any]:
    with index_db.open_db(kb_dir) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), "
            "COALESCE(MAX(hit_count), 0) FROM query_embedding_cache"
        ).fetchone()
    return {"entries": int(row[0]), "hits": int(row[1]),
            "max_hits_per_entry": int(row[2])}
