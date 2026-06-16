"""KB observability — vouch stats / kb.stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import stats
from vouch.cli import cli
from vouch.jsonl_server import handle_request
from vouch.models import Claim
from vouch.proposals import approve, expire_one, propose_claim, reject
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def test_citation_summary_clean_kb(store: KBStore) -> None:
    src = store.put_source(b"evidence body")
    store.put_claim(Claim(id="c1", text="fact", evidence=[src.id]))
    summary = stats.citation_summary(store)
    assert summary["claims_total"] == 1
    assert summary["claims_with_valid_citation"] == 1
    assert summary["coverage_rate"] == 1.0
    assert summary["broken_citation"] == 0


def test_citation_summary_flags_broken_and_invalid(store: KBStore) -> None:
    src_ok = store.put_source(b"ok")
    src_gone = store.put_source(b"gone")
    store.put_claim(Claim(id="good", text="t", evidence=[src_ok.id]))
    store.put_claim(Claim(id="broken", text="t", evidence=[src_gone.id]))
    import shutil

    shutil.rmtree(store.kb_dir / "sources" / src_gone.id)
    (store.kb_dir / "claims" / "legacy.yaml").write_text(
        "id: legacy\n"
        'text: "uncited"\n'
        "type: fact\n"
        "status: stable\n"
        "confidence: 1.0\n"
        "evidence: []\n"
    )
    summary = stats.citation_summary(store)
    assert summary["claims_total"] == 3
    assert summary["invalid_claim"] == 1
    assert summary["broken_citation"] == 1
    assert summary["claims_with_valid_citation"] == 1


def test_pending_summary_by_agent(store: KBStore) -> None:
    src = store.put_source(b"x")
    propose_claim(store, text="a", evidence=[src.id], proposed_by="agent-a")
    propose_claim(store, text="b", evidence=[src.id], proposed_by="agent-a")
    propose_claim(store, text="c", evidence=[src.id], proposed_by="agent-b")
    pending = stats.pending_summary(store)
    assert pending["total"] == 3
    assert pending["by_agent"] == {"agent-a": 2, "agent-b": 1}
    assert pending["age_days"]["median"] is not None


def test_review_summary_counts_decisions(store: KBStore) -> None:
    src = store.put_source(b"x")
    p_ok = propose_claim(store, text="ok", evidence=[src.id], proposed_by="a1")
    p_no = propose_claim(store, text="no", evidence=[src.id], proposed_by="a2")
    approve(store, p_ok.id, approved_by="human")
    reject(store, p_no.id, rejected_by="human", reason="duplicate")

    review = stats.review_summary(store, since_days=None)
    assert review["approved"] == 1
    assert review["rejected"] == 1
    assert review["approval_rate"] == 0.5
    assert review["by_agent"]["a1"]["approved"] == 1
    assert review["by_agent"]["a2"]["rejected"] == 1


def test_review_summary_respects_window(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="old", evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="human")
    path = store.kb_dir / "decided" / f"{pr.id}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["decided_at"] = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    path.write_text(yaml.safe_dump(raw, sort_keys=False))

    narrow = stats.review_summary(store, since_days=30)
    wide = stats.review_summary(store, since_days=None)
    assert narrow["approved"] == 0
    assert wide["approved"] == 1


def test_collect_stats_includes_audit_totals(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="t", evidence=[src.id], proposed_by="a")
    approve(store, pr.id, approved_by="human")
    body = stats.collect_stats(store, since_days=30)
    assert body["review"]["audit_totals"]["approved"] >= 1
    assert "citations" in body
    assert body["counts"]["claims"] >= 1


def test_cli_stats_json(store: KBStore) -> None:
    src = store.put_source(b"x")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    result = CliRunner().invoke(cli, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    assert data["citations"]["coverage_rate"] == 1.0


def test_jsonl_kb_stats(store: KBStore) -> None:
    resp = handle_request({"id": "1", "method": "kb.stats", "params": {}})
    assert resp["ok"] is True
    assert "pending" in resp["result"]
    assert "review" in resp["result"]


def test_stats_marks_expired_decisions(store: KBStore) -> None:
    src = store.put_source(b"x")
    pr = propose_claim(store, text="stale", evidence=[src.id], proposed_by="a")
    path = store.kb_dir / "proposed" / f"{pr.id}.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["proposed_at"] = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    expire_one(store, pr.id)
    review = stats.review_summary(store, since_days=None)
    assert review["expired"] == 1
    assert review["by_agent"]["a"]["expired"] == 1
