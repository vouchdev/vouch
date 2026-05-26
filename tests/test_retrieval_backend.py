"""`context._retrieve` honors `retrieval.backend` in config.yaml (#92).

These tests monkeypatch `index_db.search_semantic` so they exercise the
dispatch logic without needing the optional embeddings extras (numpy /
sentence-transformers), and therefore run under the base CI install.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import context, health
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"e")
    s.put_claim(Claim(id="c1", text="JWT token rotation", evidence=[src.id]))
    health.rebuild_index(s)
    return s


def _set_backend(store: KBStore, backend: str) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg.setdefault("retrieval", {})["backend"] = backend
    store.config_path.write_text(yaml.safe_dump(cfg))


def _force_semantic_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the embedding path always return a hit, so a backend label of
    "embedding" appears iff `_retrieve` actually consulted semantic search."""
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "JWT token rotation", 0.99)],
    )


def _backends(pack: dict) -> set[str]:
    return {item["backend"] for item in pack["items"]}


def test_backend_fts5_skips_embedding(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #92: with retrieval.backend=fts5, the embedding path
    must not run even when it would return hits."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "fts5")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert "embedding" not in _backends(pack)
    assert _backends(pack) <= {"fts5", "substring"}


def test_backend_embedding_is_recognized(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`embedding` is an accepted value and forces the semantic path."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "embedding")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert _backends(pack) == {"embedding"}


def test_backend_substring_only(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "substring")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert _backends(pack) == {"substring"}


def test_backend_auto_prefers_embedding(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default `auto` tries embedding first when it returns hits."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "auto")
    pack = context.build_context_pack(store, query="JWT")
    assert any(item["backend"] == "embedding" for item in pack["items"])


def test_unset_backend_defaults_to_auto(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config with no retrieval.backend behaves like `auto`."""
    _force_semantic_hit(monkeypatch)
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg.get("retrieval", {}).pop("backend", None)
    store.config_path.write_text(yaml.safe_dump(cfg))
    pack = context.build_context_pack(store, query="JWT")
    assert any(item["backend"] == "embedding" for item in pack["items"])
