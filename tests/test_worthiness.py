"""Tier 2 worthiness scoring: advisory, deterministic, local by default.

The scorer never runs at the funnel and never mutates a payload; these tests pin
the directional behaviour of the heuristic backend (a question scores below a
statement, an imperative below a fact, a near-duplicate below a fresh claim) plus
config parsing and backend selection.
"""

from __future__ import annotations

import pytest

from vouch import health, worthiness
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path, monkeypatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _score(store: KBStore, text: str) -> worthiness.WorthinessResult:
    return worthiness.HeuristicScorer().score(text, store=store)


# ------------------------------------------------------------ directional signals
def test_good_claim_scores_above_threshold(store: KBStore) -> None:
    r = _score(store, "Vouch stores every claim as a yaml file committed in the repo.")
    assert r.score >= worthiness.DEFAULT_MIN_SCORE


def test_question_scores_below_a_statement(store: KBStore) -> None:
    q = _score(store, "honestly, which review backend is better do you think?")
    good = _score(store, "Vouch routes every write through a single review gate.")
    assert q.score < worthiness.DEFAULT_MIN_SCORE < good.score
    assert "question" in q.reason


def test_imperative_task_scores_low(store: KBStore) -> None:
    r = _score(store, "create pr and generate the announcement message")
    assert r.score < worthiness.DEFAULT_MIN_SCORE
    assert "imperative" in r.reason


def test_leading_pronoun_scores_below_self_contained(store: KBStore) -> None:
    deictic = _score(store, "it was wiped out during the reset last night")
    grounded = _score(store, "the pending queue was wiped out during the reset last night")
    assert deictic.score < grounded.score


def test_too_short_scores_low(store: KBStore) -> None:
    assert _score(store, "session summary").score < worthiness.DEFAULT_MIN_SCORE


# ------------------------------------------------------------------------- novelty
def test_near_duplicate_scores_below_novel_claim(store: KBStore) -> None:
    src = store.put_source(b"here is a source body", title="t")
    approved = "Vouch routes every write through a single mandatory review gate."
    store.put_claim(Claim(id="c1", text=approved, evidence=[src.id]))
    health.rebuild_index(store)

    dup = _score(store, "Vouch routes every write through a single mandatory review gate.")
    novel = _score(store, "The release workflow publishes wheels to pypi via trusted publishing.")

    assert dup.score < novel.score
    assert "duplicate" in dup.reason
    assert dup.signals["novelty"] < novel.signals["novelty"]


def test_novelty_fails_open_without_index(store: KBStore) -> None:
    # no index built and no approved claims — a fresh claim must not be punished
    r = _score(store, "The audit log is the only authoritative history in vouch.")
    assert r.signals["novelty"] == 1.0


# -------------------------------------------------------------------------- config
def _write_worthiness_config(store: KBStore, body: str) -> None:
    store.config_path.write_text(f"worthiness:\n{body}", encoding="utf-8")


def test_load_config_defaults_when_absent(store: KBStore) -> None:
    cfg = worthiness.load_config(store)
    assert cfg.scorer == "heuristic"
    assert cfg.action == "annotate"
    assert cfg.min_score == worthiness.DEFAULT_MIN_SCORE


def test_load_config_parses_block(store: KBStore) -> None:
    _write_worthiness_config(store, "  scorer: off\n  min_score: 0.6\n  action: reject\n")
    cfg = worthiness.load_config(store)
    assert cfg.scorer == "off"
    assert cfg.min_score == 0.6
    assert cfg.action == "reject"


def test_get_scorer_selects_backend(store: KBStore) -> None:
    default = worthiness.get_scorer(worthiness.WorthinessConfig())
    assert isinstance(default, worthiness.HeuristicScorer)
    assert worthiness.get_scorer(worthiness.WorthinessConfig(scorer="off")) is None
    # llm backend not wired yet — degrades to no-op, never crashes a review pass
    assert worthiness.get_scorer(worthiness.WorthinessConfig(scorer="llm")) is None
