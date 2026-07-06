"""LLM session summaries: config, record building, generation, enrichment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vouch import capture as cap
from vouch import summarize as summ
from vouch.models import ProposalStatus
from vouch.proposals import propose_page
from vouch.storage import KBStore, _starter_config

FAKE_LLM_OK = (
    "python3 -c 'import sys; sys.stdin.read(); "
    'print("- worked on the capture module")\''
)
FAKE_LLM_FAIL = "python3 -c 'raise SystemExit(1)'"
FAKE_LLM_EMPTY = "python3 -c 'import sys; sys.stdin.read()'"


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _configure(store: KBStore, *, mode: str = "auto", cmd: str = FAKE_LLM_OK) -> None:
    store.config_path.write_text(
        yaml.safe_dump({
            "capture": {
                "enabled": True,
                "min_observations": 3,
                "summary_mode": mode,
                "summary_llm_cmd": cmd,
            },
        })
    )


def _add_observations(store: KBStore, sid: str) -> None:
    cap.observe(store, sid, tool="Edit", summary="Edited capture.py", now=100.0)
    cap.observe(store, sid, tool="Bash", summary="Ran: pytest", cmd="pytest -q", now=200.0)
    cap.observe(store, sid, tool="Read", summary="Read storage.py", now=300.0)


def _pending_body(store: KBStore, proposal_id: str) -> str:
    proposal = store.get_proposal(proposal_id)
    assert proposal.status == ProposalStatus.PENDING
    return cap.proposal_body(proposal)


# --- config ---------------------------------------------------------------

def test_summary_config_defaults(store: KBStore) -> None:
    cfg = summ.load_summary_config(store)
    assert cfg.mode == "auto"
    assert cfg.llm_cmd == ""
    assert cfg.configured is False


def test_summary_config_override(store: KBStore) -> None:
    _configure(store, mode="manual", cmd="claude -p")
    cfg = summ.load_summary_config(store)
    assert cfg.mode == "manual"
    assert cfg.llm_cmd == "claude -p"
    assert cfg.configured is True


def test_summary_config_invalid_mode_falls_back(store: KBStore) -> None:
    _configure(store, mode="sometimes")
    assert summ.load_summary_config(store).mode == "auto"


def test_starter_config_has_summary_keys() -> None:
    capture_cfg = _starter_config()["capture"]
    assert capture_cfg["summary_mode"] == "auto"
    assert capture_cfg["summary_llm_cmd"] == ""


# --- generate ---------------------------------------------------------------

def test_generate_returns_stdout() -> None:
    cfg = summ.SummaryConfig(mode="auto", llm_cmd=FAKE_LLM_OK)
    assert summ.generate("record", cfg) == "- worked on the capture module"


def test_generate_none_on_failure_empty_or_unconfigured() -> None:
    assert summ.generate("r", summ.SummaryConfig(llm_cmd=FAKE_LLM_FAIL)) is None
    assert summ.generate("r", summ.SummaryConfig(llm_cmd=FAKE_LLM_EMPTY)) is None
    assert summ.generate("r", summ.SummaryConfig(llm_cmd="")) is None
    assert summ.generate("r", summ.SummaryConfig(mode="off", llm_cmd=FAKE_LLM_OK)) is None


def test_generate_disables_capture_in_llm_subprocess() -> None:
    """claude -p as the summarizer fires this repo's own hooks; the env
    kill-switch stops the summarize run from capturing itself."""
    env_echo = (
        "python3 -c 'import os, sys; sys.stdin.read(); "
        'print(os.environ.get("VOUCH_CAPTURE_OFF", "unset"))\''
    )
    out = summ.generate("r", summ.SummaryConfig(mode="auto", llm_cmd=env_echo))
    assert out == "1"


def test_capture_off_env_disables_observe(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOUCH_CAPTURE_OFF", "1")
    assert cap.load_config(store).enabled is False
    assert cap.observe(store, "sid", tool="Edit", summary="Edited a.py") is False
    assert not cap.buffer_path(store, "sid").exists()


# --- transcript excerpt -----------------------------------------------------

def test_transcript_excerpt_extracts_prose(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": "fix the locale bug"}}),
        json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "reading storage.py"},
            {"type": "tool_use", "name": "Read", "input": {}},
        ]}}),
        json.dumps({"type": "progress", "unrelated": True}),
        "not json",
    ]
    transcript.write_text("\n".join(lines))
    out = summ.read_transcript_excerpt(transcript, 10_000)
    assert "user: fix the locale bug" in out
    assert "assistant: reading storage.py" in out
    assert "tool_use" not in out


def test_transcript_excerpt_truncates_to_tail(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    rows = [
        json.dumps({"message": {"role": "user", "content": f"turn {i} " + "x" * 50}})
        for i in range(100)
    ]
    transcript.write_text("\n".join(rows))
    out = summ.read_transcript_excerpt(transcript, 500)
    assert out.startswith("[…transcript truncated…]")
    assert "turn 99" in out
    assert "turn 0 " not in out


def test_transcript_excerpt_missing_file() -> None:
    assert summ.read_transcript_excerpt(None, 100) == ""
    assert summ.read_transcript_excerpt(Path("/nonexistent/t.jsonl"), 100) == ""


# --- section insertion --------------------------------------------------------

def test_insert_section_before_first_heading() -> None:
    body = "# title\n\n- session: `s1`\n\n## what happened\n\n- edited a.py\n"
    out = summ.insert_summary_section(body, "narrative here")
    assert out.index(summ.SUMMARY_SECTION_HEADER) < out.index("## what happened")
    assert "narrative here" in out


def test_insert_section_replaces_previous(store: KBStore) -> None:
    body = "# title\n\n## what happened\n\n- edited a.py\n"
    once = summ.insert_summary_section(body, "first")
    twice = summ.insert_summary_section(once, "second")
    assert twice.count(summ.SUMMARY_SECTION_HEADER) == 1
    assert "second" in twice
    assert "first" not in twice
    assert "## what happened" in twice


def test_insert_section_appends_when_no_headings() -> None:
    out = summ.insert_summary_section("# title\n\n- meta\n", "n")
    assert out.rstrip().endswith("n")
    assert summ.SUMMARY_SECTION_HEADER in out


# --- finalize integration -----------------------------------------------------

def test_finalize_auto_adds_ai_summary(store: KBStore) -> None:
    _configure(store, mode="auto")
    _add_observations(store, "s1")
    result = cap.finalize(store, "s1")
    assert result["ai_summary"] is True
    body = _pending_body(store, result["summary_proposal_id"])
    assert summ.SUMMARY_SECTION_HEADER in body
    assert "- worked on the capture module" in body


def test_finalize_survives_llm_failure(store: KBStore) -> None:
    _configure(store, mode="auto", cmd=FAKE_LLM_FAIL)
    _add_observations(store, "s1")
    result = cap.finalize(store, "s1")
    assert result["summary_proposal_id"] is not None
    assert result["ai_summary"] is False
    assert summ.SUMMARY_SECTION_HEADER not in _pending_body(
        store, result["summary_proposal_id"]
    )


def test_finalize_manual_mode_skips_llm(store: KBStore) -> None:
    _configure(store, mode="manual")
    _add_observations(store, "s1")
    result = cap.finalize(store, "s1")
    assert result["ai_summary"] is False
    assert summ.SUMMARY_SECTION_HEADER not in _pending_body(
        store, result["summary_proposal_id"]
    )


def test_finalize_allow_llm_false_skips(store: KBStore) -> None:
    _configure(store, mode="auto")
    _add_observations(store, "s1")
    result = cap.finalize(store, "s1", allow_llm=False)
    assert result["ai_summary"] is False


# --- manual / backlog enrichment ----------------------------------------------

def _captured_pending_id(store: KBStore, sid: str = "s1") -> str:
    _add_observations(store, sid)
    result = cap.finalize(store, sid)
    pid = result["summary_proposal_id"]
    assert pid is not None
    return str(pid)


def test_summarize_pending_enriches(store: KBStore) -> None:
    _configure(store, mode="manual")
    pid = _captured_pending_id(store)
    result = summ.summarize_pending(store, pid)
    assert result["summarized"] is True
    body = _pending_body(store, pid)  # asserts still PENDING — gate intact
    assert summ.SUMMARY_SECTION_HEADER in body


def test_summarize_pending_skips_non_capture_proposals(store: KBStore) -> None:
    _configure(store, mode="manual")
    proposal = propose_page(
        store, title="hand-written session note", body="# note\n",
        page_type="session", proposed_by="human",
    )
    result = summ.summarize_pending(store, proposal.id)
    assert result["summarized"] is False
    assert result["skipped"] == "not-a-captured-session"


def test_summarize_pending_handles_legacy_capture_pages(store: KBStore) -> None:
    """Pre-claim captures filed PAGE proposals; they must still enrich."""
    _configure(store, mode="manual")
    proposal = propose_page(
        store, title="session summary", body="# session\n\n## files\n\n- a.py\n",
        page_type="session", proposed_by=cap.CAPTURE_ACTOR,
        session_id="legacy-sid",
    )
    result = summ.summarize_pending(store, proposal.id)
    assert result["summarized"] is True
    refreshed = store.get_proposal(proposal.id)
    assert summ.SUMMARY_SECTION_HEADER in str(refreshed.payload["body"])


def test_summarize_pending_unconfigured(store: KBStore) -> None:
    pid = _captured_pending_id(store)
    result = summ.summarize_pending(store, pid)
    assert result["summarized"] is False
    assert result["skipped"] == "not-configured"


def test_summarize_all_pending_only_missing(store: KBStore) -> None:
    _configure(store, mode="manual")
    pid1 = _captured_pending_id(store, "s1")
    pid2 = _captured_pending_id(store, "s2")
    first = summ.summarize_all_pending(store)
    assert sorted(first["summarized"]) == sorted([pid1, pid2])
    second = summ.summarize_all_pending(store)
    assert second["summarized"] == []
    assert all(s["skipped"] == "already-summarized" for s in second["skipped"])


# --- sweep (tab-close catch-all) ----------------------------------------------


def test_sweep_finalizes_stale_and_enriches_only_new(store: KBStore) -> None:
    """finalize_all_except reports the proposals it filed; the sweep enriches
    exactly those, leaving the pre-existing backlog untouched."""
    _configure(store, mode="auto")
    # pre-existing backlog page WITHOUT an ai summary (llm-off finalize)
    _add_observations(store, "old-session")
    backlog = cap.finalize(store, "old-session", allow_llm=False)
    backlog_id = backlog["summary_proposal_id"]

    # a stale buffer left behind by a closed tab
    _add_observations(store, "closed-tab")
    result = cap.finalize_all_except(
        store, "live-session", max_age_seconds=0.0,
        now_timestamp=cap.time.time() + 10,
    )
    assert result["finalized"] == ["closed-tab"]
    assert len(result["finalized_proposals"]) == 1
    swept_id = result["finalized_proposals"][0]
    assert swept_id != backlog_id

    # enrich only what the sweep created (`vouch capture sweep --summarize`)
    cfg = summ.load_summary_config(store)
    one = summ.summarize_pending(store, swept_id, config=cfg)
    assert one["summarized"] is True
    assert summ.SUMMARY_SECTION_HEADER in _pending_body(store, swept_id)
    # backlog stays mechanical-only until --backlog / summarize --all
    assert summ.SUMMARY_SECTION_HEADER not in _pending_body(store, backlog_id)


def test_summarize_by_session_finds_proposal_and_transcript(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given only a claude session id: buffer is finalized, the transcript is
    auto-located under ~/.claude/projects/, and its prose reaches the LLM."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sid = "3cd62baa-dead-beef-8522-2fde434541ae"
    tdir = tmp_path / "home" / ".claude" / "projects" / "-some-workspace"
    tdir.mkdir(parents=True)
    (tdir / f"{sid}.jsonl").write_text(
        json.dumps({"message": {"role": "assistant",
                                "content": "marker-from-transcript"}}) + "\n"
    )
    # llm that proves what it saw on stdin
    echo_cmd = (
        "python3 -c 'import sys; d = sys.stdin.read(); "
        'print("saw-transcript" if "marker-from-transcript" in d else "blind")\''
    )
    _configure(store, mode="auto", cmd=echo_cmd)
    _add_observations(store, sid)  # buffer still open, as after a tab close

    result = summ.summarize_by_session(store, sid)
    assert result["summarized"] is True
    body = _pending_body(store, str(result["proposal_id"]))
    assert "saw-transcript" in body  # transcript reached the record


def test_summarize_by_session_unknown_id(store: KBStore) -> None:
    _configure(store, mode="auto")
    result = summ.summarize_by_session(store, "no-such-session")
    assert result["summarized"] is False
    assert result["skipped"] == "no-pending-summary-for-session"


def test_sweep_skips_current_session_buffer(store: KBStore) -> None:
    _configure(store, mode="auto")
    _add_observations(store, "live-session")
    result = cap.finalize_all_except(
        store, "live-session", max_age_seconds=0.0,
        now_timestamp=cap.time.time() + 10,
    )
    assert result["skipped_current"] == ["live-session"]
    assert result["finalized"] == []
    assert cap.buffer_path(store, "live-session").exists()
