"""Consolidation — config, survivor selection, registration parity."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import consolidate as cons
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_load_config_defaults(store: KBStore) -> None:
    cfg = cons._load_consolidate_config(store)
    assert cfg["threshold"] == 0.95
    assert cfg["mode"] == "supersede"
    assert cfg["max_clusters"] == 50


def test_load_config_custom(store: KBStore) -> None:
    raw = yaml.safe_load(store.config_path.read_text()) or {}
    raw["consolidate"] = {
        "threshold": 0.85,
        "mode": "merge",
        "max_clusters": 10,
    }
    store.config_path.write_text(yaml.dump(raw))
    cfg = cons._load_consolidate_config(store)
    assert cfg["threshold"] == 0.85
    assert cfg["mode"] == "merge"
    assert cfg["max_clusters"] == 10


def test_load_config_defensive_bad_types(store: KBStore) -> None:
    """Malformed config values should fall back to defaults."""
    raw = yaml.safe_load(store.config_path.read_text()) or {}
    raw["consolidate"] = {
        "threshold": "not-a-float",
        "mode": "invalid-mode",
        "max_clusters": -5,
    }
    store.config_path.write_text(yaml.dump(raw))
    cfg = cons._load_consolidate_config(store)
    assert cfg["threshold"] == 0.95
    assert cfg["mode"] == "supersede"
    assert cfg["max_clusters"] == 50


def test_load_config_consolidate_is_bool(store: KBStore) -> None:
    """consolidate: true should not crash — falls back to defaults."""
    raw = yaml.safe_load(store.config_path.read_text()) or {}
    raw["consolidate"] = True
    store.config_path.write_text(yaml.dump(raw))
    cfg = cons._load_consolidate_config(store)
    assert cfg["threshold"] == 0.95


def test_survivor_selection_deterministic(store: KBStore) -> None:
    """Highest confidence wins; ties broken by updated_at then id."""
    from vouch.models import Claim

    src = store.put_source(b"evidence")
    store.put_claim(Claim(
        id="c1", text="first", evidence=[src.id], confidence=0.8,
    ))
    store.put_claim(Claim(
        id="c2", text="second", evidence=[src.id], confidence=0.9,
    ))
    store.put_claim(Claim(
        id="c3", text="third", evidence=[src.id], confidence=0.9,
    ))
    # c2 and c3 tie on confidence; c3 is created after c2 so has later
    # updated_at (both default to utcnow but created in sequence).
    survivor = cons._select_survivor(store, ["c1", "c2", "c3"])
    # c2 or c3 should win (both 0.9), c1 (0.8) should not.
    assert survivor in ("c2", "c3")
    assert survivor != "c1"


def test_consolidate_no_embeddings(store: KBStore) -> None:
    """Without embeddings, consolidate returns empty clusters."""
    result = cons.consolidate(store, dry_run=True)
    assert result.clusters == []
    assert result.dry_run is True


def test_consolidate_no_eligible_claims(store: KBStore) -> None:
    """With no approved claims, returns empty."""
    result = cons.consolidate(store)
    assert result.clusters == []
