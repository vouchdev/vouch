"""Ingest-time duplicate detection via embedding cosine similarity."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np

from .. import index_db

DEFAULT_THRESHOLD = 0.95


def check_and_log(
    kb_dir: Path, *,
    kind: str, id: str, vec: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[str, float] | None:
    """Find nearest-neighbor cosine >= threshold; log if found."""
    candidates = index_db.search_embedding(
        kb_dir, query_vec=vec, kinds=(kind,), limit=2,
    )
    for k, near_id, _snip, cos in candidates:
        if k != kind or near_id == id:
            continue
        if cos >= threshold:
            _log(kb_dir, kind=kind, id=id, near_id=near_id, cosine=cos)
            return near_id, cos
    return None


def _log(kb_dir: Path, *, kind: str, id: str, near_id: str, cosine: float) -> None:
    with index_db.open_db(kb_dir) as conn:
        conn.execute(
            "INSERT INTO embedding_dupes (kind, id, near_id, cosine, detected_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, id, near_id, float(cosine),
             dt.datetime.now(dt.UTC).isoformat(timespec="seconds")),
        )


def list_duplicates(kb_dir: Path) -> list[dict[str, Any]]:
    with index_db.open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT kind, id, near_id, cosine, detected_at FROM embedding_dupes "
            "ORDER BY detected_at DESC"
        ).fetchall()
    return [
        {"kind": k, "id": i, "near_id": n, "cosine": float(c), "detected_at": d}
        for k, i, n, c, d in rows
    ]


def scan_all(
    kb_dir: Path, *, threshold: float = DEFAULT_THRESHOLD, dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Cross-artifact scan within same kind. For ad-hoc audit."""
    found: list[dict[str, Any]] = []
    with index_db.open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT kind, id, vec, dim FROM embedding_index"
        ).fetchall()
    if not rows:
        return found
    vecs: dict[tuple[str, str], np.ndarray] = {}
    for kind, id_, blob, dim in rows:
        vecs[(kind, id_)] = np.frombuffer(blob, dtype=np.float32, count=dim).copy()
    seen: set[frozenset[tuple[str, str]]] = set()
    keys = list(vecs.keys())
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            if k1[0] != k2[0]:
                continue
            if vecs[k1].shape != vecs[k2].shape:
                # Different embedding dims (e.g. mid-migration) aren't comparable.
                continue
            cos = float(vecs[k1] @ vecs[k2])
            if cos >= threshold:
                pair = frozenset([k1, k2])
                if pair in seen:
                    continue
                seen.add(pair)
                if not dry_run:
                    _log(kb_dir, kind=k1[0], id=k2[1], near_id=k1[1], cosine=cos)
                found.append({
                    "kind": k1[0], "id": k2[1], "near_id": k1[1], "cosine": cos,
                })
    return found
