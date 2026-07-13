"""`vouch flag-anomalies` — advisory anomaly flags on pending proposals (#323).

Covers the non-embedding codes (thin_evidence, contradicts_many), graceful
degradation when no embedder is present (far_from_corpus produces no code), the
read-only / no-mutation invariant, worst-first ordering, config resolution, and
the cli surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.anomaly import (
    DEFAULT_FAR_FROM_CORPUS_FLOOR,
    Anomaly,
    flag_anomalies,
    render_text,
)
from vouch.audit import count_events
from vouch.cli import cli
from vouch.models import Claim, ClaimStatus, Proposal, ProposalKind
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _pending_claim(
    store: KBStore, pid: str, *, evidence, contradicts=None, text="a claim", by="agent"
):
    payload = {
        "id": pid, "text": text, "type": "observation",
        "confidence": 0.7, "evidence": list(evidence), "entities": [], "tags": [],
    }
    if contradicts is not None:
        payload["contradicts"] = list(contradicts)
    store.put_proposal(Proposal(id=pid, kind=ProposalKind.CLAIM, proposed_by=by, payload=payload))


def _seed(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    # an approved live claim (a valid contradiction target) + a retired one
    store.put_claim(Claim(id="approved-1", text="live claim", evidence=[src.id]))
    store.put_claim(Claim(id="retired-1", text="old claim", evidence=[src.id],
                          status=ClaimStatus.ARCHIVED))
    # p_thin: only one citation -> thin_evidence
    _pending_claim(store, "p_thin", evidence=[src.id])
    # p_contra: well-cited but declares a contradiction against an approved claim
    _pending_claim(store, "p_contra", evidence=[src.id, src.id], contradicts=["approved-1"])
    # p_both: thin AND contradicts -> two reasons (worst)
    _pending_claim(store, "p_both", evidence=[src.id], contradicts=["approved-1"])
    # p_clean: well cited, no contradictions -> not flagged
    _pending_claim(store, "p_clean", evidence=[src.id, src.id])


# --- non-embedding codes --------------------------------------------------


def test_thin_evidence_flagged(store: KBStore) -> None:
    _seed(store)
    flagged = {a.proposal_id: a for a in flag_anomalies(store)}
    assert "p_thin" in flagged
    codes = {r["code"] for r in flagged["p_thin"].reasons}
    assert codes == {"thin_evidence"}
    assert flagged["p_thin"].reasons[0]["evidence_count"] == 1


def test_clean_proposal_not_flagged(store: KBStore) -> None:
    _seed(store)
    ids = {a.proposal_id for a in flag_anomalies(store)}
    assert "p_clean" not in ids


def test_contradicts_counts_only_approved_live(store: KBStore) -> None:
    _seed(store)
    # declaring a contradiction against a RETIRED claim must not count
    _pending_claim(store, "p_retired_contra", evidence=[store.list_sources()[0].id, "x"],
                   contradicts=["retired-1"])
    flagged = {a.proposal_id: a for a in flag_anomalies(store)}
    assert "p_retired_contra" not in flagged  # 2 evidence, contradiction not live -> clean
    assert "contradicts_many" in {r["code"] for r in flagged["p_contra"].reasons}


def test_worst_first_ordering(store: KBStore) -> None:
    _seed(store)
    order = [a.proposal_id for a in flag_anomalies(store)]
    # p_both has two reasons -> sorts ahead of the single-reason ones
    assert order[0] == "p_both"
    assert set(order) == {"p_thin", "p_contra", "p_both"}


def test_thresholds_configurable(store: KBStore) -> None:
    _seed(store)
    # raise the evidence floor so 2-citation claims also flag as thin
    hi = flag_anomalies(store, min_evidence=2)
    assert "p_contra" in {a.proposal_id for a in hi}  # now thin too (2 <= 2)
    # a huge contradiction threshold suppresses contradicts_many
    lo = {a.proposal_id: a for a in flag_anomalies(store, contradiction_count=99)}
    assert "p_contra" not in lo  # its only reason was the contradiction


# --- embeddings graceful degradation --------------------------------------


def test_far_from_corpus_absent_without_embedder(store: KBStore) -> None:
    _seed(store)
    # the dev install has no embedder -> far_from_corpus never appears, but the
    # non-embedding codes still compute.
    for a in flag_anomalies(store):
        assert all(r["code"] != "far_from_corpus" for r in a.reasons)
    assert flag_anomalies(store)  # still produced non-embedding flags


def test_far_from_corpus_uses_floor_when_embedder_present(store: KBStore, monkeypatch) -> None:
    _seed(store)

    class FakeEmbedder:
        def encode(self, text):
            return [1.0, 0.0, 0.0]

    monkeypatch.setattr("vouch.anomaly._try_embedder", lambda: FakeEmbedder())
    # force the corpus search to return a far neighbour (low cosine)
    monkeypatch.setattr(
        "vouch.index_db.search_embedding",
        lambda *a, **k: [("claim", "approved-1", "live claim", 0.10)],
    )
    flagged = {
        a.proposal_id: a
        for a in flag_anomalies(store, far_floor=DEFAULT_FAR_FROM_CORPUS_FLOOR)
    }
    far = [r for r in flagged["p_clean"].reasons if r["code"] == "far_from_corpus"]
    assert far and far[0]["best_cosine"] == pytest.approx(0.10)
    # a close neighbour (high cosine) must NOT flag
    monkeypatch.setattr(
        "vouch.index_db.search_embedding",
        lambda *a, **k: [("claim", "approved-1", "live claim", 0.95)],
    )
    flagged2 = {a.proposal_id: a for a in flag_anomalies(store)}
    assert "p_clean" not in flagged2


# --- read-only invariant --------------------------------------------------


def test_flag_writes_nothing(store: KBStore) -> None:
    _seed(store)
    before = count_events(store.kb_dir)
    flag_anomalies(store)
    flag_anomalies(store, min_evidence=3)
    assert count_events(store.kb_dir) == before


def test_empty_kb(store: KBStore) -> None:
    assert flag_anomalies(store) == []
    assert "no pending proposals look anomalous" in render_text([])


# --- cli ------------------------------------------------------------------


def test_cli_text(store: KBStore, monkeypatch) -> None:
    _seed(store)
    monkeypatch.chdir(store.root)
    res = CliRunner().invoke(cli, ["flag-anomalies"])
    assert res.exit_code == 0, res.output
    assert "flag-anomalies" in res.output
    assert "p_both" in res.output
    assert "thin_evidence" in res.output


def test_cli_json(store: KBStore, monkeypatch) -> None:
    _seed(store)
    monkeypatch.chdir(store.root)
    res = CliRunner().invoke(cli, ["flag-anomalies", "--json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc[0]["proposal_id"] == "p_both"  # worst-first
    assert {r["code"] for r in doc[0]["reasons"]} == {"thin_evidence", "contradicts_many"}


def test_cli_defaults_and_dataclass() -> None:
    assert isinstance(Anomaly("p", "claim", "a", []).to_dict(), dict)
