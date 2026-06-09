"""Migration engine tests — round-trip, rollback, dry-run, transform factories."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.migration import (
    MigrateResult,
    Migration,
    _chain,
    add_default,
    migrate_kb,
    rename_field,
)
from vouch.models import VOUCH_SCHEMA_VERSION
from vouch.storage import CONFIG_FILENAME, KB_DIRNAME, KBStore, _yaml_dump, _yaml_load

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(kb_dir: Path, schema_version: str) -> None:
    cfg_path = kb_dir / CONFIG_FILENAME
    cfg = _yaml_load(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg["schema_version"] = schema_version
    cfg_path.write_text(_yaml_dump(cfg))


def _seed_kb_with_claim(
    tmp_path: Path,
    claim_id: str = "test-claim",
    **claim_extra,
) -> tuple[Path, str]:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"evidence")
    raw = _claim_yaml(claim_id, evidence=[src.id], **claim_extra)
    (store.kb_dir / "claims" / f"{claim_id}.yaml").write_text(_yaml_dump(raw))
    return store.kb_dir, src.id


def _claim_yaml(claim_id: str, **extra) -> dict:
    base = {
        "id": claim_id,
        "text": "A test claim.",
        "type": "observation",
        "status": "working",
        "confidence": 0.7,
        "evidence": [],
        "entities": [],
        "supersedes": [],
        "superseded_by": None,
        "contradicts": [],
        "scope": "project",
        "tags": [],
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "last_confirmed_at": None,
        "approved_by": None,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# _chain() resolution
# ---------------------------------------------------------------------------


def test_chain_same_version_returns_empty():
    assert _chain("0.1", "0.1") == []


def test_chain_unknown_version_raises():
    with pytest.raises(ValueError, match="Check that your vouch installation is up to date"):
        _chain("0.1", "99.0")


# ---------------------------------------------------------------------------
# No-op migration (from == to)
# ---------------------------------------------------------------------------


def test_migrate_no_op(tmp_path: Path):
    KBStore.init(tmp_path)
    result = migrate_kb(tmp_path, from_v=VOUCH_SCHEMA_VERSION, to_v=VOUCH_SCHEMA_VERSION)
    assert isinstance(result, MigrateResult)
    assert result.changed == []
    assert result.dry_run is False


# ---------------------------------------------------------------------------
# rename_field transform factory
# ---------------------------------------------------------------------------


def test_rename_field_transform():
    from vouch.migration import rename_field
    t = rename_field("claims", old="old_name", new="new_name")
    raw = {"old_name": "value", "other": 1}
    result = t(raw, "claims")
    assert "new_name" in result
    assert "old_name" not in result
    assert result["other"] == 1


def test_rename_field_wrong_subdir_is_noop():
    t = rename_field("claims", old="old_name", new="new_name")
    raw = {"old_name": "value"}
    result = t(raw, "entities")
    assert "old_name" in result
    assert "new_name" not in result


# ---------------------------------------------------------------------------
# add_default transform factory
# ---------------------------------------------------------------------------


def test_add_default_adds_missing():
    t = add_default("claims", field_name="new_field", default="hello")
    raw = {"id": "x"}
    result = t(raw, "claims")
    assert result["new_field"] == "hello"


def test_add_default_skips_existing():
    t = add_default("claims", field_name="new_field", default="hello")
    raw = {"id": "x", "new_field": "existing"}
    result = t(raw, "claims")
    assert result["new_field"] == "existing"


def test_add_default_callable():
    t = add_default("claims", field_name="tags", default=list)
    raw = {"id": "x"}
    result = t(raw, "claims")
    assert result["tags"] == []


# ---------------------------------------------------------------------------
# Round-trip with a real Migration in MIGRATIONS
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_migrations(monkeypatch):
    """Register a fake 0.1→0.2 migration for the duration of a test."""
    fake_migration = Migration(
        from_version="0.1",
        to_version="0.2",
        transforms=[
            rename_field("claims", old="old_field", new="new_field"),
            add_default("claims", field_name="added_field", default="default_val"),
        ],
    )
    monkeypatch.setattr("vouch.migration.MIGRATIONS", [fake_migration])
    return fake_migration


def test_migrate_round_trip(tmp_path: Path, patched_migrations):
    kb_dir, _ = _seed_kb_with_claim(tmp_path, old_field="old_value")
    _write_config(kb_dir, "0.1")

    result = migrate_kb(tmp_path, from_v="0.1", to_v="0.2")

    assert "claims/test-claim.yaml" in result.changed
    migrated = _yaml_load((kb_dir / "claims" / "test-claim.yaml").read_text())
    assert "new_field" in migrated
    assert migrated["new_field"] == "old_value"
    assert "old_field" not in migrated
    assert migrated["added_field"] == "default_val"

    # config.yaml should be bumped
    cfg = _yaml_load((kb_dir / CONFIG_FILENAME).read_text())
    assert cfg["schema_version"] == "0.2"


def test_migrate_no_transform_skips_yaml_roundtrip_whitespace(tmp_path: Path, monkeypatch):
    """No-op transforms must not flag files whose on-disk YAML differs from _yaml_dump."""
    noop_migration = Migration(from_version="0.1", to_version="0.2", transforms=[])
    monkeypatch.setattr("vouch.migration.MIGRATIONS", [noop_migration])

    kb_dir, src_id = _seed_kb_with_claim(tmp_path, "messy-claim", text="hello")

    messy_yaml = f"""id: messy-claim
text: hello
type: observation
status: working
confidence: 0.7
evidence:
- {src_id}
entities: []
supersedes: []
superseded_by: null
contradicts: []
scope: project
tags: []
created_at: '2024-01-01T00:00:00+00:00'
updated_at: '2024-01-01T00:00:00+00:00'
last_confirmed_at: null
approved_by: null
"""
    (kb_dir / "claims" / "messy-claim.yaml").write_text(messy_yaml)
    _write_config(kb_dir, "0.1")

    result = migrate_kb(tmp_path, from_v="0.1", to_v="0.2", dry_run=True)

    assert "claims/messy-claim.yaml" not in result.changed


def test_migrate_dry_run_does_not_write(tmp_path: Path, patched_migrations):
    kb_dir, _ = _seed_kb_with_claim(tmp_path, old_field="old_value")
    _write_config(kb_dir, "0.1")

    result = migrate_kb(tmp_path, from_v="0.1", to_v="0.2", dry_run=True)

    assert result.dry_run is True
    assert result.changed  # reported as changed
    # But the file on disk is untouched
    on_disk = _yaml_load((kb_dir / "claims" / "test-claim.yaml").read_text())
    assert "old_field" in on_disk
    assert "new_field" not in on_disk
    # And no tmp dir left behind
    assert not (tmp_path / ".vouch-migrate-tmp").exists()


# ---------------------------------------------------------------------------
# Rollback on validation failure
# ---------------------------------------------------------------------------


def test_migrate_rollback_on_validation_error(tmp_path: Path, monkeypatch):
    """When the transform produces an invalid artifact, the original is preserved."""
    from vouch.migration import Migration

    def _bad_transform(raw, subdir):
        # Delete a required field to break Pydantic validation
        raw.pop("id", None)
        return raw

    bad_migration = Migration(
        from_version="0.1",
        to_version="0.2",
        transforms=[_bad_transform],
    )
    monkeypatch.setattr("vouch.migration.MIGRATIONS", [bad_migration])

    kb_dir, _ = _seed_kb_with_claim(tmp_path)
    original_text = (kb_dir / "claims" / "test-claim.yaml").read_text()
    _write_config(kb_dir, "0.1")

    with pytest.raises(RuntimeError, match="validation failed"):
        migrate_kb(tmp_path, from_v="0.1", to_v="0.2")

    # Original must be untouched
    on_disk = (kb_dir / "claims" / "test-claim.yaml").read_text()
    assert on_disk == original_text
    # No tmp dir left behind
    assert not (kb_dir.parent / ".vouch-migrate-tmp").exists()
    # config still at old version
    cfg = _yaml_load((kb_dir / CONFIG_FILENAME).read_text())
    assert cfg.get("schema_version") == "0.1"


# ---------------------------------------------------------------------------
# migrate_kb raises on non-existent KB
# ---------------------------------------------------------------------------


def test_migrate_no_kb_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        migrate_kb(tmp_path, from_v="0.1", to_v="0.2")
