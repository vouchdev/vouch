"""Typed Config model + validation for `.vouch/config.yaml` (issue #243)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.models import Config, ConfigError
from vouch.storage import KBStore, _starter_config, _yaml_dump


def _init_kb(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _write_config(store: KBStore, raw: dict) -> None:
    store.config_path.write_text(_yaml_dump(raw))


# --- the model in isolation ------------------------------------------------


def test_starter_config_parses_with_no_unknown_keys() -> None:
    cfg = Config.load(_starter_config())
    assert cfg.review.require_human_approval is True
    assert cfg.review.expire_pending_after_days == 90
    assert cfg.retrieval.backend == "auto"
    assert cfg.retrieval.default_limit == 10
    assert cfg.unknown_keys() == []


def test_empty_and_none_yield_defaults() -> None:
    for raw in (None, {}):
        cfg = Config.load(raw)
        assert cfg.review.expire_pending_after_days == 90
        assert cfg.retrieval.resolved_backend() == "auto"
        assert cfg.review.approver_role is None


def test_partial_config_fills_documented_defaults() -> None:
    # An existing KB with only a `review` block still gets retrieval defaults.
    cfg = Config.load({"version": 1, "review": {"approver_role": "trusted-agent"}})
    assert cfg.review.approver_role == "trusted-agent"
    assert cfg.retrieval.resolved_backend() == "auto"
    assert cfg.retrieval.default_limit == 10


def test_malformed_value_fails_fast_with_field_path() -> None:
    with pytest.raises(ConfigError) as ei:
        Config.load({"retrieval": {"default_limit": "ten"}})
    assert "retrieval.default_limit" in str(ei.value)


def test_negative_expire_days_rejected() -> None:
    with pytest.raises(ConfigError) as ei:
        Config.load({"review": {"expire_pending_after_days": -1}})
    assert "review.expire_pending_after_days" in str(ei.value)


def test_non_mapping_top_level_rejected() -> None:
    with pytest.raises(ConfigError):
        Config.load(["not", "a", "mapping"])


def test_unknown_top_level_key_preserved_not_dropped() -> None:
    # `reveiw` is a typo — it must not silently vanish.
    cfg = Config.load({"reveiw": {"require_human_approval": False}})
    assert cfg.unknown_keys() == ["reveiw"]
    # The correctly-spelled default still applies.
    assert cfg.review.require_human_approval is True


def test_known_sections_never_flagged_as_unknown() -> None:
    cfg = Config.load(
        {
            "serve": {"bearer_tokens": []},
            "volunteer": {"enabled": False},
            "mcp": {"publish_skills": True},
        }
    )
    assert cfg.unknown_keys() == []


def test_resolved_backend_honours_legacy_list() -> None:
    cfg = Config.load({"retrieval": {"backends": ["fts5", "substring"]}})
    assert cfg.retrieval.resolved_backend() == "fts5"


def test_resolved_backend_falls_back_on_unrecognised() -> None:
    cfg = Config.load({"retrieval": {"backend": "nonsense"}})
    assert cfg.retrieval.resolved_backend() == "auto"


# --- KBStore integration ---------------------------------------------------


def test_store_config_defaults_when_file_missing(tmp_path: Path) -> None:
    store = _init_kb(tmp_path)
    store.config_path.unlink()
    fresh = KBStore(tmp_path)
    assert fresh.config.retrieval.resolved_backend() == "auto"


def test_store_config_round_trips_starter(tmp_path: Path) -> None:
    store = _init_kb(tmp_path)
    assert store.config.unknown_keys() == []
    assert store.config.review.expire_pending_after_days == 90


def test_store_config_raises_on_malformed(tmp_path: Path) -> None:
    store = _init_kb(tmp_path)
    _write_config(store, {"retrieval": {"default_limit": "ten"}})
    with pytest.raises(ConfigError):
        _ = KBStore(tmp_path).config


def test_doctor_warns_on_unknown_key(tmp_path: Path) -> None:
    from vouch.health import doctor

    store = _init_kb(tmp_path)
    _write_config(store, {**_starter_config(), "reveiw": {}})
    report = doctor(KBStore(tmp_path))
    codes = {(f.severity, f.code) for f in report.findings}
    assert ("warning", "config_unknown_key") in codes
    # An unknown key is only a warning — doctor stays ok.
    assert report.ok is True


def test_doctor_errors_on_invalid_config(tmp_path: Path) -> None:
    from vouch.health import doctor

    store = _init_kb(tmp_path)
    _write_config(store, {"retrieval": {"default_limit": "ten"}})
    report = doctor(KBStore(tmp_path))
    codes = {(f.severity, f.code) for f in report.findings}
    assert ("error", "config_invalid") in codes
    assert report.ok is False
