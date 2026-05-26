"""On-disk KB format migration behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import audit, migrations
from vouch.cli import cli
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _config(store: KBStore) -> dict:
    loaded = yaml.safe_load(store.config_path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_current_kb_has_no_migration_plan(store: KBStore) -> None:
    plan = migrations.build_plan(store)

    assert plan.current_version == 1
    assert plan.target_version == 1
    assert plan.steps == []

    result = CliRunner().invoke(cli, ["migrate", "--check"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_migrate_check_reports_needed_migration_without_writing(store: KBStore) -> None:
    config = _config(store)
    config.pop("version")
    store.config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    result = CliRunner().invoke(cli, ["migrate", "--check"])

    assert result.exit_code == 1, result.output
    assert "0 -> 1" in result.output
    assert "dry run" in result.output
    assert "version" not in _config(store)


def test_migrate_dry_run_does_not_create_missing_layout(store: KBStore) -> None:
    config = _config(store)
    config.pop("version")
    store.config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    (store.kb_dir / "proposed").rmdir()

    result = CliRunner().invoke(cli, ["migrate", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "create proposed/" in result.output
    assert "version" not in _config(store)
    assert not (store.kb_dir / "proposed").exists()


def test_migrate_applies_legacy_v0_to_v1(store: KBStore) -> None:
    config = _config(store)
    config.pop("version")
    store.config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    (store.kb_dir / "proposed").rmdir()
    (store.kb_dir / ".gitignore").write_text("custom.tmp\n")

    result = CliRunner().invoke(cli, ["migrate"])

    assert result.exit_code == 0, result.output
    assert _config(store)["version"] == 1
    assert (store.kb_dir / "proposed").is_dir()
    assert "custom.tmp" in (store.kb_dir / ".gitignore").read_text()
    assert "proposed/" in (store.kb_dir / ".gitignore").read_text()
    assert (store.kb_dir / "state.db").exists()
    events = list(audit.read_events(store.kb_dir))
    assert events[-1].event == "kb.migrate"
    assert events[-1].data["from_version"] == 0
    assert events[-1].data["to_version"] == 1


def test_migrate_rejects_newer_kb_version_cleanly(store: KBStore) -> None:
    config = _config(store)
    config["version"] = 999
    store.config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    result = CliRunner().invoke(cli, ["migrate"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "newer than this vouch supports" in result.output
