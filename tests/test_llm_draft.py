"""Shared LLM drafting plumbing used by compile and session_split."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch.llm_draft import LLMDraftError, parse_drafts, run_llm


def _stub(tmp_path: Path, payload: str) -> str:
    out = tmp_path / "out.txt"
    out.write_text(payload, encoding="utf-8")
    return f"cat {out}"


def test_run_llm_returns_stdout(tmp_path: Path) -> None:
    cmd = _stub(tmp_path, '[{"title": "x", "body": "y"}]')
    assert run_llm(cmd, "prompt", timeout_seconds=10.0).strip().startswith("[")


def test_run_llm_nonzero_raises_with_label(tmp_path: Path) -> None:
    with pytest.raises(LLMDraftError, match=r"capture\.split\.llm_cmd failed"):
        run_llm("false", "p", timeout_seconds=10.0, label="capture.split.llm_cmd")


def test_run_llm_timeout_raises(tmp_path: Path) -> None:
    with pytest.raises(LLMDraftError, match="timed out"):
        run_llm("sleep 5", "p", timeout_seconds=0.2)


def test_parse_drafts_strips_fence() -> None:
    raw = '```json\n[{"title": "a", "body": "b"}]\n```'
    assert parse_drafts(raw) == [{"title": "a", "body": "b"}]


def test_parse_drafts_bad_json_raises() -> None:
    with pytest.raises(LLMDraftError, match="not valid JSON"):
        parse_drafts("not json")


def test_parse_drafts_non_list_raises() -> None:
    with pytest.raises(LLMDraftError, match="must be a JSON array"):
        parse_drafts('{"title": "a"}')


def test_parse_drafts_non_dict_element_raises() -> None:
    with pytest.raises(LLMDraftError, match="array of page objects"):
        parse_drafts('["just a string"]')
