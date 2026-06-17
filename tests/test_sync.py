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


# --- graph integrity on the sync path ------------------------------------
#
# Sync walks the same `bundle._validate_content` per-file pass as
# import, then also calls `bundle._check_graph_integrity` against the
# destination KB. So a directory-or-bundle source whose Relation /
# Page references resolve to nothing locally or in the source itself
# must be rejected by both sync_check and sync_apply.


def _hand_write_dangling_relation_kb(root: Path) -> Path:
    """Build a `.vouch/` directory whose only relation has dangling
    endpoints. Bypasses `put_relation` (which would now reject it) by
    writing the YAML directly, simulating an attacker-supplied source."""
    kb = _store(root)
    rel_yaml = (
        "id: r-dangling\n"
        "source: ghost-source\n"
        "relation: uses\n"
        "target: ghost-target\n"
        "confidence: 0.7\n"
        "evidence: []\n"
        "created_at: '2026-05-27T00:00:00+00:00'\n"
        "updated_at: '2026-05-27T00:00:00+00:00'\n"
    )
    (kb.kb_dir / "relations" / "r-dangling.yaml").write_text(rel_yaml)
    return kb.root


def test_sync_check_rejects_dangling_relation_from_directory_source(
    tmp_path: Path,
) -> None:
    src_root = _hand_write_dangling_relation_kb(tmp_path / "incoming")
    dest = _store(tmp_path / "dest")
    report = sync.sync_check(dest.kb_dir, src_root)
    assert not report.ok
    assert any("dangling reference" in i for i in report.issues), report.issues


def test_sync_apply_refuses_dangling_relation(tmp_path: Path) -> None:
    src_root = _hand_write_dangling_relation_kb(tmp_path / "incoming")
    dest = _store(tmp_path / "dest")
    with pytest.raises(RuntimeError, match="dangling reference"):
        sync.sync_apply(dest.kb_dir, src_root)
    assert not (dest.kb_dir / "relations" / "r-dangling.yaml").exists()


def test_sync_rejects_source_content_address_mismatch(tmp_path: Path) -> None:
    """Sync walks the same per-file validation as bundle import, so a
    source directory whose content does not hash to its name must be
    rejected by sync_check and refused by sync_apply. The dangling source
    is hand-written (bypassing put_source, which would recompute the id)
    to model an attacker-supplied or corrupted sync source."""
    import yaml

    incoming = _store(tmp_path / "incoming")
    dir_id = "a" * 64  # not the hash of the content below
    sdir = incoming.kb_dir / "sources" / dir_id
    sdir.mkdir(parents=True)
    (sdir / "content").write_bytes(b"attacker-controlled bytes")
    (sdir / "meta.yaml").write_text(yaml.safe_dump({
        "id": dir_id, "type": "file", "locator": "x.txt", "title": "t",
        "hash": dir_id, "immutable": True, "scope": "project",
        "byte_size": 25, "media_type": "text/plain",
        "created_at": "2026-05-27T00:00:00+00:00", "metadata": {}, "tags": [],
    }, sort_keys=False))

    dest = _store(tmp_path / "dest")
    report = sync.sync_check(dest.kb_dir, incoming.root)
    assert not report.ok
    assert any("content-address mismatch" in i for i in report.issues), report.issues

    with pytest.raises(RuntimeError, match="content-address mismatch"):
        sync.sync_apply(dest.kb_dir, incoming.root)
    assert not (dest.kb_dir / "sources" / dir_id / "content").exists()
