"""Tests for `vouch compile` — the llm-wiki ingest pass.

The LLM is always a stub here (`cat <canned.json>`), so the tests pin the
mechanical guarantees: drafts become PENDING proposals and never durable
pages, every citation is verified against the store, and one bad draft is
dropped with a reason instead of sinking the batch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import compile as compile_mod
from vouch.compile import CompileConfig, CompileError, compile_kb
from vouch.models import ProposalStatus
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _approved_claim(store: KBStore, text: str) -> str:
    src = store.put_source(text.encode())
    pr = propose_claim(store, text=text, evidence=[src.id], proposed_by="agent-A")
    claim = approve(store, pr.id, approved_by="human-B")
    return claim.id


def _stub_llm(tmp_path: Path, drafts: list[dict]) -> str:
    """Return an llm_cmd that ignores its stdin and emits canned drafts."""
    out = tmp_path / "drafts.json"
    out.write_text(json.dumps(drafts), encoding="utf-8")
    return f"cat {out}"


def _cfg(llm_cmd: str, **kwargs) -> CompileConfig:
    return CompileConfig(llm_cmd=llm_cmd, **kwargs)


# --- the gate stays intact --------------------------------------------------


def test_compile_files_pending_page_proposals_never_durable_pages(
    store: KBStore, tmp_path: Path,
) -> None:
    c1 = _approved_claim(store, "the retry limit is three")
    c2 = _approved_claim(store, "staging runs postgres sixteen")
    cmd = _stub_llm(tmp_path, [
        {
            "title": "Billing Retries",
            "type": "decision",
            "body": f"Retries cap at three [claim: {c1}]. See [[Staging Database]].",
            "claims": [c1],
        },
        {
            "title": "Staging Database",
            "type": "workflow",
            "body": f"Staging is on postgres 16 [claim: {c2}].",
            "claims": [c2],
        },
    ])

    report = compile_kb(store, config=_cfg(cmd))

    assert len(report.proposed) == 2
    assert report.dropped == []
    # nothing durable yet — compile only proposes.
    assert store.list_pages() == []
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert {p.kind.value for p in pending} == {"page"}
    assert all(p.proposed_by == compile_mod.COMPILE_ACTOR for p in pending)

    # a human approval (different actor) materialises the page.
    page = approve(store, report.proposed[0]["proposal_id"], approved_by="human-B")
    assert store.get_page(page.id).title == "Billing Retries"


def test_dry_run_files_nothing(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    cmd = _stub_llm(tmp_path, [
        {"title": "T", "type": "concept", "body": f"x [claim: {c1}]", "claims": [c1]},
    ])
    report = compile_kb(store, config=_cfg(cmd), dry_run=True)
    assert report.dry_run
    assert len(report.proposed) == 1
    assert store.list_proposals(ProposalStatus.PENDING) == []


# --- citation verification ---------------------------------------------------


def test_unknown_claim_id_drops_draft(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a real fact")
    cmd = _stub_llm(tmp_path, [
        {"title": "Ghost", "type": "concept", "body": "x", "claims": ["no-such-claim"]},
        {"title": "Real", "type": "concept", "body": f"y [claim: {c1}]", "claims": [c1]},
    ])
    report = compile_kb(store, config=_cfg(cmd))
    assert [r["title"] for r in report.proposed] == ["Real"]
    assert report.dropped[0]["title"] == "Ghost"
    assert "unknown claim" in report.dropped[0]["reason"]


def test_inline_marker_citing_unlisted_claim_drops_draft(
    store: KBStore, tmp_path: Path,
) -> None:
    c1 = _approved_claim(store, "listed fact")
    c2 = _approved_claim(store, "unlisted fact")
    cmd = _stub_llm(tmp_path, [
        {
            "title": "Sneaky",
            "type": "concept",
            "body": f"a [claim: {c1}] and b [claim: {c2}]",
            "claims": [c1],  # c2 cited inline but not linked
        },
    ])
    report = compile_kb(store, config=_cfg(cmd))
    assert report.proposed == []
    assert "unlisted claim" in report.dropped[0]["reason"]


def test_uncited_draft_drops(store: KBStore, tmp_path: Path) -> None:
    _approved_claim(store, "a fact so the KB is non-empty")
    cmd = _stub_llm(tmp_path, [
        {"title": "Vibes", "type": "concept", "body": "trust me", "claims": []},
    ])
    report = compile_kb(store, config=_cfg(cmd))
    assert report.proposed == []
    assert "cites no claims" in report.dropped[0]["reason"]


def test_unresolved_wikilink_drops_draft(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    cmd = _stub_llm(tmp_path, [
        {
            "title": "Linky",
            "type": "concept",
            "body": f"see [[No Such Page]] [claim: {c1}]",
            "claims": [c1],
        },
    ])
    report = compile_kb(store, config=_cfg(cmd))
    assert report.proposed == []
    assert "unresolved wikilink" in report.dropped[0]["reason"]


def test_wikilink_to_existing_page_resolves(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    pr = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "Anchor", "type": "concept", "body": f"x [claim: {c1}]",
         "claims": [c1]},
    ])))
    approve(store, pr.proposed[0]["proposal_id"], approved_by="human-B")

    report = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "Follower", "type": "concept",
         "body": f"see [[Anchor]] [claim: {c1}]", "claims": [c1]},
    ])))
    assert [r["title"] for r in report.proposed] == ["Follower"]


def test_session_and_log_types_are_rejected(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    cmd = _stub_llm(tmp_path, [
        {"title": "S", "type": "session", "body": f"x [claim: {c1}]", "claims": [c1]},
        {"title": "L", "type": "log", "body": f"x [claim: {c1}]", "claims": [c1]},
    ])
    report = compile_kb(store, config=_cfg(cmd))
    assert report.proposed == []
    assert len(report.dropped) == 2


def test_max_pages_cap(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    drafts = [
        {"title": f"P{i}", "type": "concept", "body": f"x [claim: {c1}]",
         "claims": [c1]}
        for i in range(4)
    ]
    report = compile_kb(store, config=_cfg(_stub_llm(tmp_path, drafts)), max_pages=2)
    assert len(report.proposed) == 2
    assert all("max_pages" in d["reason"] for d in report.dropped)


# --- failure shapes -----------------------------------------------------------


def test_missing_llm_cmd_raises(store: KBStore) -> None:
    _approved_claim(store, "a fact")
    with pytest.raises(CompileError, match="llm_cmd is not configured"):
        compile_kb(store, config=CompileConfig())


def test_empty_kb_raises_before_running_llm(store: KBStore) -> None:
    with pytest.raises(CompileError, match="nothing to compile"):
        compile_kb(store, config=_cfg("false"))


def test_non_positive_max_pages_raises(store: KBStore) -> None:
    # `false` as llm_cmd: the guard must fire before the LLM is spent
    _approved_claim(store, "a fact")
    with pytest.raises(CompileError, match="max_pages must be >= 1"):
        compile_kb(store, config=_cfg("false"), max_pages=0)


def test_llm_failure_raises(store: KBStore) -> None:
    _approved_claim(store, "a fact")
    with pytest.raises(CompileError, match="failed"):
        compile_kb(store, config=_cfg("false"))


def test_non_json_output_raises(store: KBStore) -> None:
    _approved_claim(store, "a fact")
    with pytest.raises(CompileError, match="not valid JSON"):
        compile_kb(store, config=_cfg("echo hello world"))


def test_fenced_json_is_tolerated(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    out = tmp_path / "fenced.txt"
    body = json.dumps([
        {"title": "F", "type": "concept", "body": f"x [claim: {c1}]", "claims": [c1]},
    ])
    out.write_text(f"```json\n{body}\n```", encoding="utf-8")
    report = compile_kb(store, config=_cfg(f"cat {out}"))
    assert [r["title"] for r in report.proposed] == ["F"]


# --- config -------------------------------------------------------------------


def test_load_config_reads_compile_stanza(store: KBStore) -> None:
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + "\ncompile:\n  llm_cmd: \"cat /dev/null\"\n  max_pages: 9\n",
        encoding="utf-8",
    )
    cfg = compile_mod.load_config(store)
    assert cfg.llm_cmd == "cat /dev/null"
    assert cfg.max_pages == 9


def test_load_config_defaults_when_absent(store: KBStore) -> None:
    cfg = compile_mod.load_config(store)
    assert cfg.llm_cmd is None
    assert cfg.max_pages == compile_mod.DEFAULT_MAX_PAGES


def test_load_config_bad_values_fall_back_to_defaults(store: KBStore) -> None:
    # a config typo must degrade, not 500 the web queue that reads this
    # config on every render.
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + "\ncompile:\n  llm_cmd: \"cat /dev/null\"\n"
          "  max_pages: five\n  timeout_seconds:\n",
        encoding="utf-8",
    )
    cfg = compile_mod.load_config(store)
    assert cfg.llm_cmd == "cat /dev/null"
    assert cfg.max_pages == compile_mod.DEFAULT_MAX_PAGES
    assert cfg.timeout_seconds == compile_mod.DEFAULT_TIMEOUT_SECONDS


# --- review-hardening regressions --------------------------------------------


def test_non_dict_array_elements_raise(store: KBStore, tmp_path: Path) -> None:
    _approved_claim(store, "a fact")
    out = tmp_path / "strings.json"
    out.write_text(json.dumps(["Page One", "Page Two"]), encoding="utf-8")
    with pytest.raises(CompileError, match="array of page objects"):
        compile_kb(store, config=_cfg(f"cat {out}"))


def test_collision_with_existing_page_dropped(
    store: KBStore, tmp_path: Path,
) -> None:
    """approve() routes an existing page id through update_page, so a
    colliding draft would silently overwrite the page — compile must drop
    it at validation time."""
    c1 = _approved_claim(store, "a fact")
    first = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "Deploy Workflow", "type": "workflow",
         "body": f"x [claim: {c1}]", "claims": [c1]},
    ])))
    approve(store, first.proposed[0]["proposal_id"], approved_by="human-B")
    before = store.get_page("deploy-workflow").body

    report = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "Deploy Workflow", "type": "workflow",
         "body": f"OVERWRITE [claim: {c1}]", "claims": [c1]},
    ])))
    assert report.proposed == []
    assert "already exists" in report.dropped[0]["reason"]
    assert store.get_page("deploy-workflow").body == before


def test_collision_with_pending_proposal_dropped(
    store: KBStore, tmp_path: Path,
) -> None:
    """re-running compile (or double-clicking the button) must not queue
    duplicate drafts of the same topic."""
    c1 = _approved_claim(store, "a fact")
    drafts = [{"title": "Retry Policy", "type": "decision",
               "body": f"x [claim: {c1}]", "claims": [c1]}]
    first = compile_kb(store, config=_cfg(_stub_llm(tmp_path, drafts)))
    assert len(first.proposed) == 1

    second = compile_kb(store, config=_cfg(_stub_llm(tmp_path, drafts)))
    assert second.proposed == []
    assert "pending review" in second.dropped[0]["reason"]


def test_dropped_sibling_dangles_wikilink_and_cascades(
    store: KBStore, tmp_path: Path,
) -> None:
    """A links [[B]]; B cites an unknown claim. B drops for the citation,
    and A must then drop too — filing A would ship a dangling link."""
    c1 = _approved_claim(store, "a fact")
    report = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "A", "type": "concept",
         "body": f"see [[B]] [claim: {c1}]", "claims": [c1]},
        {"title": "B", "type": "concept", "body": "x", "claims": ["no-such"]},
    ])))
    assert report.proposed == []
    reasons = {d["title"]: d["reason"] for d in report.dropped}
    assert "unknown claim" in reasons["B"]
    assert "unresolved wikilink" in reasons["A"]


def test_capped_sibling_dangles_wikilink(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    report = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "A", "type": "concept",
         "body": f"see [[B]] [claim: {c1}]", "claims": [c1]},
        {"title": "B", "type": "concept",
         "body": f"y [claim: {c1}]", "claims": [c1]},
    ])), max_pages=1)
    # B falls to the cap; A's [[B]] then dangles, so nothing survives.
    assert report.proposed == []


def test_unicode_body_survives(store: KBStore, tmp_path: Path) -> None:
    c1 = _approved_claim(store, "a fact")
    body = f"em-dash — and naïve café [claim: {c1}]"
    report = compile_kb(store, config=_cfg(_stub_llm(tmp_path, [
        {"title": "Unicode", "type": "concept", "body": body, "claims": [c1]},
    ])))
    assert len(report.proposed) == 1
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert pending[0].payload["body"] == body


def test_jsonl_kb_compile_files_proposals(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kb.compile over the JSONL wire — what vouch-ui calls — files pending
    page proposals and returns the report envelope."""
    from vouch.jsonl_server import handle_request

    c1 = _approved_claim(store, "a wire-visible fact")
    cmd = _stub_llm(tmp_path, [
        {"title": "Wire Topic", "type": "concept",
         "body": f"x [claim: {c1}]", "claims": [c1]},
    ])
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + f"\ncompile:\n  llm_cmd: \"{cmd}\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r1", "method": "kb.compile", "params": {}})
    assert resp["ok"]
    assert resp["result"]["proposed"][0]["title"] == "Wire Topic"
    assert store.list_pages() == []
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert pending and pending[0].kind.value == "page"


def test_jsonl_kb_compile_unconfigured_is_clean_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch.jsonl_server import handle_request

    _approved_claim(store, "a fact")
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r2", "method": "kb.compile", "params": {}})
    assert not resp["ok"]
    assert "llm_cmd is not configured" in resp["error"]["message"]


def test_compile_logs_attributed_audit_event(
    store: KBStore, tmp_path: Path,
) -> None:
    from vouch import audit as audit_mod

    c1 = _approved_claim(store, "a fact")
    cmd = _stub_llm(tmp_path, [
        {"title": "T", "type": "concept", "body": f"x [claim: {c1}]",
         "claims": [c1]},
    ])
    compile_kb(store, config=_cfg(cmd), triggered_by="human-reviewer")
    events = [e for e in audit_mod.read_events(store.kb_dir)
              if e.event == "compile.run"]
    assert len(events) == 1
    assert events[0].actor == "human-reviewer"
    assert events[0].data["proposed"] == 1

    # dry runs mutate nothing and log nothing.
    compile_kb(store, config=_cfg(cmd), dry_run=True, triggered_by="human-reviewer")
    events = [e for e in audit_mod.read_events(store.kb_dir)
              if e.event == "compile.run"]
    assert len(events) == 1
