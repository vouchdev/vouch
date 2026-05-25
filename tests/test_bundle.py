"""Portable bundle export / import round-trip."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from vouch import bundle
from vouch.models import Claim, Page
from vouch.storage import KBStore

_UNSAFE_PATH_RE = r"traversal|absolute path|nul byte|unsafe path|empty path"


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_export_import_round_trip(store: KBStore, tmp_path: Path) -> None:
    src = store.put_source(b"e", title="doc")
    store.put_claim(Claim(id="c1", text="alpha", evidence=[src.id]))
    store.put_page(Page(id="p1", title="Page one"))
    bundle_path = tmp_path / "out.tar.gz"
    manifest = bundle.export(store.kb_dir, dest=bundle_path)
    assert bundle_path.exists()
    assert manifest["counts"]["claims"] == 1
    chk = bundle.export_check(bundle_path)
    assert chk.ok

    dest_root = tmp_path / "dest"
    dest = KBStore.init(dest_root)
    diff = bundle.import_check(dest.kb_dir, bundle_path)
    assert diff.ok
    assert diff.conflicts == []
    assert len(diff.new_files) >= 3
    result = bundle.import_apply(dest.kb_dir, bundle_path)
    assert result["bundle_id"] == manifest["bundle_id"]
    assert len(result["written"]) >= 3
    assert dest.get_claim("c1").text == "alpha"


def test_import_apply_skips_conflicts_by_default(store: KBStore, tmp_path: Path) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="first", evidence=[src.id]))
    bundle_path = tmp_path / "b.tar.gz"
    bundle.export(store.kb_dir, dest=bundle_path)
    c = store.get_claim("c1")
    c.text = "changed"
    store.update_claim(c)
    result = bundle.import_apply(store.kb_dir, bundle_path, on_conflict="skip")
    assert result["skipped_conflicts"]
    assert store.get_claim("c1").text == "changed"


def test_import_apply_fails_when_requested(store: KBStore, tmp_path: Path) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="first", evidence=[src.id]))
    bundle_path = tmp_path / "b.tar.gz"
    bundle.export(store.kb_dir, dest=bundle_path)
    c = store.get_claim("c1")
    c.text = "changed"
    store.update_claim(c)
    with pytest.raises(RuntimeError, match="conflicts"):
        bundle.import_apply(store.kb_dir, bundle_path, on_conflict="fail")


def _write_malicious_bundle(bundle_path: Path, member_name: str, payload: bytes) -> None:
    """Build a tarball with a single attacker-named member + matching manifest."""
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": "deadbeef",
        "files": [
            {
                "path": member_name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False, "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))


def test_import_apply_rejects_path_traversal(store: KBStore, tmp_path: Path) -> None:
    """CVE-2007-4559 / issue #9: tar member names with `../` must not escape kb_dir."""
    bundle_path = tmp_path / "evil.tar.gz"
    _write_malicious_bundle(bundle_path, "../../evil.txt", b"pwned")

    canary = tmp_path.parent / "evil.txt"
    canary_existed = canary.exists()
    try:
        with pytest.raises(RuntimeError, match=_UNSAFE_PATH_RE):
            bundle.import_apply(store.kb_dir, bundle_path)
        assert canary.exists() == canary_existed, "import wrote outside kb_dir"
    finally:
        if not canary_existed and canary.exists():
            canary.unlink()


def test_import_apply_rejects_absolute_path(store: KBStore, tmp_path: Path) -> None:
    target = tmp_path / "absolute-victim.txt"
    bundle_path = tmp_path / "abs.tar.gz"
    _write_malicious_bundle(bundle_path, str(target), b"pwned")
    with pytest.raises(RuntimeError, match=_UNSAFE_PATH_RE):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not target.exists()


def test_import_check_flags_path_traversal(store: KBStore, tmp_path: Path) -> None:
    bundle_path = tmp_path / "evil.tar.gz"
    _write_malicious_bundle(bundle_path, "../../evil.txt", b"pwned")
    result = bundle.import_check(store.kb_dir, bundle_path)
    assert not result.ok
    assert any(
        "traversal" in i or "unsafe" in i or "absolute path" in i
        for i in result.issues
    )


def _write_hash_mismatched_bundle(
    bundle_path: Path,
    member_name: str,
    declared_payload: bytes,
    actual_payload: bytes,
) -> None:
    """Build a bundle where the manifest records the sha256 of
    `declared_payload` but the tar member at the same path contains
    `actual_payload`. Models the smallest possible integrity attack:
    swap a member's body without re-signing the manifest."""
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": "deadbeef",
        "files": [
            {
                "path": member_name,
                "size": len(declared_payload),
                "sha256": hashlib.sha256(declared_payload).hexdigest(),
            }
        ],
        "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False, "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        info.size = len(actual_payload)
        tar.addfile(info, io.BytesIO(actual_payload))
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))


def test_import_rejects_member_with_mismatched_sha256(
    store: KBStore, tmp_path: Path
) -> None:
    """Regression for #74: a tar member whose body does not hash to the
    sha256 the manifest claims is a documented integrity violation —
    export_check flags it, so import_check and import_apply must too."""
    legitimate = b"text: original\n"
    tampered = b"text: TAMPERED\n"
    bundle_path = tmp_path / "tampered.tar.gz"
    _write_hash_mismatched_bundle(bundle_path, "claims/c1.yaml", legitimate, tampered)

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("hash mismatch" in i for i in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match="hash mismatch"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "claims" / "c1.yaml").exists()


def test_import_rejects_source_content_mismatch(
    store: KBStore, tmp_path: Path
) -> None:
    """`_validate_content` skips `sources/*/content` files, so the manifest
    sha256 is the only thing that can detect substituted source bytes."""
    legitimate = b"original source bytes"
    tampered = b"attacker-controlled bytes"
    bundle_path = tmp_path / "tampered.tar.gz"
    _write_hash_mismatched_bundle(
        bundle_path, "sources/deadbeef/content", legitimate, tampered
    )

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("hash mismatch" in i for i in diff.issues)

    with pytest.raises(RuntimeError, match="hash mismatch"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "sources" / "deadbeef" / "content").exists()


def test_import_check_passes_when_member_matches_manifest(
    store: KBStore, tmp_path: Path
) -> None:
    """The hash check is positive too: a member that matches manifest
    sha256 should not be reported as `hash mismatch`."""
    payload = b"text: original\n"
    bundle_path = tmp_path / "good.tar.gz"
    _write_hash_mismatched_bundle(bundle_path, "claims/c1.yaml", payload, payload)

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not any("hash mismatch" in i for i in diff.issues), diff.issues
