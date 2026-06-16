"""Tests for the semver model-schema migration runner.

Covers every acceptance criterion in #200: forward apply + model-validate,
deterministic plan, byte-equivalent rollback, crash-leaves-prior-version,
pending-proposal precondition, and the named synthetic 0099 manifest. The legacy
integer-format `vouch migrate` is exercised separately in test_migrations.py and
must keep passing (the command is now a group with the legacy behavior on the
no-subcommand path).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import audit
from vouch import migrations as mig
from vouch.cli import cli
from vouch.migrations import runner, schema
from vouch.migrations.manifest import ManifestError, parse_manifest
from vouch.proposals import propose_claim
from vouch.storage import KBStore, _yaml_dump, _yaml_load

FIXTURES = Path(__file__).parent / "fixtures" / "migrations"


@pytest.fixture
def store(tmp_path, monkeypatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _write_claim_yaml(store: KBStore, claim_id: str, data: dict) -> Path:
    """Write a raw claim file (lets us inject legacy/extra fields off-model)."""
    path = store.kb_dir / "claims" / f"{claim_id}.yaml"
    path.write_text(_yaml_dump({"id": claim_id, **data}))
    return path


def _make_manifest(path: Path, **fields) -> None:
    path.write_text(_yaml_dump(fields))


# --- init writes the schema stamp -----------------------------------------


def test_init_writes_schema_version(store: KBStore) -> None:
    assert (store.kb_dir / "schema_version").read_text().strip() == "0.1.0"
    assert schema.read_schema_version(store) == "0.1.0"


def test_absent_schema_version_is_baseline(store: KBStore) -> None:
    (store.kb_dir / "schema_version").unlink()
    assert schema.read_schema_version(store) == "0.1.0"


# --- acceptance #6: the named synthetic 0099 manifest ---------------------


def test_0099_rename_hits_right_files_and_rolls_back(store: KBStore) -> None:
    touched = _write_claim_yaml(store, "c-legacy", {"text": "t", "old_note": "keep-me"})
    untouched = _write_claim_yaml(store, "c-plain", {"text": "t", "evidence": ["x"]})
    before_touched = touched.read_text()
    before_untouched = untouched.read_text()

    plan = runner.plan(store, manifests_dir=FIXTURES)
    assert plan["needed"] is True
    assert plan["steps"][0]["changed"] == ["claims/c-legacy.yaml"]  # only the legacy one

    result = runner.apply(store, manifests_dir=FIXTURES, actor="tester")
    assert result["applied"] is True
    assert schema.read_schema_version(store) == "0.2.0"
    migrated = _yaml_load(touched.read_text())
    assert "old_note" not in migrated and migrated["note_tag"] == "keep-me"
    assert untouched.read_text() == before_untouched  # untouched stays byte-identical

    runner.rollback(store, actor="tester")
    assert schema.read_schema_version(store) == "0.1.0"
    assert touched.read_text() == before_touched  # byte-equivalent restore


# --- acceptance #1: old KB loads cleanly after a multi-step migrate -------


def _chain_manifests(dirpath: Path) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    _make_manifest(
        dirpath / "0001-body-to-text.yaml",
        from_version="0.1.0", to_version="0.2.0", artifact="claims",
        description="rename body -> text (the required field)",
        transforms=[{"rename": {"from": "body", "to": "text"}}],
    )
    _make_manifest(
        dirpath / "0002-default-confidence.yaml",
        from_version="0.2.0", to_version="0.3.0", artifact="claims",
        description="default confidence",
        transforms=[{"default": {"field": "confidence", "value": 0.7}}],
    )
    return dirpath


def test_legacy_kb_loads_cleanly_after_migrate(store: KBStore, tmp_path) -> None:
    manifests = _chain_manifests(tmp_path / "chain")
    # An "old-form" claim: carries `body` instead of the required `text` field,
    # so it fails to model-validate until migrated.
    _write_claim_yaml(store, "c1", {"body": "old fact", "evidence": ["src-1"]})
    before = runner.verify(store)
    assert before["ok"] is False  # missing required `text`

    result = runner.apply(store, to_version="0.3.0", manifests_dir=manifests, actor="t")
    assert result["to_version"] == "0.3.0"
    assert result["manifests"] == ["0001-body-to-text", "0002-default-confidence"]

    after = runner.verify(store)
    assert after["ok"] is True, after["errors"]
    assert schema.read_schema_version(store) == "0.3.0"


# --- acceptance #2: plan is deterministic + byte-identical on repeat ------


def test_plan_is_deterministic(store: KBStore) -> None:
    for i in range(5):
        _write_claim_yaml(store, f"c{i}", {"text": "t", "old_note": str(i)})
    first = json.dumps(runner.plan(store, manifests_dir=FIXTURES), sort_keys=True)
    second = json.dumps(runner.plan(store, manifests_dir=FIXTURES), sort_keys=True)
    assert first == second


# --- acceptance #3: apply -> rollback is byte-equivalent ------------------


def test_apply_then_rollback_byte_equivalent(store: KBStore) -> None:
    for i in range(3):
        _write_claim_yaml(store, f"c{i}", {"text": "t", "old_note": f"n{i}"})
    snapshot = {p.name: p.read_text() for p in (store.kb_dir / "claims").glob("*.yaml")}

    runner.apply(store, manifests_dir=FIXTURES, actor="t")
    runner.rollback(store, actor="t")

    restored = {p.name: p.read_text() for p in (store.kb_dir / "claims").glob("*.yaml")}
    assert restored == snapshot


# --- acceptance #4: interrupt mid-run leaves prior version ----------------


def test_crash_mid_apply_leaves_prior_version(store: KBStore) -> None:
    for i in range(3):
        _write_claim_yaml(store, f"c{i}", {"text": "t", "old_note": f"n{i}"})
    snapshot = {p.name: p.read_text() for p in (store.kb_dir / "claims").glob("*.yaml")}

    with pytest.raises(runner.CrashSimulated):
        runner.apply(store, manifests_dir=FIXTURES, actor="t", _fail_after=1)

    # version stamp is bumped last, so a crash leaves the KB at the prior version
    assert schema.read_schema_version(store) == "0.1.0"
    # ...and the journal lets rollback restore the partially-written files exactly
    runner.rollback(store, actor="t")
    restored = {p.name: p.read_text() for p in (store.kb_dir / "claims").glob("*.yaml")}
    assert restored == snapshot
    assert schema.read_schema_version(store) == "0.1.0"


# --- acceptance #5: pending proposals block apply -------------------------


def test_apply_refuses_with_pending_proposals(store: KBStore) -> None:
    src = store.put_source(b"a source")
    propose_claim(store, text="pending one", evidence=[src.id], proposed_by="agent")
    _write_claim_yaml(store, "c-legacy", {"text": "t", "old_note": "x"})

    with pytest.raises(mig.MigrationError) as exc:
        runner.apply(store, manifests_dir=FIXTURES, actor="t")
    assert "list-pending" in str(exc.value)


# --- audit + status -------------------------------------------------------


def test_apply_audits_and_status_reports(store: KBStore) -> None:
    _write_claim_yaml(store, "c-legacy", {"text": "t", "old_note": "x"})
    runner.apply(store, manifests_dir=FIXTURES, actor="tester")
    events = [e for e in audit.read_events(store.kb_dir) if e.event == "kb.migrate.apply"]
    assert events and events[-1].actor == "tester"

    st = runner.status(store, manifests_dir=FIXTURES)
    assert st["schema_version"] == "0.2.0"
    assert st["up_to_date"] is True


# --- manifest validation --------------------------------------------------


def test_manifest_rejects_bad_verb(tmp_path) -> None:
    p = tmp_path / "0001-bad.yaml"
    _make_manifest(p, from_version="0.1.0", to_version="0.2.0", artifact="claims",
                   transforms=[{"frobnicate": {"x": 1}}])
    with pytest.raises(ManifestError):
        parse_manifest(p)


def test_manifest_rejects_backwards_version(tmp_path) -> None:
    p = tmp_path / "0001-back.yaml"
    _make_manifest(p, from_version="0.3.0", to_version="0.2.0", artifact="claims",
                   transforms=[])
    with pytest.raises(ManifestError):
        parse_manifest(p)


# --- CLI ------------------------------------------------------------------


def test_cli_migrate_status_up_to_date(store: KBStore) -> None:
    res = CliRunner().invoke(cli, ["migrate", "status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["schema_version"] == "0.1.0" and data["up_to_date"] is True


def test_cli_migrate_apply_and_verify(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_MIGRATIONS_DIR", str(FIXTURES))
    _write_claim_yaml(store, "c-legacy", {"text": "t", "old_note": "x"})

    res = CliRunner().invoke(cli, ["migrate", "apply", "--yes", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["to_version"] == "0.2.0"

    res2 = CliRunner().invoke(cli, ["migrate", "verify", "--json"])
    assert res2.exit_code == 0, res2.output
    assert json.loads(res2.output)["ok"] is True


def test_cli_migrate_plan_lists_files(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_MIGRATIONS_DIR", str(FIXTURES))
    _write_claim_yaml(store, "c-legacy", {"text": "t", "old_note": "x"})
    res = CliRunner().invoke(cli, ["migrate", "plan"])
    assert res.exit_code == 0, res.output
    assert "claims/c-legacy.yaml" in res.output


def test_cli_legacy_migrate_still_works(store: KBStore) -> None:
    # `vouch migrate` with no subcommand keeps the legacy integer-format behavior.
    res = CliRunner().invoke(cli, ["migrate", "--check"])
    assert res.exit_code == 0, res.output
    assert "KB format" in res.output
