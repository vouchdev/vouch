"""Lint and doctor health checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import health, index_db
from vouch.models import Claim, ClaimStatus, Proposal, ProposalKind, ProposalStatus
from vouch.storage import KBStore, _yaml_dump


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
    # Hand-write the YAML to simulate a relation that landed before
    # `put_relation` enforced endpoint existence (or one introduced
    # by a manual edit). `health.lint` is the after-the-fact safety
    # net for exactly this case; the on-disk YAML is still a
    # `dangling_relation` finding even though no current write path
    # would land it.
    dangling_yaml = (
        "id: rel-x\n"
        "source: c1\n"
        "relation: uses\n"
        "target: ghost\n"
        "confidence: 0.7\n"
        "evidence: []\n"
        "created_at: '2026-05-27T00:00:00+00:00'\n"
        "updated_at: '2026-05-27T00:00:00+00:00'\n"
    )
    (store.kb_dir / "relations" / "rel-x.yaml").write_text(dangling_yaml)
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


# --- fsck ----------------------------------------------------------------


def _index_claim(store: KBStore, claim: Claim) -> None:
    """Write the FTS5 row for `claim` so fsck sees a healthy index baseline."""
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id=claim.id, text=claim.text,
            type=claim.type.value, status=claim.status.value, tags=claim.tags,
        )


def _write_claim_direct(store: KBStore, claim: Claim) -> None:
    """Persist a claim straight to disk, bypassing put_claim's reference
    guard (`_validate_claim_refs`). Simulates a poisoned / legacy claim YAML
    that landed before the guard existed — exactly the on-disk state fsck's
    dangling_* checks must still surface after the write path is tightened."""
    (store.kb_dir / "claims" / f"{claim.id}.yaml").write_text(
        _yaml_dump(claim.model_dump(mode="json"))
    )


def test_fsck_clean_kb_passes(store: KBStore) -> None:
    """A KB with one consistently-indexed claim is fsck-clean."""
    src = store.put_source(b"e")
    c = Claim(id="c1", text="t", evidence=[src.id])
    store.put_claim(c)
    _index_claim(store, c)
    report = health.fsck(store)
    assert report.ok is True
    assert all(f.severity != "error" for f in report.findings)


def test_fsck_flags_dangling_supersedes(store: KBStore) -> None:
    """`claim.supersedes` pointing at a missing claim is an error.

    Written directly to disk: put_claim now rejects dangling graph refs
    (`_validate_claim_refs`), so this reproduces the legacy/poisoned on-disk
    YAML that fsck must still catch after the write path is tightened."""
    src = store.put_source(b"e")
    _write_claim_direct(store, Claim(id="c1", text="t", evidence=[src.id],
                                     supersedes=["ghost"]))
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "dangling_supersedes" in codes
    assert report.ok is False


def test_fsck_flags_dangling_superseded_by(store: KBStore) -> None:
    """`claim.superseded_by` pointing at a missing claim is an error."""
    src = store.put_source(b"e")
    _write_claim_direct(store, Claim(id="c1", text="t", evidence=[src.id],
                                     superseded_by="ghost"))
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "dangling_superseded_by" in codes
    assert report.ok is False


def test_fsck_flags_dangling_contradicts(store: KBStore) -> None:
    """`claim.contradicts` pointing at a missing claim is an error."""
    src = store.put_source(b"e")
    _write_claim_direct(store, Claim(id="c1", text="t", evidence=[src.id],
                                     contradicts=["ghost"]))
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "dangling_contradicts" in codes
    assert report.ok is False


def test_fsck_flags_asymmetric_contradicts(store: KBStore) -> None:
    """A → B contradiction not mirrored by B → A is a warning, not silent."""
    src = store.put_source(b"e")
    # c2 must exist before c1 cites it — put_claim now enforces resolvable
    # graph refs, and an asymmetric (not dangling) link needs both ends real.
    store.put_claim(Claim(id="c2", text="b", evidence=[src.id]))
    store.put_claim(Claim(id="c1", text="a", evidence=[src.id],
                          contradicts=["c2"]))
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "asymmetric_contradicts" in codes


def test_fsck_flags_dangling_claim_entity(store: KBStore) -> None:
    """`claim.entities` pointing at a missing entity is an error finding."""
    src = store.put_source(b"e")
    _write_claim_direct(store, Claim(
        id="c1", text="t", evidence=[src.id], entities=["ghost-entity"],
    ))
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "dangling_claim_entity" in codes
    assert report.ok is False


def test_fsck_decided_missing_artifact(store: KBStore) -> None:
    """An approved decided proposal whose artifact is gone is reported."""
    store.put_proposal(Proposal(
        id="prop-1",
        kind=ProposalKind.CLAIM,
        proposed_by="agent",
        payload={"id": "vanished", "text": "t", "evidence": ["e1"]},
        status=ProposalStatus.APPROVED,
    ))
    # Move it to decided/ so list_proposals finds it as approved.
    src_path = store.kb_dir / "proposed" / "prop-1.yaml"
    dst_path = store.kb_dir / "decided" / "prop-1.yaml"
    dst_path.write_text(src_path.read_text())
    src_path.unlink()

    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "decided_missing_artifact" in codes


def test_fsck_index_orphan_row(store: KBStore) -> None:
    """An FTS5 row with no on-disk claim is reported as an index orphan."""
    src = store.put_source(b"e")
    c = Claim(id="real", text="t", evidence=[src.id])
    store.put_claim(c)
    _index_claim(store, c)
    # Inject a row for a claim that doesn't exist on disk.
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id="ghost", text="x",
            type="fact", status="working", tags=[],
        )
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "index_orphan_claim" in codes


def test_fsck_index_missing_row(store: KBStore) -> None:
    """A claim on disk that never made it into FTS5 is reported."""
    src = store.put_source(b"e")
    c = Claim(id="unindexed", text="t", evidence=[src.id])
    store.put_claim(c)
    # State.db exists but the row was never written.
    with index_db.open_db(store.kb_dir) as _conn:
        pass
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "index_missing_row" in codes


def test_fsck_index_status_drift(store: KBStore) -> None:
    """Regression cover for #78: status on disk vs FTS5 must agree."""
    src = store.put_source(b"e")
    c = Claim(id="drifty", text="t", evidence=[src.id],
              status=ClaimStatus.STABLE)
    store.put_claim(c)
    # Index says working, disk says stable — the #78 failure shape.
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id=c.id, text=c.text, type=c.type.value,
            status="working", tags=c.tags,
        )
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "index_status_drift" in codes


def test_fsck_orphan_embedding(store: KBStore) -> None:
    """An embedding row for a kind/id with no artifact on disk is flagged."""
    src = store.put_source(b"e")
    c = Claim(id="real", text="t", evidence=[src.id])
    store.put_claim(c)
    _index_claim(store, c)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_embedding(conn, kind="claim", id="ghost", vec=[0.1, 0.2])
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "orphan_embedding" in codes


def test_fsck_surfaces_invalid_claim_yaml_without_crashing(
    store: KBStore,
) -> None:
    """fsck opens by loading every claim; a single invalid YAML (e.g. a
    legacy uncited claim from before #81) must become an `invalid_claim`
    finding rather than aborting the deep check with a traceback — that
    bad YAML is exactly the inconsistency fsck should surface. Reuses the
    same per-file loader as lint."""
    src = store.put_source(b"e")
    good = Claim(id="good", text="t", evidence=[src.id])
    store.put_claim(good)
    _index_claim(store, good)

    legacy_uncited = (
        "id: legacy\n"
        'text: "shipped before the validator existed"\n'
        "type: fact\n"
        "status: stable\n"
        "confidence: 1.0\n"
        "evidence: []\n"
    )
    (store.kb_dir / "claims" / "legacy.yaml").write_text(legacy_uncited)

    report = health.fsck(store)  # must not raise
    codes = {f.code for f in report.findings}
    assert "invalid_claim" in codes, [f.message for f in report.findings]
    invalid = next(f for f in report.findings if f.code == "invalid_claim")
    assert "legacy" in invalid.object_ids
    assert report.ok is False  # invalid_claim is severity=error
    # counts reflect only the safely-loaded claim — building them didn't
    # re-trip the strict loader on the bad YAML.
    assert report.counts["claims"] == 1


def test_fsck_without_state_db_reports_info(store: KBStore) -> None:
    """No state.db → info-level `index_missing`, report stays ok."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    # The embedding write-hook may auto-create state.db on put_claim; this
    # test verifies the explicit "no index yet" path.
    db_path = store.kb_dir / index_db.DB_FILENAME
    if db_path.exists():
        db_path.unlink()
    report = health.fsck(store)
    codes = {f.code for f in report.findings}
    assert "index_missing" in codes
    # info finding alone shouldn't fail the report.
    assert report.ok is True
