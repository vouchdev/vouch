"""Lint and doctor health checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import health
from vouch.models import Claim, ClaimStatus, Relation
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
    store.put_relation(Relation(id="rel-x", source="c1",
                                relation="uses", target="ghost"))
    report = health.lint(store)
    codes = {f.code for f in report.findings}
    assert "dangling_relation" in codes


def test_doctor_runs_full_sweep(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    report = health.doctor(store)
    # Clean KB → ok=True (info-level "index_missing" doesn't fail).
    assert report.ok is True


def test_list_claims_filtered_by_status(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id],
                          status=ClaimStatus.STABLE))
    store.put_claim(Claim(id="c2", text="y", evidence=[src.id],
                          status=ClaimStatus.ARCHIVED))
    stable = [c for c in store.list_claims() if c.status == ClaimStatus.STABLE]
    assert [c.id for c in stable] == ["c1"]
