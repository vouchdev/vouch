"""CLI flag surface for embeddings commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.embeddings._fakes import MockEmbedder
from vouch.cli import cli
from vouch.embeddings import register
from vouch.embeddings.base import DEFAULT_MODEL_NAME
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _register_default() -> None:
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="some text", evidence=[src.id]))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_search_semantic_flag(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "some text", "--semantic"])
    assert result.exit_code == 0
    assert "c1" in result.output


def test_search_backend_flag(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "some text", "--backend", "embedding"])
    assert result.exit_code == 0


def test_search_top_k_flag(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["search", "x", "--top-k", "3"])
    assert result.exit_code == 0


def test_embeddings_stats(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["embeddings", "stats"])
    assert result.exit_code == 0
    assert "model" in result.output.lower() or "claim" in result.output.lower()


def test_eval_embedding_outputs_metrics(kb: Path, tmp_path: Path) -> None:
    import json as _json
    qfile = tmp_path / "queries.jsonl"
    qfile.write_text(_json.dumps({"query": "some text", "relevant": ["claim:c1"]}) + "\n")
    runner = CliRunner()
    result = runner.invoke(cli, [
        "eval", "embedding",
        "--queries", str(qfile),
        "--metric", "recall@10,mrr,ndcg",
    ])
    assert result.exit_code == 0
    assert "recall" in result.output.lower() or "mrr" in result.output.lower()


def test_dedup_scan_lists_duplicates(kb: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["dedup", "--threshold", "0.5", "--dry-run"])
    assert result.exit_code == 0


def test_reindex_embeddings_backfills(kb: Path) -> None:
    from vouch import index_db
    from vouch.storage import KBStore, discover_root
    store = KBStore(discover_root(kb))
    with index_db.open_db(store.kb_dir) as conn:
        conn.execute("DELETE FROM embedding_index")
    runner = CliRunner()
    result = runner.invoke(cli, ["reindex", "--embeddings", "--backfill"])
    assert result.exit_code == 0
    assert index_db.get_embedding(store.kb_dir, kind="claim", id="c1") is not None
