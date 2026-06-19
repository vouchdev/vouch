"""Typed config loading and health reporting."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import health
from vouch.storage import ConfigError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_config_loads_defaults_for_missing_optional_blocks(store: KBStore) -> None:
    store.config_path.write_text(yaml.safe_dump({"version": 1}))

    cfg = store.config

    assert cfg.review.require_human_approval is True
    assert cfg.retrieval.default_limit == 10
    assert cfg.retrieval.backends == ["fts5", "substring"]
    assert cfg.mcp == {}


def test_config_rejects_malformed_values(store: KBStore) -> None:
    store.config_path.write_text(yaml.safe_dump({
        "retrieval": {"default_limit": "ten"},
    }))

    with pytest.raises(ConfigError, match="retrieval.default_limit"):
        _ = store.config


def test_doctor_warns_on_unknown_top_level_config_key(store: KBStore) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg["reveiw"] = {"approver_role": "trusted-agent"}
    store.config_path.write_text(yaml.safe_dump(cfg))

    report = health.doctor(store)

    assert report.ok
    assert any(
        f.code == "unknown_config_key" and "reveiw" in f.message
        for f in report.findings
    )


def test_doctor_errors_on_invalid_config(store: KBStore) -> None:
    store.config_path.write_text(yaml.safe_dump({
        "retrieval": {"default_limit": "ten"},
    }))

    report = health.doctor(store)

    assert not report.ok
    assert any(f.code == "invalid_config" for f in report.findings)
