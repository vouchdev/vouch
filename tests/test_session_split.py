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
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].payload["type"] == "session"


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


def _config_with_split(store: KBStore, llm_cmd: str, threshold: int = 3, max_pages: int = 6) -> None:
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
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 2
    assert all(p.payload["type"] == "session" for p in pending)
    assert all(p.proposed_by == session_split.SPLIT_ACTOR for p in pending)
    assert store.list_pages() == []  # nothing durable — only proposed


def test_split_forces_session_type_even_if_llm_says_concept(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "a topic", "type": "concept", "body": "body " * 20},
    ])
    _config_with_split(store, cmd, threshold=3)
    session_split.summarize(store, "s1", mode="auto")
    assert store.list_proposals(ProposalStatus.PENDING)[0].payload["type"] == "session"


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
