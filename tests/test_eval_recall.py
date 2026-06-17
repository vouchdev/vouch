"""Recall-quality eval harness: metrics, run_recall, baseline gate."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from vouch import health
from vouch.eval.recall import (
    compare_baseline,
    load_queries,
    run_recall,
    score_query,
)
from vouch.models import Claim
from vouch.storage import KBStore


def test_score_query_hand_computed() -> None:
    ranked = ["a", "x", "b", "y", "z", "c"]
    expected = ["a", "b", "c"]
    out = score_query(ranked, expected, k=5)
    # Top-5 = a,x,b,y,z -> 2 of 5 relevant.
    assert out["p_at_k"] == 2 / 5
    # 2 of 3 expected appear in top-5.
    assert out["r_at_k"] == 2 / 3
    # First relevant hit is at rank 1.
    assert out["mrr"] == 1.0
    # DCG: a@1 (1/log2 2) + b@3 (1/log2 4). c is at rank 6, outside k=5.
    dcg = 1.0 / math.log2(2) + 1.0 / math.log2(4)
    ideal = 1.0 / math.log2(2) + 1.0 / math.log2(3) + 1.0 / math.log2(4)
    assert out["ndcg_at_k"] == dcg / ideal


def test_score_query_no_hits() -> None:
    out = score_query(["x", "y"], ["a"], k=5)
    assert out == {"p_at_k": 0.0, "r_at_k": 0.0, "mrr": 0.0, "ndcg_at_k": 0.0}


def test_score_query_mrr_uses_full_ranking() -> None:
    # Relevant id is at rank 3 (outside k for P@k, but MRR scans full list).
    out = score_query(["x", "y", "a", "z"], ["a"], k=2)
    assert out["mrr"] == 1 / 3
    assert out["p_at_k"] == 0.0


def test_load_queries_accepts_both_keys(tmp_path: Path) -> None:
    p = tmp_path / "q.jsonl"
    p.write_text(
        json.dumps({"query": "one", "expected": ["c1"]})
        + "\n\n"
        + json.dumps({"query": "two", "expected_ids": ["c2", "c3"]})
        + "\n",
        encoding="utf-8",
    )
    rows = load_queries(p)
    assert rows == [
        {"query": "one", "expected": ["c1"]},
        {"query": "two", "expected": ["c2", "c3"]},
    ]


def test_run_recall_deterministic_report(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"evidence")
    store.put_claim(Claim(id="auth-jwt", text="auth uses JWT bearer tokens",
                          evidence=[src.id]))
    store.put_claim(Claim(id="db-postgres", text="datastore is PostgreSQL",
                          evidence=[src.id]))
    store.put_claim(Claim(id="cache-redis", text="Redis caches query results",
                          evidence=[src.id]))
    health.rebuild_index(store)

    qpath = tmp_path / "queries.jsonl"
    qpath.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"query": "JWT", "expected": ["auth-jwt"]},
                {"query": "PostgreSQL", "expected": ["db-postgres"]},
                {"query": "Redis", "expected": ["cache-redis"]},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_recall(store, qpath, k=5)
    assert report["k"] == 5
    assert report["n_queries"] == 3
    # Each query's single relevant claim is the top FTS5 hit.
    for pq in report["per_query"]:
        assert pq["ranked"][0] == pq["expected"][0]
        assert pq["scores"]["mrr"] == 1.0
        assert pq["scores"]["r_at_k"] == 1.0
        assert pq["scores"]["p_at_k"] == 1 / 5
    assert report["macro"]["mrr"] == 1.0
    assert report["macro"]["p_at_k"] == pytest.approx(1 / 5)

    # Determinism: identical inputs yield an identical report.
    assert run_recall(store, qpath, k=5) == report


def test_compare_baseline_flags_regression() -> None:
    report = {"k": 5, "macro": {"p_at_k": 0.40}}
    baseline = {"k": 5, "macro": {"p_at_k": 0.50}}
    ok, msg = compare_baseline(report, baseline, max_regression=0.05)
    assert ok is False
    assert "regression" in msg


def test_compare_baseline_within_tolerance() -> None:
    report = {"k": 5, "macro": {"p_at_k": 0.47}}
    baseline = {"k": 5, "macro": {"p_at_k": 0.50}}
    ok, msg = compare_baseline(report, baseline, max_regression=0.05)
    assert ok is True
    assert "ok" in msg


def test_compare_baseline_improvement_is_ok() -> None:
    report = {"k": 5, "macro": {"p_at_k": 0.80}}
    baseline = {"k": 5, "macro": {"p_at_k": 0.50}}
    ok, _ = compare_baseline(report, baseline, max_regression=0.05)
    assert ok is True


def test_committed_query_set_is_loadable() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    rows = load_queries(repo_root / "eval" / "queries.jsonl")
    assert len(rows) >= 6
    assert all(r["query"] and r["expected"] for r in rows)
