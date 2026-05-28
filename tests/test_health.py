"""Lint, doctor, and metrics health checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vouch import health
from vouch.models import Claim, ClaimStatus, Proposal, ProposalKind, ProposalStatus, Relation
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_lint_finds_broken_citation_when_source_removed(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    (store.kb_dir / "sources" / src.id / "meta.yaml").unlink()
    report = health.lint(store)
    codes = {f.code for f in report.findings}
    assert "broken_citation" in codes
    assert report.ok is False


def test_lint_dangling_relation(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    store.put_relation(Relation(id="rel-x", source="c1", relation="uses", target="ghost"))
    report = health.lint(store)
    codes = {f.code for f in report.findings}
    assert "dangling_relation" in codes


def test_doctor_runs_full_sweep(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    report = health.doctor(store)
    # Clean KB → ok=True (info-level "index_missing" doesn't fail).
    assert report.ok is True


def test_metrics_approval_rate(store: KBStore) -> None:
    assert health.metrics(store)["approval_rate"] is None

    store.put_proposal(
        Proposal(
            id="p1",
            kind=ProposalKind.CLAIM,
            proposed_by="a",
            payload={},
            status=ProposalStatus.APPROVED,
        )
    )
    assert health.metrics(store)["approval_rate"] == 1.0

    store.put_proposal(
        Proposal(
            id="p2",
            kind=ProposalKind.CLAIM,
            proposed_by="a",
            payload={},
            status=ProposalStatus.REJECTED,
        )
    )
    assert health.metrics(store)["approval_rate"] == 0.5

    store.put_proposal(
        Proposal(
            id="p3",
            kind=ProposalKind.CLAIM,
            proposed_by="a",
            payload={},
            status=ProposalStatus.REJECTED,
        )
    )
    assert health.metrics(store)["approval_rate"] == 1 / 3


def test_metrics_citation_coverage(store: KBStore) -> None:
    src = store.put_source(b"e")
    assert health.metrics(store)["citation_coverage"] is None

    store.put_claim(Claim(id="c1", text="uncited"))
    assert health.metrics(store)["citation_coverage"] == 0.0

    store.put_claim(Claim(id="c2", text="cited", evidence=[src.id]))
    assert health.metrics(store)["citation_coverage"] == 0.5


def test_metrics_stale_ratio(store: KBStore) -> None:
    assert health.metrics(store)["stale_ratio"] is None

    old = datetime.now(UTC) - timedelta(days=200)
    fresh = datetime.now(UTC)

    store.put_claim(Claim(id="c1", text="stale claim", created_at=old, updated_at=old))
    store.put_claim(Claim(id="c2", text="fresh claim", created_at=fresh, updated_at=fresh))
    assert health.metrics(store)["stale_ratio"] == 0.5

    store.put_claim(
        Claim(
            id="c3",
            text="archived stale",
            created_at=old,
            updated_at=old,
            status=ClaimStatus.ARCHIVED,
        )
    )
    assert health.metrics(store)["stale_ratio"] == 0.5


def test_metrics_stale_uses_last_confirmed_at(store: KBStore) -> None:
    old = datetime.now(UTC) - timedelta(days=200)
    recent = datetime.now(UTC) - timedelta(days=30)

    store.put_claim(
        Claim(
            id="c1",
            text="old with recent confirm",
            created_at=old,
            updated_at=old,
            last_confirmed_at=recent,
        )
    )
    assert health.metrics(store)["stale_ratio"] == 0.0


def test_status_includes_metrics_keys(store: KBStore) -> None:
    result = health.status(store)
    for key in ("approval_rate", "citation_coverage", "stale_ratio"):
        assert key in result


def test_lint_surfaces_legacy_uncited_claim_yaml_without_crashing(
    store: KBStore,
) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="good", text="t", evidence=[src.id]))

    legacy_uncited = (
        "id: legacy\n"
        'text: "shipped before the validator existed"\n'
        "type: fact\n"
        "status: stable\n"
        "confidence: 1.0\n"
        "evidence: []\n"
    )
    (store.kb_dir / "claims" / "legacy.yaml").write_text(legacy_uncited)

    report = health.lint(store)
    codes = {f.code for f in report.findings}
    assert "invalid_claim" in codes, [f.message for f in report.findings]
    invalid = next(f for f in report.findings if f.code == "invalid_claim")
    assert "legacy" in invalid.object_ids
    assert "delete the file" in invalid.message or "add a citation" in invalid.message
    assert report.ok is False

    good_findings = [f for f in report.findings if "good" in f.object_ids]
    assert all(f.severity != "error" for f in good_findings), good_findings


def test_list_claims_filtered_by_status(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id], status=ClaimStatus.STABLE))
    store.put_claim(Claim(id="c2", text="y", evidence=[src.id], status=ClaimStatus.ARCHIVED))
    stable = [c for c in store.list_claims() if c.status == ClaimStatus.STABLE]
    assert [c.id for c in stable] == ["c1"]
