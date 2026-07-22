"""Phase B — source → receipt-backed claims, the capture step with no human.

Segments an ingested source into quotable spans and files each as a claim that
quotes it verbatim, so every claim's receipt verifies by construction and
Phase D's gate can auto-approve it. Deterministic and llm-free: the receipt
check is the guardrail, so the pipeline runs in the base install and under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import extract, index_db, receipts
from vouch.cli import cli
from vouch.models import ProposalStatus
from vouch.storage import KBStore

SOURCE = (
    b"The sky is blue and clear. Water boils at one hundred degrees celsius. "
    b"Grass is green in the spring."
)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_segment_source_splits_into_verbatim_spans() -> None:
    text = SOURCE.decode()
    segs = extract.segment_source(text)
    assert "The sky is blue and clear." in segs
    assert "Water boils at one hundred degrees celsius." in segs
    # every segment is a verbatim substring of the source it came from.
    for s in segs:
        assert s in text


def test_segment_source_drops_short_noise_and_dupes() -> None:
    text = (
        "ok. The very same sentence appears here twice in a row now. "
        "The very same sentence appears here twice in a row now. ###---***"
    )
    segs = extract.segment_source(text)
    assert "ok" not in segs  # below min length
    assert segs.count("The very same sentence appears here twice in a row now.") == 1
    assert all(any(c.isalpha() for c in s) for s in segs)  # no pure-symbol spans


def test_extract_receipt_claims_files_verifying_claims(store: KBStore) -> None:
    src = store.put_source(SOURCE)
    filed = extract.extract_receipt_claims(store, src.id, proposed_by="agent")
    assert len(filed) == 3
    # each filed claim carries a receipt that verifies against the source bytes.
    for res in filed:
        evidence_ids = store.get_proposal(res.id).payload["evidence"]
        assert receipts.evaluate_claim_receipts(store, evidence_ids).approve


def test_ingest_source_auto_approves_and_is_recallable(store: KBStore) -> None:
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: true\n", encoding="utf-8"
    )
    _src, approved = extract.ingest_source(store, SOURCE, proposed_by="agent")
    assert len(approved) == 3
    # durable with no human, and recallable.
    assert len(store.list_claims()) == 3
    hits = index_db.search(store.kb_dir, "boils")
    assert any(k == "claim" for k, *_ in hits)


def test_ingest_source_leaves_pending_when_gate_off(store: KBStore) -> None:
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: false\n", encoding="utf-8"
    )
    _src, approved = extract.ingest_source(store, SOURCE, proposed_by="agent")
    assert approved == []
    # proposed and receipt-backed, but still waiting for a human.
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 3


# --- M1: selection / compression -----------------------------------------
# Every-sentence capture beats nothing but not grep; the fidelity number
# (accuracy / tokens) moves only when ingest keeps the fact-dense spans and
# drops filler. Selection ranks spans by information density and keeps the
# best under a budget — but only ever *returns a subset of the verbatim
# spans*, never a paraphrase, so every kept claim's receipt still verifies by
# construction.

# Two fact-dense sentences (distinct content words, a number, proper nouns)
# and two filler sentences (stopwords and repetition, near-zero new content).
DENSE_A = "Photosynthesis converts carbon dioxide and water into glucose using sunlight."
DENSE_B = "Mount Everest rises 8849 metres above sea level on the border of Nepal."
FILLER_A = "And so it is, and so it is, and so it is, and so it is once more."
FILLER_B = "That is just how it is and how it will be, more or less, as you know."
MIX = f"{DENSE_A} {FILLER_A} {DENSE_B} {FILLER_B}".encode()


def test_select_spans_without_budget_returns_all() -> None:
    segs = ["Alpha beta gamma delta epsilon.", "One two three four five."]
    # no cap == today's behaviour: the unbudgeted baseline is left untouched.
    assert extract.select_spans(segs) == segs


def test_select_spans_keeps_densest_under_max_claims() -> None:
    segs = [DENSE_A, FILLER_A, DENSE_B, FILLER_B]
    kept = extract.select_spans(segs, max_claims=2)
    # the two fact-dense spans survive, filler is dropped, source order kept.
    assert kept == [DENSE_A, DENSE_B]


def test_select_spans_respects_char_budget() -> None:
    segs = [FILLER_A, DENSE_A]
    kept = extract.select_spans(segs, budget_chars=len(DENSE_A))
    assert kept == [DENSE_A]
    assert sum(len(s) for s in kept) <= len(DENSE_A)


def test_select_spans_preserves_source_order_and_is_deterministic() -> None:
    a = "Glucose stores chemical energy inside living plant cells."
    b = "Chlorophyll absorbs red and blue wavelengths of visible light."
    c = "Mitochondria release that stored energy during aerobic respiration."
    segs = [a, b, c]
    once = extract.select_spans(segs, max_claims=2)
    twice = extract.select_spans(segs, max_claims=2)
    assert once == twice  # deterministic, llm-free
    # whichever two win, they come back in their original document order.
    assert once == [s for s in segs if s in once]


def test_extract_receipt_claims_max_claims_selects_densest_and_verifies(
    store: KBStore,
) -> None:
    src = store.put_source(MIX)
    filed = extract.extract_receipt_claims(
        store, src.id, proposed_by="agent", max_claims=2
    )
    assert len(filed) == 2
    texts = {store.get_proposal(res.id).payload["text"] for res in filed}
    assert DENSE_A in texts and DENSE_B in texts  # density selection, not truncation
    assert FILLER_A not in texts and FILLER_B not in texts
    # selecting a subset never breaks the receipt: kept spans stay verbatim.
    for res in filed:
        evidence_ids = store.get_proposal(res.id).payload["evidence"]
        assert receipts.evaluate_claim_receipts(store, evidence_ids).approve


def test_ingest_source_budget_compresses_and_stays_recallable(store: KBStore) -> None:
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: true\n", encoding="utf-8"
    )
    _src, approved = extract.ingest_source(
        store, MIX, proposed_by="agent", max_claims=2
    )
    # compressed: fewer claims than the four spans, and only the dense ones.
    assert len(approved) == 2
    assert len(store.list_claims()) == 2
    hits = index_db.search(store.kb_dir, "photosynthesis")
    assert any(k == "claim" for k, *_ in hits)


def test_cli_ingest_max_claims_compresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("VOUCH_KB_PATH", raising=False)
    monkeypatch.delenv("VOUCH_PROJECT_DIR", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    store = KBStore.init(proj)
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: true\n", encoding="utf-8"
    )
    doc = proj / "doc.txt"
    doc.write_bytes(MIX)
    monkeypatch.chdir(proj)
    r = CliRunner().invoke(cli, ["ingest", str(doc), "--max-claims", "2", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["approved"] == 2
    assert len(store.list_claims()) == 2
