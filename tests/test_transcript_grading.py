"""LLM review-relevance grading — validation, caching, degradation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from vouch import transcript
from vouch.storage import KBStore

SID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"


def _write_session(root: Path) -> Path:
    f = root / "-proj" / f"{SID}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>secret recall</system-reminder>"},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "fix the login redirect"},
        ]}},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "text", "text": "The redirect drops the port."},
        ], "usage": {}}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "thanks"},
        ]}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return f


def _fake_llm(tmp_path: Path, response: dict | str) -> str:
    """A stdin→stdout llm_cmd that logs each call's prompt for assertions."""
    script = tmp_path / "fake_llm.py"
    out = json.dumps(response) if isinstance(response, dict) else response
    script.write_text(
        "import sys, pathlib\n"
        f"log = pathlib.Path({str(tmp_path / 'calls.log')!r})\n"
        "prompt = sys.stdin.read()\n"
        "with log.open('a') as fh:\n"
        "    fh.write(prompt.replace(chr(10), ' ') + chr(10))\n"
        f"sys.stdout.write({out!r})\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path / "repo")
    monkeypatch.chdir(s.root)
    monkeypatch.setenv(
        "VOUCH_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"),
    )
    _write_session(tmp_path / "projects")
    return s


def _set_llm(store: KBStore, cmd: str | None) -> None:
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg["compile"] = {"llm_cmd": cmd} if cmd else {}
    store.config_path.write_text(yaml.safe_dump(cfg))


def test_grades_attach_and_invalid_entries_dropped(
    store: KBStore, tmp_path: Path,
) -> None:
    _set_llm(store, _fake_llm(tmp_path, {"grades": [
        {"i": 1, "grade": "key", "note": "the actual goal"},
        {"i": 3, "grade": "low"},
        {"i": 99, "grade": "key"},          # out of range — dropped
        {"i": 2, "grade": "meh"},           # unknown grade — dropped
        {"i": 0, "grade": "key"},           # noise message, not offered — dropped
    ]}))

    t = transcript.load_transcript(store, SID, grade=True)

    assert t["grading_available"] is True
    assert t["messages"][1]["relevance"] == {"grade": "key", "note": "the actual goal"}
    assert t["messages"][3]["relevance"] == {"grade": "low", "note": None}
    assert "relevance" not in t["messages"][0]
    assert "relevance" not in t["messages"][2]
    assert t["grading"]["cached"] is False
    assert t["grading"]["graded_messages"] == 2


def test_noise_never_reaches_the_llm(store: KBStore, tmp_path: Path) -> None:
    _set_llm(store, _fake_llm(tmp_path, {"grades": []}))

    transcript.load_transcript(store, SID, grade=True)

    (prompt,) = _calls(tmp_path)
    assert "secret recall" not in prompt
    assert "fix the login redirect" in prompt


def test_second_grade_serves_from_cache(store: KBStore, tmp_path: Path) -> None:
    _set_llm(store, _fake_llm(tmp_path, {"grades": [{"i": 1, "grade": "key"}]}))

    first = transcript.load_transcript(store, SID, grade=True)
    second = transcript.load_transcript(store, SID, grade=True)

    assert len(_calls(tmp_path)) == 1
    assert second["grading"]["cached"] is True
    assert second["messages"][1]["relevance"] == first["messages"][1]["relevance"]


def test_regrade_bypasses_cache(store: KBStore, tmp_path: Path) -> None:
    _set_llm(store, _fake_llm(tmp_path, {"grades": []}))

    transcript.load_transcript(store, SID, grade=True)
    t = transcript.load_transcript(store, SID, regrade=True)

    assert len(_calls(tmp_path)) == 2
    assert t["grading"]["cached"] is False


def test_no_llm_cmd_reports_unavailable(store: KBStore) -> None:
    _set_llm(store, None)

    t = transcript.load_transcript(store, SID, grade=True)

    assert t["grading_available"] is False
    assert "not configured" in t["grading"]["error"]
    assert t["messages"], "transcript must still render"


def test_garbage_llm_output_degrades_cleanly(
    store: KBStore, tmp_path: Path,
) -> None:
    _set_llm(store, _fake_llm(tmp_path, "definitely not json"))

    t = transcript.load_transcript(store, SID, grade=True)

    assert "error" in t["grading"]
    assert all("relevance" not in m for m in t["messages"])
    # a failed grading must not poison the cache
    _set_llm(store, _fake_llm(tmp_path, {"grades": [{"i": 1, "grade": "key"}]}))
    t2 = transcript.load_transcript(store, SID, grade=True)
    assert t2["messages"][1].get("relevance") == {"grade": "key", "note": None}


def test_plain_load_never_grades(store: KBStore, tmp_path: Path) -> None:
    _set_llm(store, _fake_llm(tmp_path, {"grades": []}))

    t = transcript.load_transcript(store, SID)

    assert _calls(tmp_path) == []
    assert "grading" not in t
    assert t["grading_available"] is True


def test_grading_subprocess_gets_capture_kill_switch(
    store: KBStore, tmp_path: Path,
) -> None:
    """The llm_cmd child env carries VOUCH_CAPTURE_DISABLE=1 so the agent
    session it spawns cannot file a fresh pending summary on every grade."""
    script = tmp_path / "env_probe.py"
    script.write_text(
        "import os, sys, pathlib\n"
        f"out = pathlib.Path({str(tmp_path / 'env.log')!r})\n"
        "sys.stdin.read()\n"
        "out.write_text(os.environ.get('VOUCH_CAPTURE_DISABLE', 'unset'))\n"
        'sys.stdout.write(\'{"grades": []}\')\n',
        encoding="utf-8",
    )
    _set_llm(store, f"{sys.executable} {script}")

    transcript.load_transcript(store, SID, grade=True)

    assert (tmp_path / "env.log").read_text() == "1"
