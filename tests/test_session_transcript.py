"""kb.session_transcript — locate + parse raw agent transcripts on demand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import capture, transcript
from vouch.storage import KBStore


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


# --- Task 2: Claude parser ------------------------------------------------

_CLAUDE_LINES = [
    {"type": "user", "cwd": "/repo", "gitBranch": "main",
     "timestamp": "2026-07-10T04:44:19.043Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]}},
    {"type": "ai-title", "aiTitle": "Fix the bug"},
    {"type": "assistant", "timestamp": "2026-07-10T04:44:35.759Z",
     "message": {"id": "msg_1", "model": "claude-opus-4-8", "role": "assistant",
                 "content": [{"type": "thinking", "thinking": "let me look"}],
                 "usage": {"input_tokens": 100, "output_tokens": 10,
                           "cache_read_input_tokens": 5,
                           "cache_creation_input_tokens": 2}}},
    {"type": "assistant", "timestamp": "2026-07-10T04:44:36.771Z",
     "message": {"id": "msg_1", "model": "claude-opus-4-8", "role": "assistant",
                 "content": [{"type": "text", "text": "I'll edit it."}],
                 "usage": {"input_tokens": 100, "output_tokens": 10}}},
    {"type": "assistant", "timestamp": "2026-07-10T04:44:36.772Z",
     "message": {"id": "msg_1", "model": "claude-opus-4-8", "role": "assistant",
                 "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash",
                              "input": {"command": "go test ./..."}}],
                 "usage": {"input_tokens": 100, "output_tokens": 10}}},
    {"type": "user",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok\n", "is_error": False}]}},
]


def test_parse_claude_pairs_result_and_merges_by_message_id(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, _CLAUDE_LINES)
    out = transcript.parse_claude_transcript(f)

    assert out["session"]["cwd"] == "/repo"
    assert out["session"]["git_branch"] == "main"
    assert out["session"]["title"] == "Fix the bug"
    assert out["session"]["model"] == "claude-opus-4-8"
    assert out["truncated"] is False

    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant"]  # tool_result user entry is consumed

    a = out["messages"][1]
    assert [b["type"] for b in a["blocks"]] == ["thinking", "text", "tool_use"]
    tu = a["blocks"][2]
    assert tu["name"] == "Bash" and tu["input"] == {"command": "go test ./..."}
    assert tu["result"] == {"content": "ok\n", "is_error": False, "subagent_session_id": None}
    assert a["tokens"] == {"input": 100, "output": 10, "cache_read": 5, "cache_creation": 2}


def test_parse_claude_truncates_at_cap(tmp_path: Path) -> None:
    lines = [
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "text", "text": f"m{i}"}]}}
        for i in range(5)
    ]
    f = tmp_path / "big.jsonl"
    _write_jsonl(f, lines)
    out = transcript.parse_claude_transcript(f, max_messages=3)
    assert out["truncated"] is True
    assert len(out["messages"]) == 3


def test_parse_claude_tolerates_malformed_lines(tmp_path: Path) -> None:
    f = tmp_path / "m.jsonl"
    good = '{"type":"user","message":{"role":"user","content":"hey"}}'
    f.write_text('{"bad json\n' + good + "\n", encoding="utf-8")
    out = transcript.parse_claude_transcript(f)
    assert len(out["messages"]) == 1
    assert out["messages"][0]["blocks"] == [{"type": "text", "text": "hey"}]


# --- Task 3: load_transcript orchestrator ---------------------------------


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_load_transcript_available(
    tmp_path: Path, store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "projects"
    sid = "ad5d5e5f-0097-494c-8316-b01aed5dabf2"
    f = root / "-repo" / f"{sid}.jsonl"
    _write_jsonl(f, _CLAUDE_LINES)
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    out = transcript.load_transcript(store, sid)
    assert out["available"] is True
    assert out["source"] == {"agent": "claude", "path": str(f)}
    assert out["session"]["title"] == "Fix the bug"


def test_load_transcript_degrades_to_observations(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(store.kb_dir / "none"))
    sid = "99999999-9999-9999-9999-999999999999"
    capture.observe(store, sid, tool="Edit", summary="Edited x.go")
    out = transcript.load_transcript(store, sid)
    assert out["available"] is False
    assert out["observations"][0]["tool"] == "Edit"
    assert "reason" in out


def test_load_transcript_degrades_when_oversized(
    tmp_path: Path, store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "projects"
    sid = "88888888-8888-8888-8888-888888888888"
    f = root / "-repo" / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x" * 32, encoding="utf-8")
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setattr(transcript, "MAX_FILE_BYTES", 16)
    out = transcript.load_transcript(store, sid)
    assert out["available"] is False
    assert "too large" in out["reason"]


# --- Task 4: RPC handler --------------------------------------------------


def test_capabilities_advertises_session_transcript() -> None:
    from vouch.capabilities import capabilities
    from vouch.jsonl_server import HANDLERS

    assert "kb.session_transcript" in capabilities().methods
    assert "kb.session_transcript" in HANDLERS


def test_handler_missing_session_id_is_missing_param() -> None:
    from vouch.jsonl_server import handle_request

    resp = handle_request({"id": "1", "method": "kb.session_transcript", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_param"


def test_handler_bad_agent_is_invalid_request() -> None:
    from vouch.jsonl_server import handle_request

    resp = handle_request({
        "id": "2", "method": "kb.session_transcript",
        "params": {"session_id": "11111111-1111-1111-1111-111111111111", "agent": "grok"},
    })
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_request"


def test_handler_returns_degraded_when_absent() -> None:
    from vouch.jsonl_server import handle_request

    resp = handle_request({
        "id": "3", "method": "kb.session_transcript",
        "params": {"session_id": "11111111-1111-1111-1111-111111111111"},
    })
    assert resp["ok"] is True
    assert resp["result"]["available"] is False


# --- Task 9: Codex parser -------------------------------------------------

_CODEX_LINES = [
    {"type": "session_meta", "payload": {
        "id": "cx-1", "cwd": "/repo", "timestamp": "2026-06-22T08:01:54Z",
        "git": {"branch": "feat/x"}}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "<permissions>boilerplate"}]}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "run the tests"}]}},
    {"type": "response_item", "payload": {
        "type": "reasoning", "encrypted_content": "gAAA...", "summary": []}},
    {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Running them now."}]}},
    {"type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command",
        "arguments": "{\"cmd\": \"pytest\"}", "call_id": "call_1"}},
    {"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "call_1", "output": "1 passed"}},
    {"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "apply_patch",
        "input": "*** Begin Patch", "call_id": "call_2"}},
    {"type": "response_item", "payload": {
        "type": "custom_tool_call_output", "call_id": "call_2", "output": "Success"}},
]


def test_parse_codex_pairs_calls_and_skips_boilerplate(tmp_path: Path) -> None:
    f = tmp_path / "rollout-x.jsonl"
    _write_jsonl(f, _CODEX_LINES)
    out = transcript.parse_codex_transcript(f)

    assert out["session"]["agent"] == "codex"
    assert out["session"]["cwd"] == "/repo"
    assert out["session"]["git_branch"] == "feat/x"

    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant"]  # developer message skipped

    assistant = out["messages"][1]
    types = [b["type"] for b in assistant["blocks"]]
    assert types == ["text", "tool_use", "tool_use"]  # reasoning (encrypted) skipped

    exec_call = assistant["blocks"][1]
    assert exec_call["name"] == "exec_command"
    assert exec_call["result"]["content"] == "1 passed"
    patch_call = assistant["blocks"][2]
    assert patch_call["name"] == "apply_patch"
    assert patch_call["result"]["content"] == "Success"


def test_load_transcript_codex_source(
    tmp_path: Path, store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No Claude file exists; a Codex rollout does -> load_transcript finds it.
    codex_home = tmp_path / "codex"
    sid = "019eec6b-4a0c-7ad0-afd1-68973c902231"
    day = codex_home / "sessions" / "2026" / "06" / "22"
    roll = day / f"rollout-2026-06-22T08-01-54-{sid}.jsonl"
    _write_jsonl(roll, _CODEX_LINES)
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    out = transcript.load_transcript(store, sid)
    assert out["available"] is True
    assert out["source"]["agent"] == "codex"
