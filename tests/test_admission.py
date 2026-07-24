"""Deterministic admission gate: knowledge-shaped garbage is auto-rejected at the
proposal funnel for passive auto-capture actors, and left alone for deliberate
authors. Corpus is the real trash observed in a live KB's pending queue.
"""

from __future__ import annotations

import pytest

from vouch import admission
from vouch.models import ProposalStatus
from vouch.proposals import propose_claim, propose_page
from vouch.storage import KBStore

# --- real fragments observed in the wild (vouch-capture claim spans) ----------
FRAGMENT_CLAIMS = [
    "Here's where things stand:",                       # lead-in colon
    "> From vouch memory:",                             # lead-in colon (quoted)
    "## The one idea everything hangs on",              # markdown heading
    "com/vouchdev/vouch/pull/517) is open",             # stray close paren
    "- [adapters/claude-code/README.",                  # unbalanced bracket
    "The dorahack entry in `~/.",                       # unbalanced backtick
    "✨ **What's new**",                            # emoji + bold heading label
    "**Summary**",                                      # bold-only label
]

# claims the adversarial pass PROVED were being wrongly hard-rejected — the gate
# must ADMIT these (precision regressions; keep them or the false positives return)
PRECISION_REGRESSIONS = [
    "Vouch is the knowledge base every project depends on.",          # stranded preposition
    "The config lives in the directory the KB was initialized in.",   # stranded preposition
    "Use **kwargs to accept arbitrary keyword arguments in Python.",  # lone ** (kwargs)
    "# of pending proposals dropped to zero after the sweep.",        # '#' = number-of
    'A 27" monitor improved the reviewer workflow.',                  # lone double-quote
]

# --- real durable claims that MUST survive (Karpathy + the two borderline) ----
GOOD_CLAIMS = [
    'In the Oct 2025 Dwarkesh Podcast interview, Andrej Karpathy proposed a "cognitive core".',
    "> Simply: one-prompt memory remembers *what was said*; vouch remembers *what was verified*.",
    "> In vouch's design, care of the output is mostly mechanical, "
    "with a thin human curator on top.",
]


@pytest.fixture
def store(tmp_path, monkeypatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


# ---------------------------------------------------------------- pure predicate
@pytest.mark.parametrize("text", FRAGMENT_CLAIMS)
def test_assess_claim_rejects_structural_fragments(text: str) -> None:
    verdict = admission.assess_claim(text)
    assert not verdict.admit, f"should reject fragment: {text!r}"
    assert verdict.reason


@pytest.mark.parametrize("text", GOOD_CLAIMS)
def test_assess_claim_admits_durable_claims(text: str) -> None:
    assert admission.assess_claim(text).admit, f"should admit: {text!r}"


@pytest.mark.parametrize("text", PRECISION_REGRESSIONS)
def test_assess_claim_admits_previously_false_rejected(text: str) -> None:
    assert admission.assess_claim(text).admit, f"regressed to a false reject: {text!r}"


def test_assess_claim_rejects_emphasis_heading_labels() -> None:
    """Emoji/bold heading labels are not claims — the '##' rule cannot see them.

    Regression for "✨ **What's new**", a section header the receipt-verified
    auto-approve path was laundering into approved knowledge.
    """
    for label in ("✨ **What's new**", "**Summary**", "📝 **Notes**", "***TODO***"):
        verdict = admission.assess_claim(label)
        assert not verdict.admit, f"should reject label: {label!r}"
        assert verdict.reason


def test_assess_claim_admits_inline_emphasis_prose() -> None:
    """A claim that merely *contains* emphasis is prose, not a label — admit it.

    Precision guard so the emphasis-label rule never eats a real claim: a fully
    wrapped span is a label, a wrapped word inside a sentence is not.
    """
    assert admission.assess_claim(
        "The release ships with **trusted** publishing enabled on pypi."
    ).admit
    assert admission.assess_claim(
        "Use **kwargs to accept arbitrary keyword arguments in Python."
    ).admit


def test_autocapture_emphasis_label_is_auto_rejected(store: KBStore) -> None:
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store, text="✨ **What's new**", evidence=[src.id],
        proposed_by="vouch-capture",  # passive firehose actor
    )
    assert result.proposal.status is ProposalStatus.REJECTED


def test_resolve_pending_receipt_claim_skips_gate_rejected(store: KBStore) -> None:
    # a fragment the admission gate rejected must not crash capture's auto-approve
    # loop — resolve must return None on a decided proposal, never call approve().
    from vouch.proposals import resolve_pending_receipt_claim
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store, text="Here's where things stand:", evidence=[src.id],
        proposed_by="vouch-capture",
    )
    assert result.proposal.status is ProposalStatus.REJECTED
    out = resolve_pending_receipt_claim(
        store, result.proposal, actor="vouch-capture", reason="x",
    )
    assert out is None  # skipped gracefully, no ProposalError


def test_assess_page_rejects_uncited_session_narrative() -> None:
    payload = {"type": "session", "claims": [], "sources": []}
    assert not admission.assess_page(payload).admit


def test_assess_page_admits_cited_session_page() -> None:
    payload = {"type": "session", "claims": ["some-claim"], "sources": []}
    assert admission.assess_page(payload).admit


def test_assess_page_admits_non_raw_type() -> None:
    payload = {"type": "concept", "claims": [], "sources": []}
    assert admission.assess_page(payload).admit


# ------------------------------------------------------------- provenance gating
def test_autocapture_fragment_claim_is_auto_rejected(store: KBStore) -> None:
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store,
        text="Here's where things stand:",
        evidence=[src.id],
        proposed_by="vouch-capture",  # passive firehose actor
    )
    assert result.proposal.status is ProposalStatus.REJECTED
    # it never lingers in the pending queue
    pending = {p.id for p in store.list_proposals() if p.status is ProposalStatus.PENDING}
    assert result.proposal.id not in pending


def test_autocapture_good_claim_stays_pending(store: KBStore) -> None:
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store,
        text="Vouch routes every write through a single review gate.",
        evidence=[src.id],
        proposed_by="vouch-capture",
    )
    assert result.proposal.status is ProposalStatus.PENDING


def test_deliberate_author_fragment_is_advisory_not_rejected(store: KBStore) -> None:
    # a human/agent who deliberately files something is never hard-rejected by
    # the regex; the human review gate decides.
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store,
        text="Here's where things stand:",
        evidence=[src.id],
        proposed_by="claude-code",  # deliberate actor
    )
    assert result.proposal.status is ProposalStatus.PENDING


def test_autocapture_session_page_is_auto_rejected(store: KBStore) -> None:
    proposal = propose_page(
        store,
        title="Built and stress-tested the adopt feature",
        body="Implemented adopt.py, wrote tests, opened the PR.",
        page_type="session",
        proposed_by="session-split",  # passive firehose actor
    )
    assert proposal.status is ProposalStatus.REJECTED
    pending = {p.id for p in store.list_proposals() if p.status is ProposalStatus.PENDING}
    assert proposal.id not in pending


def test_deliberate_session_page_stays_pending(store: KBStore) -> None:
    proposal = propose_page(
        store,
        title="Built and stress-tested the adopt feature",
        body="Implemented adopt.py, wrote tests, opened the PR.",
        page_type="session",
        proposed_by="claude-code",  # deliberate actor
    )
    assert proposal.status is ProposalStatus.PENDING


# ----------------------------------------------------------- config: confidence
def _write_admission_config(store: KBStore, body: str) -> None:
    store.config_path.write_text(f"admission:\n{body}", encoding="utf-8")


def test_assess_claim_confidence_floor() -> None:
    good = "Vouch routes every write through a single review gate."
    assert not admission.assess_claim(good, confidence=0.4, min_confidence=0.5).admit
    assert admission.assess_claim(good, confidence=0.6, min_confidence=0.5).admit
    assert admission.assess_claim(good, confidence=None, min_confidence=0.5).admit
    assert admission.assess_claim(good).admit  # no floor by default


def test_confidence_floor_rejects_low_confidence_autocapture(store: KBStore) -> None:
    _write_admission_config(store, "  min_confidence: 0.8\n")
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store, text="Vouch routes every write through a single review gate.",
        evidence=[src.id], proposed_by="vouch-capture", confidence=0.7,
    )
    assert result.proposal.status is ProposalStatus.REJECTED
    assert "confidence" in (result.proposal.decision_reason or "")


def test_confidence_floor_advisory_for_deliberate(store: KBStore) -> None:
    _write_admission_config(store, "  min_confidence: 0.8\n")
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store, text="Vouch routes every write through a single review gate.",
        evidence=[src.id], proposed_by="claude-code", confidence=0.7,
    )
    assert result.proposal.status is ProposalStatus.PENDING


# ---------------------------------------------------------------- config: toggles
def test_admission_disabled_lets_fragments_through(store: KBStore) -> None:
    _write_admission_config(store, "  enabled: false\n")
    src = store.put_source(b"here is a source body", title="t")
    result = propose_claim(
        store, text="Here's where things stand:", evidence=[src.id],
        proposed_by="vouch-capture",
    )
    assert result.proposal.status is ProposalStatus.PENDING


def test_page_rule_toggle_off_keeps_session_page(store: KBStore) -> None:
    _write_admission_config(store, "  reject_uncited_session_pages: false\n")
    proposal = propose_page(
        store, title="Built the adopt feature", body="did work",
        page_type="session", proposed_by="session-split",
    )
    assert proposal.status is ProposalStatus.PENDING


# ------------------------------------------------------------- check rejected cli
def test_vouch_rejected_lists_admission_rejections(store: KBStore) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli
    src = store.put_source(b"here is a source body", title="t")
    propose_claim(
        store, text="Here's where things stand:", evidence=[src.id],
        proposed_by="vouch-capture",
    )
    res = CliRunner().invoke(cli, ["rejected", "--admission"])
    assert res.exit_code == 0, res.output
    assert "vouch-admission" in res.output
    assert "admission:" in res.output  # the recorded reason is visible
