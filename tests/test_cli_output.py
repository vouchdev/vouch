"""Track 2 (#54) — friendlier CLI output: colour, --json, progress callbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import bundle, health
from vouch.cli import cli
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    src = s.put_source(b"jwt rotation notes")
    s.put_claim(Claim(id="c1", text="JWT rotation matters", evidence=[src.id]))
    health.rebuild_index(s)
    return s


# --- 2a colour ------------------------------------------------------------


def test_status_no_color_by_default_in_pipe(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output  # no ANSI when not a TTY


def test_status_force_color_emits_ansi(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "\x1b[" in result.output  # ANSI present when forced


def test_no_color_wins_over_force_color(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    result = CliRunner().invoke(cli, ["status"])
    assert "\x1b[" not in result.output


# --- 2c --json ------------------------------------------------------------


def test_lint_json(store: KBStore) -> None:
    result = CliRunner().invoke(cli, ["lint", "--json"])
    payload = json.loads(result.output)
    assert "ok" in payload and "findings" in payload
    assert isinstance(payload["findings"], list)


def test_search_json(store: KBStore) -> None:
    result = CliRunner().invoke(cli, ["search", "JWT", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "backend" in payload
    assert isinstance(payload["hits"], list)


def test_search_human_default_has_no_json(store: KBStore) -> None:
    result = CliRunner().invoke(cli, ["search", "JWT"])
    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


# --- 2b progress callbacks ------------------------------------------------


def test_rebuild_index_reports_progress(store: KBStore) -> None:
    seen: list[str] = []
    health.rebuild_index(store, on_progress=seen.append)
    assert {"claims", "pages", "entities", "embeddings"} <= set(seen)


def test_import_apply_reports_progress(store: KBStore, tmp_path: Path) -> None:
    bundle_path = tmp_path / "kb.tar.gz"
    bundle.export(store.kb_dir, dest=bundle_path)
    dest = KBStore.init(tmp_path / "dest")
    seen: list[str] = []
    bundle.import_apply(dest.kb_dir, bundle_path, on_progress=seen.append)
    assert seen  # at least one member written + reported


def test_export_reports_progress(store: KBStore, tmp_path: Path) -> None:
    seen: list[str] = []
    bundle.export(store.kb_dir, dest=tmp_path / "k.tar.gz", on_progress=seen.append)
    assert seen
