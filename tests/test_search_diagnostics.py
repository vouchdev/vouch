"""Search diagnostics for retrieval misses."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from vouch import capabilities, health
from vouch.cli import cli
from vouch.jsonl_server import HANDLERS, handle_request
from vouch.models import Claim
from vouch.search_diagnostics import diagnose_search
from vouch.storage import KBStore


def test_diagnose_search_reports_found_target(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="jwt-claim", text="JWT tokens secure the API", evidence=[src.id]))
    health.rebuild_index(store)

    report = diagnose_search(
        store,
        query="JWT tokens",
        target_kind="claim",
        target_id="jwt-claim",
        backend="fts5",
    )

    assert report["target"]["exists"] is True
    assert report["target"]["indexed"] is True
    assert report["target"]["found"] is True
    assert report["target"]["scoped_rank"] == 1
    assert report["reasons"] == ["target is present in scoped results"]


def test_diagnose_search_reports_unindexed_target(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="jwt-claim", text="JWT tokens secure the API", evidence=[src.id]))

    report = diagnose_search(
        store,
        query="JWT tokens",
        target_kind="claim",
        target_id="jwt-claim",
        backend="fts5",
    )

    assert report["target"]["exists"] is True
    assert report["target"]["indexed"] is False
    assert report["target"]["found"] is False
    assert report["reasons"] == ["target artifact is not present in the derived index"]


def test_search_diagnose_cli_outputs_json(tmp_path: Path, monkeypatch) -> None:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="jwt-claim", text="JWT tokens secure the API", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    result = CliRunner().invoke(
        cli,
        [
            "search-diagnose",
            "JWT tokens",
            "--target-kind",
            "claim",
            "--target-id",
            "jwt-claim",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"]["found"] is True


def test_jsonl_search_diagnose_handler(tmp_path: Path, monkeypatch) -> None:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"e")
    store.put_claim(Claim(id="jwt-claim", text="JWT tokens secure the API", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    resp = handle_request({
        "id": "diag-1",
        "method": "kb.search_diagnose",
        "params": {
            "query": "JWT tokens",
            "target_kind": "claim",
            "target_id": "jwt-claim",
        },
    })

    assert resp["ok"] is True
    assert resp["result"]["target"]["found"] is True
    assert "kb.search_diagnose" in HANDLERS
    assert "kb.search_diagnose" in capabilities.capabilities().methods
