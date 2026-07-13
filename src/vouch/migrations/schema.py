"""The on-disk schema-version stamp (``.vouch/schema_version``).

A single-line semver. Absent means "treat as the baseline" so KBs created before
this feature keep loading until their first migrate.
"""

from __future__ import annotations

from pathlib import Path

from ..storage import SCHEMA_VERSION as _CURRENT
from ..storage import SCHEMA_VERSION_FILENAME, KBStore
from . import semver
from .rewriter import atomic_write_text

#: KBs with no `schema_version` file are treated as this version.
BASELINE_SCHEMA_VERSION = _CURRENT


def schema_version_path(store: KBStore) -> Path:
    return store.kb_dir / SCHEMA_VERSION_FILENAME


def read_schema_version(store: KBStore) -> str:
    p = schema_version_path(store)
    if not p.exists():
        return BASELINE_SCHEMA_VERSION
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return BASELINE_SCHEMA_VERSION
    semver.parse(raw)  # validate; raises ValueError on garbage
    return raw


def write_schema_version(store: KBStore, version: str) -> None:
    semver.parse(version)
    atomic_write_text(schema_version_path(store), version + "\n")
