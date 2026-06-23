"""Tests for the desktop shell helpers (#207)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.desktop import (
    DesktopState,
    check_kb_folder,
    init_kb_at,
    load_state,
    save_state,
    state_file_path,
    touch_recent_kb,
)
from vouch.desktop.paths import config_dir
from vouch.storage import KBStore


def test_config_dir_under_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "vouch-desktop"


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = DesktopState(last_kb="/proj/a")
    save_state(state, path)
    loaded = load_state(path)
    assert loaded.last_kb == "/proj/a"
    assert loaded.version == 1


def test_touch_recent_caps_at_five(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    roots = [tmp_path / f"proj{i}" for i in range(7)]
    for root in roots:
        root.mkdir()
        touch_recent_kb(root, path=path)
    state = load_state(path)
    assert len(state.recent_kbs) == 5
    assert Path(state.recent_kbs[0].path) == roots[6].resolve()


def test_touch_recent_dedupes_and_moves_front(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    touch_recent_kb(root_a, path=path)
    touch_recent_kb(root_b, path=path)
    touch_recent_kb(root_a, label="renamed", path=path)
    state = load_state(path)
    assert [Path(e.path) for e in state.recent_kbs] == [
        root_a.resolve(),
        root_b.resolve(),
    ]
    assert state.recent_kbs[0].label == "renamed"


def test_check_kb_folder_ok(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "my-project")
    result = check_kb_folder(store.root)
    assert result.ok is True
    assert result.project_root == str(store.root)
    assert result.kb_dir == str(store.kb_dir)


def test_check_kb_folder_accepts_dot_vouch(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "proj")
    result = check_kb_folder(store.kb_dir)
    assert result.ok is True
    assert result.project_root == str(store.root)


def test_check_kb_folder_missing(tmp_path: Path) -> None:
    bare = tmp_path / "empty"
    bare.mkdir()
    result = check_kb_folder(bare)
    assert result.ok is False
    assert "no .vouch" in result.message


def test_init_kb_at_creates_starter(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    out = init_kb_at(target, actor="test-desktop")
    assert out["ok"] is True
    assert out["starter_present"] is True
    assert Path(out["kb_dir"]).is_dir()


def test_cli_desktop_kb_check(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "p")
    runner = CliRunner()
    result = runner.invoke(cli, ["desktop", "kb-check", str(store.root)])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["ok"] is True


def test_cli_desktop_kb_init(tmp_path: Path) -> None:
    target = tmp_path / "new-kb"
    runner = CliRunner()
    result = runner.invoke(cli, ["desktop", "kb-init", str(target)])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["starter_present"] is True


def test_cli_init_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--path", str(tmp_path / "kb"), "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["ok"] is True
    assert body["starter_present"] is True


def test_cli_desktop_state_touch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = KBStore.init(tmp_path / "proj")
    state_path = tmp_path / "state.json"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "desktop",
            "state-touch",
            str(store.root),
            "--state-file",
            str(state_path),
            "--label",
            "my-proj",
        ],
    )
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["last_kb"] == str(store.root.resolve())
    assert body["recent_kbs"][0]["label"] == "my-proj"


def test_default_state_file_name() -> None:
    assert state_file_path().name == "state.json"
