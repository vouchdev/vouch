"""Host-blind session summarization: size gate, mechanical rollup, LLM split."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import session_split
from vouch.session_split import SplitConfig, load_split_config
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_split_config_defaults(store: KBStore) -> None:
    cfg = load_split_config(store)
    assert cfg == SplitConfig()
    assert cfg.threshold_observations == 40
    assert cfg.max_pages == 6
    assert cfg.enabled is True


def test_split_config_reads_override(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n    threshold_observations: 5\n    max_pages: 2\n"
        "    llm_cmd: \"cat /dev/null\"\n",
        encoding="utf-8",
    )
    cfg = load_split_config(store)
    assert cfg.threshold_observations == 5
    assert cfg.max_pages == 2
    assert cfg.llm_cmd == "cat /dev/null"


def test_split_config_malformed_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  split:\n  - not-a-mapping\n", encoding="utf-8")
    assert load_split_config(store) == SplitConfig()


def test_split_config_typo_coerces_to_default(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n    max_pages: six\n", encoding="utf-8"
    )
    assert load_split_config(store).max_pages == 6


def _observe(store: KBStore, sid: str, n: int, tool: str = "Edit") -> None:
    from vouch import capture
    for i in range(n):
        capture.observe(store, sid, tool=tool, summary=f"{tool} file{i}.py", now=float(i))


def test_below_min_skips_and_deletes_buffer(store: KBStore) -> None:
    from vouch import capture
    capture.observe(store, "s1", tool="Edit", summary="one", now=1.0)
    res = session_split.summarize(store, "s1")
    assert res["skipped"] == "below-min"
    assert res["summary_proposal_ids"] == []
    assert not capture.buffer_path(store, "s1").exists()


def test_disabled_returns_skip(store: KBStore) -> None:
    from vouch import capture
    _observe(store, "s1", 5)
    cfg = capture.CaptureConfig(enabled=False)
    res = session_split.summarize(store, "s1", config=cfg)
    assert res["skipped"] == "disabled"


def test_mechanical_single_page_below_threshold(store: KBStore) -> None:
    from vouch.models import ProposalStatus
    _observe(store, "s1", 5)  # >= min (3), < threshold (40)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "mechanical"
    assert len(res["summary_proposal_ids"]) == 1
    assert res["summary_proposal_id"] == res["summary_proposal_ids"][0]
    # a bare mechanical session rollup is auto-rejected by the admission gate
    prop = store.get_proposal(res["summary_proposal_ids"][0])
    assert prop.status is ProposalStatus.REJECTED
    assert prop.decided_by == "vouch-admission"
    assert prop.payload["type"] == "session"
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_finalize_still_returns_summary_proposal_id(store: KBStore) -> None:
    from vouch import capture
    _observe(store, "s1", 5)
    res = capture.finalize(store, "s1", cwd=None, generated_at="2026-07-09T00:00:00Z")
    assert "summary_proposal_id" in res
    assert res["summary_proposal_id"] is not None
    assert res["mode"] == "mechanical"


def _stub_llm(tmp_path: Path, drafts: list[dict]) -> str:
    out = tmp_path / "drafts.json"
    out.write_text(json.dumps(drafts), encoding="utf-8")
    return f"cat {out}"


def _config_with_split(
    store: KBStore, llm_cmd: str, threshold: int = 3, max_pages: int = 6
) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n"
        f"    threshold_observations: {threshold}\n"
        f"    max_pages: {max_pages}\n"
        f"    llm_cmd: \"{llm_cmd}\"\n",
        encoding="utf-8",
    )


def test_split_files_multiple_pending_session_pages(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "refactored the audit writer", "body": "one thread of work " * 10},
        {"title": "fixed the ci locale bug", "body": "another thread of work " * 10},
    ])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "split"
    assert len(res["summary_proposal_ids"]) == 2
    # both split pages are uncited type:session from an auto-capture actor, so
    # the admission gate auto-rejects them — nothing pending, nothing durable.
    rejected = [store.get_proposal(pid) for pid in res["summary_proposal_ids"]]
    assert all(p.status is ProposalStatus.REJECTED for p in rejected)
    assert all(p.payload["type"] == "session" for p in rejected)
    assert all(p.proposed_by == session_split.SPLIT_ACTOR for p in rejected)
    assert store.list_proposals(ProposalStatus.PENDING) == []
    assert store.list_pages() == []  # nothing durable


def test_split_forces_session_type_even_if_llm_says_concept(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "a topic", "type": "concept", "body": "body " * 20},
    ])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    # type is forced to session even though the llm said concept — which is why
    # the admission gate then auto-rejects it as an uncited session page.
    prop = store.get_proposal(res["summary_proposal_ids"][0])
    assert prop.payload["type"] == "session"
    assert prop.status is ProposalStatus.REJECTED


def test_no_llm_cmd_falls_back_to_mechanical(store: KBStore) -> None:
    _observe(store, "s1", 50)  # over default threshold 40
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "fallback"
    assert len(res["summary_proposal_ids"]) == 1


def test_junk_llm_output_falls_back(store: KBStore) -> None:
    _observe(store, "s1", 5)
    _config_with_split(store, "echo not-json", threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "fallback"
    assert len(res["summary_proposal_ids"]) == 1


def test_dedupe_drops_colliding_title(store: KBStore, tmp_path: Path) -> None:
    from vouch.proposals import approve, propose_page
    pr = propose_page(store, title="Existing Topic", body="b", page_type="concept", proposed_by="a")
    approve(store, pr.id, approved_by="human-B")
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "Existing Topic", "body": "dup " * 20},
        {"title": "Fresh Topic", "body": "fresh " * 20},
    ])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert len(res["summary_proposal_ids"]) == 1
    assert any(d["reason"].startswith("title already") for d in res["dropped"])


def test_cap_enforced(store: KBStore, tmp_path: Path) -> None:
    _observe(store, "s1", 5)
    drafts = [{"title": f"topic {i}", "body": "x " * 20} for i in range(5)]
    cmd = _stub_llm(tmp_path, drafts)
    _config_with_split(store, cmd, threshold=3, max_pages=2)
    res = session_split.summarize(store, "s1", mode="auto")
    assert len(res["summary_proposal_ids"]) == 2
    assert len([d for d in res["dropped"] if "over max_pages" in d["reason"]]) == 3


def test_host_neutral_tool_names_do_not_crash(store: KBStore, tmp_path: Path) -> None:
    from vouch import capture
    for i, tool in enumerate(["fs.write", "shell.exec", "browser.open"]):
        capture.observe(store, "s1", tool=tool, summary=f"{tool} did thing {i}", now=float(i))
    capture.observe(store, "s1", tool="fs.write", summary="one more", now=9.0)
    cmd = _stub_llm(tmp_path, [{"title": "the work", "body": "did things " * 15}])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "split"


def test_truncation_flagged_when_over_budget(store: KBStore, tmp_path: Path) -> None:
    from vouch import capture
    # distinct summaries so capture.observe's dedup window does not collapse them
    for i in range(50):
        capture.observe(store, "s1", tool="Edit", summary=f"edit {i} " + "x" * 200, now=float(i))
    cmd = _stub_llm(tmp_path, [{"title": "t", "body": "b " * 20}])
    store.config_path.write_text(
        "capture:\n  split:\n    threshold_observations: 3\n"
        "    max_input_chars: 500\n"
        f"    llm_cmd: \"{cmd}\"\n",
        encoding="utf-8",
    )
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["truncated"] is True


def test_kb_summarize_session_in_capabilities_and_handlers() -> None:
    from vouch import capabilities
    from vouch.jsonl_server import HANDLERS
    assert "kb.summarize_session" in capabilities.METHODS
    assert "kb.summarize_session" in HANDLERS


def test_jsonl_handler_summarizes(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    import vouch.jsonl_server as js
    _observe(store, "s1", 5)
    monkeypatch.setattr(js, "_store", lambda: store)
    res = js.HANDLERS["kb.summarize_session"]({"session_id": "s1"})
    assert res["mode"] == "mechanical"
    assert res["summary_proposal_id"] is not None


def test_starter_config_has_split_defaults() -> None:
    from vouch.storage import _starter_config
    split = _starter_config()["capture"]["split"]
    assert split["threshold_observations"] == 40
    assert split["enabled"] is True


# --- kb.list_sessions: the session-review pipeline view --------------------


def test_build_session_rows_lists_open_buffer(store: KBStore) -> None:
    _observe(store, "sess-open", 3)
    rows = session_split.build_session_rows(store)
    row = next(r for r in rows if r["session_id"] == "sess-open")
    assert row["stage"] == "buffer"
    assert row["summarized"] is False
    assert row["observations"] == 3
    assert row["proposal_id"] is None
    assert row["last_activity"] is not None


def test_build_session_rows_omits_auto_rejected_mechanical_summary(store: KBStore) -> None:
    from vouch import capture
    _observe(store, "sess-filed", 5)
    capture.finalize(store, "sess-filed", cwd=None, generated_at="2026-07-09T00:00:00Z")
    # the mechanical rollup is auto-rejected (uncited session page), so the
    # session no longer surfaces as a pending row awaiting narration.
    rows = session_split.build_session_rows(store)
    assert not any(
        r["session_id"] == "sess-filed" and r["stage"] == "pending" for r in rows
    )


def test_build_session_rows_split_proposal_is_summarized(store: KBStore, tmp_path: Path) -> None:
    _observe(store, "sess-split", 5)
    cmd = _stub_llm(tmp_path, [{"title": "the work thread", "body": "narrative " * 20}])
    _config_with_split(store, cmd, threshold=3)
    session_split.summarize(store, "sess-split", mode="auto")
    rows = session_split.build_session_rows(store)
    assert all(r["summarized"] for r in rows if r["session_id"] == "sess-split")


def test_finalized_session_not_listed_after_auto_reject(store: KBStore) -> None:
    from vouch import capture
    _observe(store, "sess-x", 5)
    capture.finalize(store, "sess-x", cwd=None, generated_at="2026-07-09T00:00:00Z")
    # buffer consumed by finalize + mechanical summary auto-rejected → the
    # session is neither a live buffer nor a pending proposal.
    rows = [r for r in session_split.build_session_rows(store) if r["session_id"] == "sess-x"]
    assert rows == []


def test_summarize_returns_webapp_keys_on_split(store: KBStore, tmp_path: Path) -> None:
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [{"title": "did the work", "body": "narrative " * 20}])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["session_id"] == "s1"
    assert res["summarized"] is True
    assert res["proposal_id"] == res["summary_proposal_ids"][0]


def test_summarize_webapp_keys_on_skip(store: KBStore) -> None:
    from vouch import capture
    capture.observe(store, "s1", tool="Edit", summary="one", now=1.0)
    res = session_split.summarize(store, "s1")
    assert res["summarized"] is False
    assert res["session_id"] == "s1"
    assert res["proposal_id"] is None


def test_summarize_fallback_flags_llm_failed(store: KBStore) -> None:
    _observe(store, "s1", 50)  # over default threshold, no llm_cmd configured
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "fallback"
    assert res["summarized"] is False
    assert res["skipped"] == "llm-failed"


def test_renarrate_is_noop_when_mechanical_summary_auto_rejected(
    store: KBStore, tmp_path: Path
) -> None:
    from vouch import capture
    from vouch.models import ProposalStatus
    _observe(store, "sess-m", 5)
    res0 = capture.finalize(store, "sess-m", cwd=None, generated_at="2026-07-09T00:00:00Z")
    mech_id = res0["summary_proposal_id"]
    # the mechanical rollup is auto-rejected at filing (uncited session page)
    assert store.get_proposal(mech_id).status is ProposalStatus.REJECTED
    assert not capture.buffer_path(store, "sess-m").exists()  # buffer gone

    cmd = _stub_llm(tmp_path, [
        {"title": "narrated: the parser work", "body": "narrative prose " * 15},
    ])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "sess-m", mode="auto")

    # no pending mechanical summary survives, so renarrate has nothing to act on
    assert res["summarized"] is False
    assert res["skipped"] == "no-pending-summary-for-session"
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_renarrate_without_llm_after_auto_reject(store: KBStore) -> None:
    from vouch import capture
    from vouch.models import ProposalStatus
    _observe(store, "sess-m", 5)
    res0 = capture.finalize(store, "sess-m", cwd=None, generated_at="2026-07-09T00:00:00Z")
    mech_id = res0["summary_proposal_id"]
    assert store.get_proposal(mech_id).status is ProposalStatus.REJECTED
    res = session_split.summarize(store, "sess-m", mode="auto")  # no llm_cmd
    assert res["summarized"] is False
    # the mechanical summary was already auto-rejected; nothing left to narrate
    assert store.get_proposal(mech_id).status is ProposalStatus.REJECTED


def test_summarize_no_buffer_no_proposal_skips(store: KBStore) -> None:
    res = session_split.summarize(store, "never-seen", mode="auto")
    assert res["summarized"] is False
    assert res["skipped"] == "no-pending-summary-for-session"


def test_kb_list_sessions_registered_and_returns_sessions(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vouch.jsonl_server as js
    from vouch import capabilities
    assert "kb.list_sessions" in capabilities.METHODS
    assert "kb.list_sessions" in js.HANDLERS
    _observe(store, "sess-open", 3)
    monkeypatch.setattr(js, "_store", lambda: store)
    res = js.HANDLERS["kb.list_sessions"]({})
    assert "sessions" in res
    assert any(s["session_id"] == "sess-open" for s in res["sessions"])
