"""Deterministic sync / merge workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import bundle, sync
from vouch.cli import cli
from vouch.models import Claim
from vouch.storage import KBStore


def _store(root: Path) -> KBStore:
    return KBStore.init(root)


def _claim(store: KBStore, claim_id: str, text: str) -> None:
    src = store.put_source(b"shared evidence", title="evidence")
    store.put_claim(Claim(id=claim_id, text=text, evidence=[src.id]))


def test_sync_check_and_apply_from_kb_directory(tmp_path: Path) -> None:
    incoming = _store(tmp_path / "incoming")
    _claim(incoming, "c1", "alpha")
    dest = _store(tmp_path / "dest")

    report = sync.sync_check(dest.kb_dir, incoming.root)

    assert report.ok
    assert report.source_type == "kb"
    assert "claims/c1.yaml" in report.new_files
    assert not report.conflicts

    result = sync.sync_apply(dest.kb_dir, incoming.root, actor="tester")

    assert "claims/c1.yaml" in result["written"]
    assert dest.get_claim("c1").text == "alpha"


def test_sync_excludes_config_yaml(tmp_path: Path) -> None:
    incoming = _store(tmp_path / "incoming")
    incoming.config_path.write_text("version: 1\nsync: incoming\n")
    _claim(incoming, "c1", "alpha")
    dest = _store(tmp_path / "dest")
    dest.config_path.write_text("version: 1\nsync: local\n")

    report = sync.sync_check(dest.kb_dir, incoming.root)

    assert "config.yaml" not in report.new_files
    assert "config.yaml" not in report.identical
    assert not any(c.path == "config.yaml" for c in report.conflicts)

    sync.sync_apply(dest.kb_dir, incoming.root)
    assert dest.config_path.read_text() == "version: 1\nsync: local\n"


def test_sync_check_classifies_claim_conflicts(tmp_path: Path) -> None:
    incoming = _store(tmp_path / "incoming")
    _claim(incoming, "c1", "incoming text")
    dest = _store(tmp_path / "dest")
    _claim(dest, "c1", "local text")

    report = sync.sync_check(dest.kb_dir, incoming.root)

    assert report.ok
    assert any(c.path == "claims/c1.yaml" for c in report.conflicts)
    assert any(
        c.kind == "claim" and c.artifact_id == "c1"
        for c in report.semantic_conflicts
    )


def test_sync_apply_fails_on_conflicts_by_default(tmp_path: Path) -> None:
    incoming = _store(tmp_path / "incoming")
    _claim(incoming, "c1", "incoming text")
    dest = _store(tmp_path / "dest")
    _claim(dest, "c1", "local text")

    with pytest.raises(RuntimeError, match="conflicts"):
        sync.sync_apply(dest.kb_dir, incoming.root)

    assert dest.get_claim("c1").text == "local text"


def test_sync_apply_propose_writes_conflict_report(tmp_path: Path) -> None:
    incoming = _store(tmp_path / "incoming")
    _claim(incoming, "c1", "incoming text")
    dest = _store(tmp_path / "dest")
    _claim(dest, "c1", "local text")

    result = sync.sync_apply(dest.kb_dir, incoming.root, on_conflict="propose")

    assert dest.get_claim("c1").text == "local text"
    assert "claims/c1.yaml" in result["skipped_conflicts"]
    report_path = dest.kb_dir / result["conflict_report"]
    report = json.loads(report_path.read_text())
    assert any(c["path"] == "claims/c1.yaml" for c in report["conflicts"])


def test_sync_check_accepts_bundle_source(tmp_path: Path) -> None:
    incoming = _store(tmp_path / "incoming")
    _claim(incoming, "c1", "alpha")
    bundle_path = tmp_path / "incoming.tar.gz"
    bundle.export(incoming.kb_dir, dest=bundle_path)
    dest = _store(tmp_path / "dest")

    report = sync.sync_check(dest.kb_dir, bundle_path)

    assert report.ok
    assert report.source_type == "bundle"
    assert "claims/c1.yaml" in report.new_files


def test_sync_check_cli_outputs_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = _store(tmp_path / "incoming")
    _claim(incoming, "c1", "alpha")
    dest = _store(tmp_path / "dest")
    monkeypatch.chdir(dest.root)

    result = CliRunner().invoke(cli, ["sync-check", str(incoming.root)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_type"] == "kb"
    assert "claims/c1.yaml" in payload["new_files"]
