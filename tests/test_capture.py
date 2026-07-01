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
    assert "s1" in title
    assert "files modified this session" in body.lower()
    assert "## activity" in body.lower()
    assert "a.py" in body


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
