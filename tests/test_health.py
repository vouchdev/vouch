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


def test_doctor_warns_on_missing_external_file(store: KBStore, tmp_path: Path) -> None:
    """source verify marks missing externals as '!'; doctor must surface
    them too (not only drift)."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"original")
    src = store.put_source(
        f.read_bytes(), title="doc",
        locator=str(f.resolve()), source_type="file",
    )
    f.unlink()
    report = health.doctor(store)
    missing = [f for f in report.findings if f.code == "source_missing"]
    assert missing, [f.code for f in report.findings]
    assert missing[0].severity == "warning"
    assert src.id in missing[0].object_ids
    # warning-only — same posture as source_drift
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


def test_lint_surfaces_unreadable_source_meta(store: KBStore) -> None:
    """A source meta.yaml can carry a raw C1 control character — e.g. a
    hand edit or an external writer under a mismatched locale landing a
    utf-8 em dash double-decoded into latin-1 mojibake. pyyaml's reader
    rejects the character outright, and storage._load_or_skip
    deliberately skips the file so bulk listings survive. For lint that
    skip is the bug: the corrupt meta silently vanishes from the sweep,
    and any claim citing the source is misreported as broken_citation.
    Lint must surface the file as an `unreadable_source` error and keep
    citation checks honest — the source id is its directory name,
    readable even when the yaml is not."""
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    meta = store.kb_dir / "sources" / src.id / "meta.yaml"
    meta.write_text(
        meta.read_text(encoding="utf-8") + "title: conversation \u00e2\u0080\u0094 vouch\n",
        encoding="utf-8",
    )

    report = health.lint(store)

    unreadable = [f for f in report.findings if f.code == "unreadable_source"]
    assert unreadable, [f.code for f in report.findings]
    assert src.id in unreadable[0].object_ids
    assert report.ok is False  # unreadable_source is severity=error
    # The source directory still exists — the citation isn't broken, the
    # meta is. Reporting broken_citation here would send the user hunting
    # for a missing source that is right there on disk.
    codes = {f.code for f in report.findings}
    assert "broken_citation" not in codes


def test_list_claims_filtered_by_status(store: KBStore) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="x", evidence=[src.id],
                          status=ClaimStatus.STABLE))
    store.put_claim(Claim(id="c2", text="y", evidence=[src.id],
                          status=ClaimStatus.ARCHIVED))
    stable = [c for c in store.list_claims() if c.status == ClaimStatus.STABLE]
    assert [c.id for c in stable] == ["c1"]


def test_lint_exempts_retired_claims_from_stale_check(store: KBStore) -> None:
    """Retired claims (archived/superseded/redacted) are terminal — they are
    not expected to be refreshed, so lint must not flag them as stale,
    matching metrics and digest (issue #478)."""
    from datetime import UTC, datetime, timedelta

    src = store.put_source(b"e")
    # An archived claim 400 days old — well past the 180-day stale threshold.
    old = datetime.now(UTC) - timedelta(days=400)
    _write_claim_direct(store, Claim(
        id="c1", text="t", evidence=[src.id],
        status=ClaimStatus.ARCHIVED,
        created_at=old,
        updated_at=old,
    ))
    report = health.lint(store)
    codes = {f.code for f in report.findings}
    assert "stale_claim" not in codes, (
        f"lint should not flag retired claims as stale; got: {codes}"
    )


def test_lint_flags_stale_active_claims(store: KBStore) -> None:
    """Active (non-retired) claims past the stale threshold are still flagged."""
    from datetime import UTC, datetime, timedelta

    src = store.put_source(b"e")
    old = datetime.now(UTC) - timedelta(days=400)
    _write_claim_direct(store, Claim(
        id="c1", text="t", evidence=[src.id],
        status=ClaimStatus.STABLE,
        created_at=old,
        updated_at=old,
    ))
    report = health.lint(store)
    codes = {f.code for f in report.findings}
    assert "stale_claim" in codes


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


# --- kb.fsck: agent-facing surfaces (MCP/JSONL/CLI) -------------------------
#
# health.fsck() itself is exercised throughout this file; these tests cover
# only the newly-registered kb.fsck surfaces (it was CLI-only before, with
# no MCP tool, JSONL handler, or capabilities.METHODS entry).


def test_jsonl_fsck_matches_direct_call(store: KBStore, monkeypatch) -> None:
    from vouch.jsonl_server import handle_request

    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    monkeypatch.chdir(store.root)

    resp = handle_request({"id": "f1", "method": "kb.fsck", "params": {}})
    assert resp["ok"] is True

    direct = health.fsck(store)
    assert resp["result"]["ok"] == direct.ok
    assert {f["code"] for f in resp["result"]["findings"]} == {
        f.code for f in direct.findings
    }
    assert resp["result"]["counts"]["claims"] == direct.counts["claims"]


def test_jsonl_fsck_surfaces_findings_not_just_ok_flag(
    store: KBStore, monkeypatch,
) -> None:
    """A real finding (not just the ok flag) must round-trip through the
    JSONL envelope — confirms the handler forwards the full findings list,
    not a summarized/truncated version."""
    from vouch.jsonl_server import handle_request

    src = store.put_source(b"e")
    c = Claim(id="real", text="t", evidence=[src.id])
    store.put_claim(c)
    _index_claim(store, c)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id="ghost", text="x", type="fact", status="working", tags=[],
        )
    monkeypatch.chdir(store.root)

    resp = handle_request({"id": "f2", "method": "kb.fsck", "params": {}})
    assert resp["ok"] is True
    codes = {f["code"] for f in resp["result"]["findings"]}
    assert "index_orphan_claim" in codes


def test_mcp_surface_serves_fsck(store: KBStore, monkeypatch) -> None:
    from vouch import server

    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    monkeypatch.setattr(server, "_store", lambda: store)

    result = server.kb_fsck()
    direct = health.fsck(store)
    assert result["ok"] == direct.ok
    assert {f["code"] for f in result["findings"]} == {f.code for f in direct.findings}
    assert result["counts"]["claims"] == direct.counts["claims"]


def test_cli_fsck_registered_as_kb_fsck_method() -> None:
    """kb.fsck's default CLI mirror rule (kb.foo -> vouch foo) must resolve
    to the pre-existing `fsck` command rather than needing a new one — this
    pins that `_CLI_MIRRORS` correctly has no entry for kb.fsck."""
    from tests.test_capabilities import _CLI_MIRRORS
    from vouch import capabilities

    assert "kb.fsck" in capabilities.METHODS
    assert "kb.fsck" not in _CLI_MIRRORS


def test_receipt_coverage_fidelity_number(store: KBStore) -> None:
    from vouch.extract import ingest_source
    from vouch.models import Claim

    assert health.receipt_coverage(store) == {
        "live_claims": 0, "receipted": 0, "ratio": None,
    }
    # a receipt-backed claim (cites an Evidence record)
    ingest_source(
        store,
        b"The deploy window opens every wednesday at nine in the morning.",
        proposed_by="test",
    )
    cov = health.receipt_coverage(store)
    assert cov["live_claims"] == cov["receipted"] == 1
    assert cov["ratio"] == 1.0
    # a plain claim citing only a source — no receipt
    src = store.put_source(b"unquoted background material")
    store.put_claim(Claim(id="plain", text="a plain claim", evidence=[src.id]))
    cov = health.receipt_coverage(store)
    assert cov == {"live_claims": 2, "receipted": 1, "ratio": 0.5}
    assert health.status(store)["receipt_coverage"] == cov
