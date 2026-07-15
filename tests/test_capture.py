"""Auto-capture: config, buffer, observe, finalize."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import capture as cap
from vouch.storage import KBStore, _starter_config


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_load_config_defaults(store: KBStore) -> None:
    cfg = cap.load_config(store)
    assert cfg.enabled is True
    assert cfg.min_observations == 3
    assert cfg.dedup_window_seconds == 60.0


def test_load_config_reads_override(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  enabled: false\n  min_observations: 5\n"
    )
    cfg = cap.load_config(store)
    assert cfg.enabled is False
    assert cfg.min_observations == 5


def test_buffer_path_under_captures_dir(store: KBStore) -> None:
    p = cap.buffer_path(store, "sess-123")
    assert p == store.kb_dir / "captures" / "sess-123.jsonl"


def test_starter_config_has_capture_namespace() -> None:
    assert _starter_config()["capture"]["enabled"] is True


def test_init_gitignores_captures(tmp_path: Path) -> None:
    kb = KBStore.init(tmp_path)
    assert "captures/" in (kb.kb_dir / ".gitignore").read_text()


def test_observe_appends_line(store: KBStore) -> None:
    wrote = cap.observe(store, "s1", tool="Edit", summary="Edited a.py", now=100.0)
    assert wrote is True
    lines = cap.buffer_path(store, "s1").read_text().splitlines()
    assert len(lines) == 1
    assert "Edited a.py" in lines[0]


def test_observe_masks_secrets_before_buffering(store: KBStore) -> None:
    """A pasted credential must never reach the buffer — once there it rolls
    into a committed session page and the append-only audit log."""
    wrote = cap.observe(
        store, "s1", tool="Bash",
        summary="Ran: export AWS_KEY=AKIAIOSFODNN7EXAMPLE",
        cmd="export AWS_KEY=AKIAIOSFODNN7EXAMPLE",
        now=100.0,
    )
    assert wrote is True
    obs = cap._read_observations(cap.buffer_path(store, "s1"))
    assert len(obs) == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in obs[0]["summary"]
    assert "AKIAIOSFODNN7EXAMPLE" not in obs[0]["cmd"]


def test_observe_dedups_within_window(store: KBStore) -> None:
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=100.0)
    # identical within 60s window -> skipped
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=130.0) is False
    # same key past the window -> written again
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=200.0)
    assert len(cap.buffer_path(store, "s1").read_text().splitlines()) == 2


def test_observe_noop_when_disabled(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  enabled: false\n")
    assert cap.observe(store, "s1", tool="Edit", summary="x") is False
    assert not cap.buffer_path(store, "s1").exists()


def test_summarize_tool_skips_unobserved() -> None:
    assert cap.summarize_tool("mcp__vouch__kb_search", {}, "") is None


def test_summarize_tool_edit() -> None:
    obs = cap.summarize_tool("Edit", {"file_path": "/repo/src/a.py"}, "ok")
    assert obs is not None
    assert obs["tool"] == "Edit"
    assert obs["files"] == ["/repo/src/a.py"]
    assert "a.py" in obs["summary"]


def test_summarize_tool_bash_flags_error() -> None:
    obs = cap.summarize_tool("Bash", {"command": "pytest"}, "1 failed, error")
    assert obs is not None
    assert obs["cmd"] == "pytest"
    assert "failed" in obs["summary"].lower()


def test_summarize_tool_read_grep_web_task() -> None:
    assert "a.py" in cap.summarize_tool("Read", {"file_path": "/x/a.py"}, "")["summary"]
    assert "TODO" in cap.summarize_tool("Grep", {"pattern": "TODO"}, "")["summary"]
    web = cap.summarize_tool("WebFetch", {"url": "https://example.com"}, "")
    assert "example.com" in web["summary"]
    assert cap.summarize_tool("Task", {}, "")["summary"] == "Task completed"


def test_observe_stores_cmd_field(store: KBStore) -> None:
    cap.observe(store, "s1", tool="Bash", summary="Ran: ls", cmd="ls -la", now=1.0)
    line = cap.buffer_path(store, "s1").read_text()
    assert "ls -la" in line


def test_load_config_malformed_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("capture: [unclosed\n")
    assert cap.load_config(store).enabled is True  # default, not a crash


def test_load_config_non_dict_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("just a string\n")
    assert cap.load_config(store).min_observations == 3


def test_load_config_capture_not_a_mapping(store: KBStore) -> None:
    store.config_path.write_text("capture: 42\n")
    assert cap.load_config(store).enabled is True


def test_read_observations_skips_blank_and_bad_lines(store: KBStore) -> None:
    p = cap.buffer_path(store, "s1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('\n{"ts": 1, "tool": "Edit", "summary": "ok"}\nnot-json\n')
    obs = cap._read_observations(p)
    assert len(obs) == 1
    assert obs[0]["summary"] == "ok"


def test_git_changes_in_a_real_repo(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        )

    try:
        git("init")
    except (OSError, subprocess.CalledProcessError):
        import pytest
        pytest.skip("git not available")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-m", "init")
    (tmp_path / "a.py").write_text("x = 2\n")  # modify tracked file
    files, stat = cap._git_changes(tmp_path)
    assert "a.py" in files
    assert "a.py" in stat


def test_git_changes_swallows_subprocess_error(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(cap.subprocess, "run", boom)
    assert cap._git_changes(tmp_path) == ([], "")


def test_git_changes_stat_error_returns_files_without_stat(tmp_path: Path, monkeypatch) -> None:
    calls = {"n": 0}

    class _R:
        stdout = "a.py\n"

    def run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _R()  # first call (names) succeeds
        raise OSError("stat failed")  # second call (stat) blows up

    monkeypatch.setattr(cap.subprocess, "run", run)
    files, stat = cap._git_changes(tmp_path)
    assert files == ["a.py"]
    assert stat == ""


def test_build_summary_body_renders_git_and_commands() -> None:
    obs = [{"ts": 1.0, "tool": "Bash", "summary": "Ran: pytest", "cmd": "pytest -q"}]
    _, body = cap.build_summary_body(
        "s1", obs, ["a.py"], "a.py | 2 +-", project="proj", generated_at="2026-07-01"
    )
    assert "## git changes" in body
    assert "pytest -q" in body
    assert "## notable commands" in body
    assert "proj" in body


def _seed(store: KBStore, sid: str, n: int) -> None:
    for i in range(n):
        cap.observe(store, sid, tool="Edit", summary=f"Edited f{i}.py", now=float(i))


def test_finalize_files_one_pending_page(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalKind, ProposalStatus

    _seed(store, "s1", 3)
    result = cap.finalize(store, "s1", cwd=tmp_path)
    pid = result["summary_proposal_id"]
    assert pid is not None
    pend = store.list_proposals(ProposalStatus.PENDING)
    match = [p for p in pend if p.id == pid]
    assert len(match) == 1
    pr = match[0]
    assert pr.kind == ProposalKind.PAGE
    assert pr.proposed_by == cap.CAPTURE_ACTOR
    assert pr.payload["type"] == cap.CAPTURE_PAGE_TYPE
    assert pr.status == ProposalStatus.PENDING


def test_finalize_below_min_files_nothing(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus

    _seed(store, "s1", 2)  # below default min_observations=3, non-git cwd
    result = cap.finalize(store, "s1", cwd=tmp_path)
    assert result["summary_proposal_id"] is None
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_finalize_deletes_buffer(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 3)
    cap.finalize(store, "s1", cwd=tmp_path)
    assert not cap.buffer_path(store, "s1").exists()


def test_finalize_noop_when_disabled(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus

    _seed(store, "s1", 5)
    store.config_path.write_text("capture:\n  enabled: false\n")
    result = cap.finalize(store, "s1", cwd=tmp_path)
    assert result["summary_proposal_id"] is None
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_build_summary_body_has_sections() -> None:
    obs = [
        {"ts": 1.0, "tool": "Edit", "summary": "Edited a.py", "files": ["a.py"]},
        {"ts": 2.0, "tool": "Bash", "summary": "Ran: pytest", "cmd": "pytest"},
    ]
    title, body = cap.build_summary_body("s1", obs, ["a.py"], "a.py | 2 +-")
    # the title describes what changed, never the uuid; the uuid stays in the body
    assert "a.py" in title
    assert "s1" not in title
    assert "- session: `s1`" in body
    assert "files modified this session" in body.lower()
    assert "## activity" in body.lower()
    assert "a.py" in body


def test_title_uses_first_prompt_excerpt() -> None:
    obs = [{"ts": 1.0, "tool": "Edit", "summary": "Edited a.py", "files": ["a.py"]}]
    title, body = cap.build_summary_body(
        "s1", obs, ["a.py"], "", first_prompt="fix the login redirect bug in oauth",
    )
    assert title == "session: fix the login redirect bug in oauth"
    assert "## prompt" in body
    assert "> fix the login redirect bug in oauth" in body

    long_prompt = "p" * 300
    title, _ = cap.build_summary_body("s1", obs, [], "", first_prompt=long_prompt)
    assert len(title) <= len("session: ") + 64
    assert title.endswith("…")


def test_title_fallback_names_dirs_and_date() -> None:
    files = ["web/app.css", "web/index.html", "docs/guide.md"]
    title, _ = cap.build_summary_body(
        "s1", [], files, "", generated_at="2026-07-04T10:00:00+00:00",
    )
    assert title == "session 2026-07-04: web, docs — 3 file(s)"

    title, _ = cap.build_summary_body("s1", [{"ts": 1.0, "tool": "Read", "summary": "x"}], [], "")
    assert "no file changes" in title


def test_first_user_prompt_skips_host_wrappers(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "queue-operation", "operation": "enqueue"},
        {
            "type": "user",
            "message": {"role": "user", "content": "<command-name>/model</command-name>"},
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": "<local-command-stdout>ok</local-command-stdout>",
            },
        },
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta noise"}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "  please add   retry logic\nto the fetcher  "},
        ]}},
        {"type": "user", "message": {"role": "user", "content": "a later prompt"}},
    ]
    transcript.write_text(
        "\n".join(_json.dumps(entry) for entry in lines), encoding="utf-8"
    )
    assert cap.first_user_prompt(transcript) == "please add retry logic to the fetcher"
    assert cap.first_user_prompt(tmp_path / "missing.jsonl") is None


def test_finalize_reads_transcript_for_title(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus

    _seed(store, "s1", 3)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _json.dumps({"type": "user", "message": {"role": "user", "content": "ship the digest"}}),
        encoding="utf-8",
    )
    cap.finalize(store, "s1", cwd=tmp_path, transcript_path=transcript)
    pend = store.list_proposals(ProposalStatus.PENDING)
    assert len(pend) == 1
    assert pend[0].payload["title"] == "session: ship the digest"


def test_pending_count_counts_capture_actor(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 3)
    cap.finalize(store, "s1", cwd=tmp_path)
    assert cap.pending_count(store) == 1


import json as _json  # noqa: E402

from click.testing import CliRunner  # noqa: E402

from vouch.cli import cli  # noqa: E402
from vouch.models import ProposalStatus  # noqa: E402


def _run(store: KBStore, args: list[str], stdin: str = "") -> object:
    runner = CliRunner()
    return runner.invoke(
        cli, args, input=stdin,
        env={"VOUCH_KB_PATH": str(store.kb_dir)},
    )


def test_cli_observe_appends(store: KBStore) -> None:
    payload = _json.dumps({
        "session_id": "cc-1",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/r/a.py"},
        "tool_response": "ok",
    })
    res = _run(store, ["capture", "observe"], stdin=payload)
    assert res.exit_code == 0
    assert cap.buffer_path(store, "cc-1").exists()


def test_cli_observe_never_errors_on_garbage(store: KBStore) -> None:
    res = _run(store, ["capture", "observe"], stdin="not json")
    assert res.exit_code == 0


def test_cli_finalize_files_proposal(store: KBStore) -> None:
    for i in range(3):
        cap.observe(store, "cc-2", tool="Edit", summary=f"Edited f{i}.py", now=float(i))
    payload = _json.dumps({"session_id": "cc-2", "cwd": str(store.kb_dir.parent)})
    res = _run(store, ["capture", "finalize"], stdin=payload)
    assert res.exit_code == 0
    pend = store.list_proposals(ProposalStatus.PENDING)
    assert any(p.proposed_by == cap.CAPTURE_ACTOR for p in pend)


def test_cli_banner_emits_when_pending(store: KBStore) -> None:
    for i in range(3):
        cap.observe(store, "cc-3", tool="Edit", summary=f"Edited f{i}.py", now=float(i))
    cap.finalize(store, "cc-3", cwd=store.kb_dir.parent)
    res = _run(store, ["capture", "banner"])
    assert res.exit_code == 0
    assert "awaiting review" in res.output


def test_cli_banner_silent_when_none(store: KBStore) -> None:
    res = _run(store, ["capture", "banner"])
    assert res.exit_code == 0
    assert res.output.strip() == ""


def test_adapter_settings_wires_capture_hooks() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = _json.loads(
        (root / "adapters/claude-code/.claude/settings.json").read_text()
    )
    hooks = settings["hooks"]

    def commands(event: str) -> list[str]:
        out: list[str] = []
        for group in hooks.get(event, []):
            for h in group.get("hooks", []):
                out.append(h.get("command", ""))
        return out

    assert any("capture observe" in c for c in commands("PostToolUse"))
    assert any("capture finalize" in c for c in commands("SessionEnd"))
    assert any("capture banner" in c for c in commands("SessionStart"))


def test_capture_finalize_all_cmd_with_old_buffers(tmp_path: Path, monkeypatch) -> None:
    """CLI command should finalize old buffers and emit JSON."""
    import os
    import time as time_mod

    store = _make_store(tmp_path)
    current_sess = "current"
    old_sess = "old-session"

    # Create old buffer
    old_path = cap.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    observations = [
        '{"ts": 1.0, "tool": "Read", "summary": "test1"}',
        '{"ts": 2.0, "tool": "Read", "summary": "test2"}',
        '{"ts": 3.0, "tool": "Read", "summary": "test3"}',
    ]
    old_path.write_text("\n".join(observations) + "\n")
    old_mtime = time_mod.time() - 7200
    os.utime(old_path, (old_mtime, old_mtime))

    # Create current buffer
    curr_path = cap.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')

    # Run the CLI command
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capture", "finalize-all",
        "--session-id", current_sess,
        "--max-age-seconds", "3600",
    ], env={"VOUCH_KB_PATH": str(store.kb_dir)})

    assert result.exit_code == 0
    output = _json.loads(result.output)
    assert old_sess in output["finalized"]
    assert current_sess in output["skipped_current"]


def test_capture_finalize_all_cmd_reads_session_from_env(tmp_path: Path, monkeypatch) -> None:
    """CLI command should fall back to VOUCH_SESSION_ID env var."""
    store = _make_store(tmp_path)
    current_sess = "from-env"

    # Create current session buffer
    curr_path = cap.buffer_path(store, current_sess)
    curr_path.parent.mkdir(parents=True, exist_ok=True)
    curr_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')

    runner = CliRunner()
    result = runner.invoke(cli, [
        "capture", "finalize-all"
    ], env={
        "VOUCH_KB_PATH": str(store.kb_dir),
        "VOUCH_SESSION_ID": current_sess,
    })

    assert result.exit_code == 0
    output = _json.loads(result.output)
    assert current_sess in output["skipped_current"]


def test_capture_finalize_all_cmd_silent_on_no_kb(tmp_path: Path, monkeypatch) -> None:
    """CLI command should silently succeed if KB not found."""
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capture", "finalize-all",
        "--session-id", "test",
    ], env={"VOUCH_KB_PATH": str(tmp_path / "nonexistent")})

    # Should exit 0, not fail
    assert result.exit_code == 0


def test_is_stale_buffer_with_recent_file(tmp_path):
    """Recent file should not be stale."""
    import time as time_mod
    f = tmp_path / "recent.jsonl"
    f.write_text("test")
    now = time_mod.time()
    # File created 30 seconds ago; max_age=3600
    assert not cap.is_stale_buffer(f, max_age_seconds=3600, now_timestamp=now)


def test_is_stale_buffer_with_old_file(tmp_path):
    """File older than max_age should be stale."""
    import os
    import time as time_mod
    f = tmp_path / "old.jsonl"
    f.write_text("test")
    old_time = time_mod.time() - 7200  # 2 hours ago
    os.utime(f, (old_time, old_time))  # Set mtime to 2 hours ago
    now = time_mod.time()
    assert cap.is_stale_buffer(f, max_age_seconds=3600, now_timestamp=now)


def test_is_stale_buffer_with_exact_boundary(tmp_path):
    """File at exact max_age boundary should not be stale (>=)."""
    import os
    import time as time_mod
    f = tmp_path / "boundary.jsonl"
    f.write_text("test")
    exact_time = time_mod.time() - 3600  # Exactly 1 hour ago
    os.utime(f, (exact_time, exact_time))
    now = exact_time + 3600
    assert not cap.is_stale_buffer(f, max_age_seconds=3600, now_timestamp=now)


def _make_store(tmp_path: Path) -> KBStore:
    """Helper to create a KBStore for testing."""
    return KBStore.init(tmp_path)


def test_finalize_all_except_skips_current_session(tmp_path):
    """Should not finalize the current session buffer."""
    store = _make_store(tmp_path)
    sess_id = "current-session"

    # Create a current session buffer with observations
    path = cap.buffer_path(store, sess_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')

    result = cap.finalize_all_except(
        store, sess_id, max_age_seconds=3600.0
    )

    assert result["skipped_current"] == [sess_id]
    assert path.exists()  # Not removed


def test_finalize_all_except_finalizes_old_buffer(tmp_path):
    """Should finalize buffers older than max_age, except current session."""
    import os
    import time as time_mod
    store = _make_store(tmp_path)
    current_sess = "current"
    old_sess = "old-session"

    # Create old buffer (2 hours old)
    old_path = cap.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
    old_mtime = time_mod.time() - 7200
    os.utime(old_path, (old_mtime, old_mtime))

    # Create current buffer (recent)
    curr_path = cap.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 2.0, "tool": "Write", "summary": "test2"}\n')

    result = cap.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0
    )

    assert old_sess in result["finalized"]
    assert current_sess in result["skipped_current"]
    assert not old_path.exists()  # Removed after finalize
    assert curr_path.exists()  # Current session untouched


def test_finalize_all_except_skips_recent_buffers(tmp_path):
    """Should not finalize buffers younger than max_age."""
    import os
    import time as time_mod
    store = _make_store(tmp_path)
    current_sess = "current"
    recent_sess = "recent-other"

    # Create recent buffer (30 minutes old)
    recent_path = cap.buffer_path(store, recent_sess)
    recent_path.parent.mkdir(parents=True, exist_ok=True)
    recent_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
    recent_mtime = time_mod.time() - 1800
    os.utime(recent_path, (recent_mtime, recent_mtime))

    result = cap.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0
    )

    assert recent_sess in result["skipped_recent"]
    assert recent_path.exists()  # Not removed


def test_finalize_all_except_multiple_buffers(tmp_path):
    """Should handle multiple old and recent buffers correctly."""
    import os
    import time as time_mod
    store = _make_store(tmp_path)
    current_sess = "current"

    # Create 3 old buffers, 2 recent buffers
    old_sesses = ["old1", "old2", "old3"]
    recent_sesses = ["recent1", "recent2"]

    now = time_mod.time()
    for sid in old_sesses:
        path = cap.buffer_path(store, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
        old_mtime = now - 7200  # 2 hours ago
        os.utime(path, (old_mtime, old_mtime))

    for sid in recent_sesses:
        path = cap.buffer_path(store, sid)
        path.write_text('{"ts": 2.0, "tool": "Read", "summary": "test"}\n')
        recent_mtime = now - 600  # 10 minutes ago
        os.utime(path, (recent_mtime, recent_mtime))

    # Create current session buffer
    curr_path = cap.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 3.0, "tool": "Write", "summary": "test"}\n')

    result = cap.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0, now_timestamp=now
    )

    assert set(result["finalized"]) == set(old_sesses)
    assert set(result["skipped_recent"]) == set(recent_sesses)
    assert result["skipped_current"] == [current_sess]

    # Verify old buffers are removed, others exist
    for sid in old_sesses:
        assert not cap.buffer_path(store, sid).exists()
    for sid in [*recent_sesses, current_sess]:
        assert cap.buffer_path(store, sid).exists()


def test_finalize_all_except_empty_captures_dir(tmp_path):
    """Should handle empty or missing captures directory gracefully."""
    store = _make_store(tmp_path)
    result = cap.finalize_all_except(
        store, "current-session", max_age_seconds=3600.0
    )

    assert result["finalized"] == []
    assert result["skipped_recent"] == []
    assert result["skipped_current"] == []


def test_finalize_all_except_returns_proposal_ids(tmp_path):
    """finalize_all_except should return proposal IDs of finalized buffers."""
    import os
    import time as time_mod
    store = _make_store(tmp_path)
    old_sess = "old-session"
    current_sess = "current"

    # Create old buffer with enough observations
    old_path = cap.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    observations = [
        '{"ts": 1.0, "tool": "Read", "summary": "test1"}',
        '{"ts": 2.0, "tool": "Read", "summary": "test2"}',
        '{"ts": 3.0, "tool": "Read", "summary": "test3"}',
    ]
    old_path.write_text("\n".join(observations) + "\n")
    old_mtime = time_mod.time() - 7200
    os.utime(old_path, (old_mtime, old_mtime))

    # Create current session buffer
    curr_path = cap.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 4.0, "tool": "Write", "summary": "test"}\n')

    result = cap.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0
    )

    assert old_sess in result["finalized"]
    # Verify a proposal was created
    from vouch.models import ProposalStatus
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) > 0


def test_capture_e2e_sessionstart_cleanup_then_finalize(tmp_path):
    """End-to-end: old buffers cleaned up on sessionstart, current session on finalize."""
    import os
    import time as time_mod

    store = _make_store(tmp_path)

    # Simulate a previous session that crashed/closed without finalize
    old_sess = "crashed-session"
    old_path = cap.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    observations = [
        '{"ts": 1.0, "tool": "Read", "summary": "test1"}',
        '{"ts": 2.0, "tool": "Read", "summary": "test2"}',
        '{"ts": 3.0, "tool": "Read", "summary": "test3"}',
    ]
    old_path.write_text("\n".join(observations) + "\n")
    old_mtime = time_mod.time() - 7200  # 2 hours ago
    os.utime(old_path, (old_mtime, old_mtime))

    # Simulate a new session starting
    new_sess = "new-session"

    # 1. SessionStart cleanup (finalize old buffers)
    cleanup_result = cap.finalize_all_except(
        store, new_sess, max_age_seconds=3600.0
    )
    assert old_sess in cleanup_result["finalized"]
    assert not old_path.exists()

    # Verify old session was proposed
    pending_before = store.list_proposals(ProposalStatus.PENDING)
    old_proposals = [p for p in pending_before if p.session_id == old_sess]
    assert len(old_proposals) == 1

    # 2. SessionEnd finalize (current session)
    new_path = cap.buffer_path(store, new_sess)
    new_path.write_text("\n".join(observations) + "\n")

    finalize_result = cap.finalize(store, new_sess)
    assert finalize_result["summary_proposal_id"] is not None
    assert not new_path.exists()

    # Verify new session was proposed
    pending_after = store.list_proposals(ProposalStatus.PENDING)
    new_proposals = [p for p in pending_after if p.session_id == new_sess]
    assert len(new_proposals) == 1

    # Total proposals: old + new
    assert len(pending_after) >= 2
