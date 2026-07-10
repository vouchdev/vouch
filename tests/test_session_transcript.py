"""kb.session_transcript — locate + parse raw agent transcripts on demand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import transcript


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# --- Task 1: locator ------------------------------------------------------


def test_find_claude_file_top_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    sid = "ad5d5e5f-0097-494c-8316-b01aed5dabf2"
    f = root / "-home-a-Dev-agentsview" / f"{sid}.jsonl"
    _write_jsonl(f, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    assert transcript.find_claude_file(sid) == f


def test_find_claude_file_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    parent = "11111111-1111-1111-1111-111111111111"
    child = "22222222-2222-2222-2222-222222222222"
    f = root / "-proj" / parent / "subagents" / "jobs" / f"{child}.jsonl"
    _write_jsonl(f, [{"type": "assistant", "message": {"role": "assistant", "content": []}}])
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    assert transcript.find_claude_file(child) == f


def test_find_claude_file_rejects_bad_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert transcript.find_claude_file("../etc/passwd") is None
    assert transcript.find_claude_file("*") is None
    assert transcript.find_claude_file("") is None
