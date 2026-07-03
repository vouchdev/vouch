"""`vouch digest` — read-only operator briefing (vouchdev/vouch#324).

These tests pin the stable to_dict() schema and the four viewports against a
fixture KB whose contents are known by construction, and assert the load-bearing
invariant: a digest writes *nothing* (no proposals, no audit events, no files).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.audit import log_event
from vouch.cli import cli
from vouch.digest import (
    DEFAULT_LIMIT,
    DEFAULT_SINCE,
    Digest,
    compute_digest,
    render_markdown,
    render_text,
)
from vouch.metrics import MetricsError
from vouch.models import Claim, ClaimStatus, Proposal, ProposalKind
from vouch.storage import KBStore

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


# --- fixture builder ------------------------------------------------------


def _ev(kb_dir: Path, event: str, actor: str, ids: list[str], ts: datetime) -> None:
    """Append an audit event, then rewrite its created_at to ``ts`` — the same
    deterministic-timestamp trick test_metrics uses, against the real reader."""
    log_event(kb_dir, event=event, actor=actor, object_ids=ids)
    path = kb_dir / "audit.log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[-1])
    obj["created_at"] = ts.isoformat()
    lines[-1] = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pending(store: KBStore, pid: str, *, kind=ProposalKind.CLAIM, by="alice", at, payload):
    store.put_proposal(
        Proposal(id=pid, kind=kind, proposed_by=by, proposed_at=at, payload=payload)
    )


def _seed(store: KBStore) -> None:
    """A KB with known-by-construction contents.

    Pending (3): p_old (2d ago), p_mid (1d ago), p_new (1h ago).
    Decisions in the audit log: 2 approvals + 1 rejection inside a recent
    window, 1 approval 100d ago (outside a 7d window).
    Claims (3): c_fresh (confirmed today), c_stale (300d), c_archived (retired).
    """
    src = store.put_source(b"evidence-bytes")

    _pending(store, "p_old", by="alice", at=NOW - timedelta(days=2),
             payload={"text": "oldest pending"})
    _pending(store, "p_mid", by="bob", at=NOW - timedelta(days=1),
             payload={"title": "middle pending"})
    _pending(store, "p_new", by="alice", at=NOW - timedelta(hours=1),
             payload={"name": "newest pending"})

    store.put_claim(Claim(id="c_fresh", text="fresh claim", evidence=[src.id],
                          last_confirmed_at=NOW - timedelta(days=1)))
    store.put_claim(Claim(id="c_stale", text="stale claim", evidence=[src.id],
                          last_confirmed_at=NOW - timedelta(days=300)))
    store.put_claim(Claim(id="c_archived", text="archived claim", evidence=[src.id],
                          status=ClaimStatus.ARCHIVED,
                          last_confirmed_at=NOW - timedelta(days=400)))

    kb = store.kb_dir
    _ev(kb, "proposal.claim.approve", "carol", ["p_a", "c_fresh"], NOW - timedelta(days=2))
    _ev(kb, "proposal.page.reject", "carol", ["p_b"], NOW - timedelta(days=1))
    _ev(kb, "proposal.claim.approve", "dave", ["p_c", "c_stale"], NOW - timedelta(hours=6))
    # well outside a 7d window — must not appear:
    _ev(kb, "proposal.claim.approve", "dave", ["p_z", "c_old"], NOW - timedelta(days=100))


# --- pending viewport -----------------------------------------------------


def test_pending_oldest_first(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=None, now=NOW)
    assert d.pending_total == 3
    assert [p.id for p in d.pending] == ["p_old", "p_mid", "p_new"]
    # preview pulls text / title / name in that order
    assert d.pending[0].preview == "oldest pending"
    assert d.pending[1].preview == "middle pending"
    assert d.pending[2].preview == "newest pending"
    # age is measured from proposed_at to now
    assert d.pending[0].age_seconds == pytest.approx(2 * 86400)


def test_pending_limit_caps_but_total_is_full(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=None, limit=1, now=NOW)
    assert d.pending_total == 3
    assert [p.id for p in d.pending] == ["p_old"]


# --- decisions viewport ---------------------------------------------------


def test_decisions_window_filtering(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=NOW - timedelta(days=7), now=NOW)
    # the 100d-old approval is excluded; 2 approve + 1 reject remain
    assert d.approvals == 2
    assert d.rejections == 1
    ids = {de.proposal_id for de in d.decisions}
    assert ids == {"p_a", "p_b", "p_c"}
    # newest first
    assert d.decisions[0].proposal_id == "p_c"


def test_decisions_all_history(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=None, now=NOW)
    assert d.approvals == 3  # includes the 100d-old one
    assert d.rejections == 1


# --- stale viewport -------------------------------------------------------


def test_stale_uses_metrics_predicate(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=None, stale_after_days=180, now=NOW)
    # c_stale is stale; c_fresh is fresh; c_archived is retired (exempt)
    assert d.stale_total == 1
    assert d.stale[0].id == "c_stale"
    assert d.stale[0].age_days == pytest.approx(300, abs=1)


# --- citation-coverage delta ----------------------------------------------


def test_citation_delta_none_when_unbounded(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=None, now=NOW)
    assert d.citation_coverage_now == pytest.approx(1.0)
    assert d.citation_coverage_at_since is None
    assert d.citation_coverage_delta is None


def test_citation_delta_reports_movement(store: KBStore) -> None:
    # baseline claim cited before the cutoff; a new UNCITED claim lands after it,
    # dragging current coverage below the at-cutoff cohort coverage.
    src = store.put_source(b"bytes")
    store.put_claim(Claim(id="c_before", text="before", evidence=[src.id],
                          created_at=NOW - timedelta(days=10)))
    # write an uncited claim directly (put_claim refuses unresolvable citations)
    import yaml
    broken = Claim(id="c_after", text="after", evidence=["missing-src"],
                   created_at=NOW - timedelta(days=1))
    (store.kb_dir / "claims" / "c_after.yaml").write_text(
        yaml.safe_dump(broken.model_dump(mode="json")), encoding="utf-8")

    cutoff = NOW - timedelta(days=5)
    d = compute_digest(store, since=cutoff, now=NOW)
    assert d.citation_coverage_at_since == pytest.approx(1.0)  # only c_before existed
    assert d.citation_coverage_now == pytest.approx(0.5)       # 1 of 2 cited now
    assert d.citation_coverage_delta == pytest.approx(-0.5)


# --- empty kb -------------------------------------------------------------


def test_empty_kb(store: KBStore) -> None:
    d = compute_digest(store, since=None, now=NOW)
    assert d.pending_total == 0
    assert d.pending == []
    assert d.approvals == 0 and d.rejections == 0
    assert d.stale_total == 0
    assert d.citation_coverage_now is None
    # renders without error on an empty kb
    assert "vouch digest" in render_text(d)
    assert "vouch digest" in render_markdown(d)


# --- read-only invariant --------------------------------------------------


def test_digest_writes_nothing(store: KBStore) -> None:
    _seed(store)

    def snapshot() -> dict[str, tuple[float, int]]:
        snap: dict[str, tuple[float, int]] = {}
        for p in sorted(store.kb_dir.rglob("*")):
            if p.is_file():
                st = p.stat()
                snap[str(p.relative_to(store.kb_dir))] = (st.st_mtime, st.st_size)
        return snap

    before = snapshot()
    compute_digest(store, since=NOW - timedelta(days=7), now=NOW)
    compute_digest(store, since=None, now=NOW)
    after = snapshot()
    assert before == after  # no file created, modified, or grown


# --- schema ---------------------------------------------------------------


def test_to_dict_schema(store: KBStore) -> None:
    _seed(store)
    d = compute_digest(store, since=NOW - timedelta(days=7), now=NOW)
    doc = d.to_dict()
    assert doc["schema_version"] == 1
    assert set(doc) == {
        "schema_version", "window", "stale_after_days", "limit",
        "pending", "decisions", "stale", "citation_coverage",
    }
    assert set(doc["pending"]) == {"total", "items"}
    assert set(doc["decisions"]) == {"approvals", "rejections", "total", "items"}
    assert doc["decisions"]["total"] == 3
    assert set(doc["citation_coverage"]) == {"now", "at_since", "delta"}
    # window is round-trippable json
    json.dumps(doc)


def test_bad_since_raises(store: KBStore) -> None:
    with pytest.raises(MetricsError):
        compute_digest(store, since=NOW, until=NOW - timedelta(days=1), now=NOW)
    with pytest.raises(MetricsError):
        compute_digest(store, stale_after_days=-1, now=NOW)


# --- cli ------------------------------------------------------------------


def test_cli_text_default(store: KBStore, monkeypatch, tmp_path) -> None:
    _seed(store)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["digest", "--since", "all"])
    assert res.exit_code == 0, res.output
    assert "vouch digest" in res.output
    assert "pending review" in res.output
    assert "p_old" in res.output


def test_cli_json_is_stable_schema(store: KBStore, monkeypatch, tmp_path) -> None:
    _seed(store)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["digest", "--since", "7d", "--format", "json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["schema_version"] == 1
    assert doc["pending"]["total"] == 3


def test_cli_markdown(store: KBStore, monkeypatch, tmp_path) -> None:
    _seed(store)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["digest", "--format", "markdown", "--since", "all"])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("## vouch digest")


def test_cli_defaults_exposed(store: KBStore) -> None:
    # guard the documented defaults the help text promises
    assert DEFAULT_SINCE == "7d"
    assert DEFAULT_LIMIT == 10
    assert isinstance(Digest().to_dict(), dict)
