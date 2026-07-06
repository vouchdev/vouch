"""Session-start recall digest — inject approved knowledge into new sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import recall
from vouch.models import ClaimStatus
from vouch.proposals import approve, propose_claim, propose_page
from vouch.storage import KBStore, _starter_config


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _approve_claim(store: KBStore, text: str):
    src = store.put_source(b"evidence")
    pr = propose_claim(store, text=text, evidence=[src.id], proposed_by="a")
    return approve(store, pr.id, approved_by="u")


def _approve_page(store: KBStore, title: str):
    pr = propose_page(store, title=title, body="body", proposed_by="a")
    return approve(store, pr.id, approved_by="u")


def test_digest_includes_approved_claim_and_page(store: KBStore) -> None:
    _approve_claim(store, "JWT chosen over sessions")
    _approve_page(store, "auth design")
    d = recall.build_digest(store)
    assert "<vouch-approved-knowledge>" in d
    assert "JWT chosen over sessions" in d
    assert "auth design" in d


def test_digest_excludes_retracted_claims(store: KBStore) -> None:
    _approve_claim(store, "live fact")
    archived = _approve_claim(store, "archived fact")
    archived.status = ClaimStatus.ARCHIVED
    store.update_claim(archived)
    d = recall.build_digest(store)
    assert "live fact" in d
    assert "archived fact" not in d


def test_empty_kb_digest_is_empty(store: KBStore) -> None:
    assert recall.build_digest(store) == ""


def test_digest_truncates_with_notice(store: KBStore) -> None:
    for i in range(40):
        _approve_claim(store, f"fact number {i} " + "x" * 80)
    d = recall.build_digest(store, max_chars=600)
    assert len(d) <= 800
    assert "truncated" in d.lower()


def test_load_config_defaults(store: KBStore) -> None:
    cfg = recall.load_config(store)
    assert cfg.enabled is True
    assert cfg.max_chars == recall.DEFAULT_MAX_CHARS


def test_load_config_override(store: KBStore) -> None:
    store.config_path.write_text(
        "recall:\n  enabled: false\n  max_chars: 500\n", encoding="utf-8"
    )
    cfg = recall.load_config(store)
    assert cfg.enabled is False
    assert cfg.max_chars == 500


def test_starter_config_has_recall_namespace() -> None:
    assert _starter_config()["recall"]["enabled"] is True


def test_load_config_malformed_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("recall: [unclosed\n", encoding="utf-8")
    assert recall.load_config(store).enabled is True


def test_load_config_non_dict_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("plain string\n", encoding="utf-8")
    assert recall.load_config(store).max_chars == recall.DEFAULT_MAX_CHARS


def test_load_config_recall_not_a_mapping(store: KBStore) -> None:
    store.config_path.write_text("recall: 7\n", encoding="utf-8")
    assert recall.load_config(store).enabled is True


def test_cli_recall_emits_digest(store: KBStore) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    _approve_claim(store, "prefer ruff over flake8")
    res = CliRunner().invoke(
        cli, ["recall"], env={"VOUCH_KB_PATH": str(store.kb_dir)}
    )
    assert res.exit_code == 0
    assert "prefer ruff over flake8" in res.output
    assert "<vouch-approved-knowledge>" in res.output


def test_cli_recall_silent_when_disabled(store: KBStore) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    _approve_claim(store, "some fact")
    store.config_path.write_text("recall:\n  enabled: false\n", encoding="utf-8")
    res = CliRunner().invoke(
        cli, ["recall"], env={"VOUCH_KB_PATH": str(store.kb_dir)}
    )
    assert res.exit_code == 0
    assert res.output.strip() == ""


def test_adapter_sessionstart_runs_recall() -> None:
    import json as _json

    root = Path(__file__).resolve().parents[1]
    settings = _json.loads(
        (root / "adapters/claude-code/.claude/settings.json").read_text()
    )
    cmds = [
        h.get("command", "")
        for g in settings["hooks"]["SessionStart"]
        for h in g.get("hooks", [])
    ]
    assert any("vouch recall" in c for c in cmds)
