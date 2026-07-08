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
