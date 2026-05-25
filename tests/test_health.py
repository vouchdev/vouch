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


def test_lint_surfaces_legacy_uncited_claim_yaml_without_crashing(
    store: KBStore,
) -> None:
    """Regression for the #82 review: after the Claim.evidence min-citation
    validator landed (#81), a KB that already had an uncited claim on
    disk from before the fix would crash `vouch lint` / `vouch doctor`
    with a bare pydantic.ValidationError. Lint now skips invalid YAMLs
    per-file and surfaces them as `invalid_claim` findings so the user
    has a clear repair hint (edit the YAML to add a citation, or delete
    the file)."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="good", text="t", evidence=[src.id]))

    # Hand-craft an uncited claim YAML that the *current* model rejects —
    # matches the on-disk shape an older buggy write path could have left.
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
    assert report.ok is False  # invalid_claim is severity=error

    # The good claim is still discoverable — lint didn't bail out at the
    # bad one, so the rest of the sweep still ran.
    good_findings = [f for f in report.findings if "good" in f.object_ids]
    # No errors about the good claim itself (it's well-formed and cites a
    # present source).
    assert all(f.severity != "error" for f in good_findings), good_findings


def test_list_claims_filtered_by_status(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id],
                          status=ClaimStatus.STABLE))
    store.put_claim(Claim(id="c2", text="y", evidence=[src.id],
                          status=ClaimStatus.ARCHIVED))
    stable = [c for c in store.list_claims() if c.status == ClaimStatus.STABLE]
    assert [c.id for c in stable] == ["c1"]
