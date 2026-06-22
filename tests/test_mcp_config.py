"""``mcp:`` config block parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch.mcp_config import McpConfig, load_config
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path / "kb")


def test_load_config_missing_mcp_block_defaults_true(store: KBStore) -> None:
    cfg = load_config(store)
    assert cfg == McpConfig(publish_skills=True)


def test_load_config_publish_skills_false(store: KBStore) -> None:
    cfg_path = store.kb_dir / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    raw["mcp"] = {"publish_skills": False}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    assert load_config(store).publish_skills is False


def test_load_config_publish_skills_true_explicit(store: KBStore) -> None:
    cfg_path = store.kb_dir / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    raw["mcp"] = {"publish_skills": True}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    assert load_config(store).publish_skills is True


def test_to_capabilities_dict(store: KBStore) -> None:
    assert load_config(store).to_capabilities_dict() == {"publish_skills": True}
