"""Tests for `vouch enrich-page` / `enrich-pages` — thin-page enrichment.

Mirrors test_compile.py's shape: the mechanical guarantees under test are
that enrichment only ever proposes (never writes a durable page directly),
every added sentence carries a resolvable `[claim_id]` citation, and
thin-page selection respects the configured thresholds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import capabilities, enrich, health
from vouch.enrich import EnrichmentConfig
from vouch.jsonl_server import HANDLERS, handle_request
from vouch.models import ProposalStatus
from vouch.proposals import approve, propose_claim, propose_page
from vouch.storage import ArtifactNotFoundError, KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _approved_claim(store: KBStore, text: str) -> str:
    src = store.put_source(text.encode())
    pr = propose_claim(store, text=text, evidence=[src.id], proposed_by="agent-A")
    claim = approve(store, pr.id, approved_by="human-B")
    return claim.id


def _approved_page(
    store: KBStore, title: str, body: str = "", *, claim_ids: list[str] | None = None,
):
    pr = propose_page(
        store, title=title, body=body, claim_ids=claim_ids or [], proposed_by="agent-A",
    )
    return approve(store, pr.id, approved_by="human-B")


# --- thin-page selection -----------------------------------------------------


def test_thin_by_short_body_gets_enrichment_proposal(store: KBStore) -> None:
    c1 = _approved_claim(store, "retries are capped at three attempts")
    c2 = _approved_claim(store, "retries use exponential backoff timing")
    page = _approved_page(store, "Retries", "A stub.")
    health.rebuild_index(store)

    result = enrich.enrich_page(store, page.id, min_body_chars=200, min_citations=2)

    assert result.proposal_id is not None
    assert result.skipped_reason is None
    assert set(result.claim_ids) >= {c1, c2}
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].kind.value == "page"
    assert pending[0].payload["id"] == page.id


def test_thin_by_few_citations_gets_enrichment_proposal(store: KBStore) -> None:
    c1 = _approved_claim(store, "deploys run through the staging gate")
    long_body = "This page already has plenty of prose. " * 10
    page = _approved_page(store, "Deploys", long_body)
    health.rebuild_index(store)

    result = enrich.enrich_page(store, page.id, min_body_chars=50, min_citations=2)

    assert result.proposal_id is not None
    assert c1 in result.claim_ids


def test_page_above_thresholds_is_skipped(store: KBStore) -> None:
    c1 = _approved_claim(store, "a fact")
    c2 = _approved_claim(store, "another fact")
    page = _approved_page(store, "Solid Page", "Plenty of body text here.",
                          claim_ids=[c1, c2])
    health.rebuild_index(store)

    result = enrich.enrich_page(store, page.id, min_body_chars=10, min_citations=2)

    assert result.proposal_id is None
    assert result.skipped_reason == "page is above the thin-page thresholds"
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_page_with_no_related_claims_is_skipped(store: KBStore) -> None:
    # an unrelated claim exists so the kb isn't empty, but nothing matches
    # this page's topic.
    _approved_claim(store, "kubernetes networking uses cni plugins")
    page = _approved_page(store, "Payroll Rounding", "x")
    health.rebuild_index(store)

    result = enrich.enrich_page(store, page.id)

    assert result.proposal_id is None
    assert result.skipped_reason == "no related approved claims found"
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_missing_page_raises(store: KBStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        enrich.enrich_page(store, "no-such-page")


# --- propose, never write ----------------------------------------------------


def test_enrichment_never_writes_durably_until_approved(store: KBStore) -> None:
    _approved_claim(store, "billing retries cap at three attempts")
    page = _approved_page(store, "Billing", "x")
    health.rebuild_index(store)
    before = store.get_page(page.id).body

    result = enrich.enrich_page(store, page.id)

    assert store.get_page(page.id).body == before
    assert result.proposal_id is not None

    # a human approval (different actor) revises the existing page in place.
    revised = approve(store, result.proposal_id, approved_by="human-C")
    assert revised.id == page.id
    assert store.get_page(page.id).body != before
    assert len(store.list_pages()) == 1  # revision, not a duplicate page


def test_dry_run_files_nothing(store: KBStore) -> None:
    _approved_claim(store, "retries are capped at three attempts")
    page = _approved_page(store, "Retries", "A stub.")
    health.rebuild_index(store)

    result = enrich.enrich_page(store, page.id, dry_run=True)

    assert result.dry_run
    assert result.claim_ids
    assert store.list_proposals(ProposalStatus.PENDING) == []


# --- citation traceability ----------------------------------------------------


def test_every_added_sentence_carries_a_resolvable_citation(store: KBStore) -> None:
    ids = [
        _approved_claim(store, "auth uses short-lived jwt access tokens"),
        _approved_claim(store, "auth refresh tokens rotate on every use"),
    ]
    page = _approved_page(store, "Auth", "A stub about auth.")
    health.rebuild_index(store)

    result = enrich.enrich_page(store, page.id, dry_run=True)

    assert set(result.claim_ids) & set(ids)
    result2 = enrich.enrich_page(store, page.id)
    proposal = store.get_proposal(result2.proposal_id)
    body = proposal.payload["body"]
    assert "A stub about auth." in body  # original prose preserved
    for cid in result2.claim_ids:
        assert f"[{cid}]" in body
        assert store.get_claim(cid).id == cid


# --- config -------------------------------------------------------------------


def test_load_config_reads_enrichment_stanza(store: KBStore) -> None:
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + "\nenrichment:\n  min_body_chars: 500\n  min_citations: 4\n",
        encoding="utf-8",
    )
    cfg = enrich.load_config(store)
    assert cfg.min_body_chars == 500
    assert cfg.min_citations == 4


def test_load_config_defaults_when_absent(store: KBStore) -> None:
    cfg = enrich.load_config(store)
    assert cfg.min_body_chars == enrich.DEFAULT_MIN_BODY_CHARS
    assert cfg.min_citations == enrich.DEFAULT_MIN_CITATIONS


def test_load_config_bad_values_fall_back_to_defaults(store: KBStore) -> None:
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8")
        + "\nenrichment:\n  min_body_chars: two-hundred\n  min_citations:\n",
        encoding="utf-8",
    )
    cfg = enrich.load_config(store)
    assert cfg.min_body_chars == enrich.DEFAULT_MIN_BODY_CHARS
    assert cfg.min_citations == enrich.DEFAULT_MIN_CITATIONS


def test_is_thin_either_threshold(store: KBStore) -> None:
    cfg = EnrichmentConfig(min_body_chars=100, min_citations=2)
    page = _approved_page(store, "Short", "x" * 50)
    assert enrich.is_thin(page, cfg)

    c1 = _approved_claim(store, "fact one")
    c2 = _approved_claim(store, "fact two")
    page2 = _approved_page(store, "LongButUncited", "x" * 200)
    assert enrich.is_thin(page2, cfg)  # 0 citations < 2

    page3 = _approved_page(store, "CitedButShort", "x" * 50, claim_ids=[c1, c2])
    assert enrich.is_thin(page3, cfg)  # body still short

    page4 = _approved_page(store, "Solid", "x" * 200, claim_ids=[c1, c2])
    assert not enrich.is_thin(page4, cfg)


# --- batch pass: enrich_pages -------------------------------------------------


def test_enrich_pages_dry_run_lists_without_filing(store: KBStore) -> None:
    _approved_claim(store, "retries are capped at three attempts")
    solid_claim = _approved_claim(store, "solid page fact")
    thin = _approved_page(store, "Retries", "A stub.")
    solid = _approved_page(
        store, "Solid", "Plenty of prose here to clear the threshold easily.",
        claim_ids=[solid_claim],
    )
    health.rebuild_index(store)

    report = enrich.enrich_pages(store, min_body_chars=20, min_citations=1, dry_run=True)

    assert report.dry_run
    proposed_ids = {row["page_id"] for row in report.proposed}
    assert thin.id in proposed_ids
    assert solid.id not in proposed_ids
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_enrich_pages_respects_limit(store: KBStore) -> None:
    _approved_claim(store, "a shared fact about the system")
    pages = [
        _approved_page(store, f"Stub {i}", "x") for i in range(3)
    ]
    health.rebuild_index(store)

    report = enrich.enrich_pages(store, min_body_chars=200, min_citations=2, limit=1)

    assert len(report.proposed) + len(report.skipped) == 1
    assert {p.id for p in pages}  # sanity: pages exist


def test_enrich_pages_logs_audit_event(store: KBStore) -> None:
    from vouch import audit as audit_mod

    _approved_claim(store, "a fact for the audit test")
    _approved_page(store, "Stub", "x")
    health.rebuild_index(store)

    enrich.enrich_pages(
        store, min_body_chars=200, min_citations=2, triggered_by="human-reviewer",
    )
    events = [e for e in audit_mod.read_events(store.kb_dir)
              if e.event == "enrich_pages.run"]
    assert len(events) == 1
    assert events[0].actor == "human-reviewer"

    # dry runs mutate nothing and log nothing.
    enrich.enrich_pages(
        store, min_body_chars=200, min_citations=2, dry_run=True,
        triggered_by="human-reviewer",
    )
    events = [e for e in audit_mod.read_events(store.kb_dir)
              if e.event == "enrich_pages.run"]
    assert len(events) == 1


# --- wire surfaces -------------------------------------------------------------


def test_capabilities_lists_enrich_page() -> None:
    assert "kb.enrich_page" in capabilities.capabilities().methods
    assert "kb.enrich_page" in HANDLERS


def test_jsonl_kb_enrich_page_files_proposal(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _approved_claim(store, "retries are capped at three attempts")
    page = _approved_page(store, "Retries", "A stub.")
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    resp = handle_request({
        "id": "r1", "method": "kb.enrich_page",
        "params": {"page_id": page.id, "min_body_chars": 200, "min_citations": 2},
    })

    assert resp["ok"]
    assert resp["result"]["proposed"]
    assert store.list_pages()[0].body == "A stub."  # unchanged — still just a proposal


def test_jsonl_kb_enrich_page_missing_page_is_clean_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "r2", "method": "kb.enrich_page", "params": {"page_id": "no-such-page"},
    })
    assert not resp["ok"]
