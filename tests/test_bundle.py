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


def test_import_rejects_bundle_carrying_decided_members(
    store: KBStore, tmp_path: Path
) -> None:
    """A bundle with decided/ members is a write past the review gate.

    decided/ holds approved decisions; import writes members straight to disk,
    so importing decided/ lands approved claims/pages without a receiving-side
    proposal. Until gated import exists (roadmap 8.2), any such bundle is
    refused — by import_check (issue) and therefore import_apply (raises).
    """
    from vouch.proposals import approve, propose_claim

    src = store.put_source(b"e", title="d")
    pid = propose_claim(
        store, text="alpha", evidence=[src.id], proposed_by="a"
    ).id
    approve(store, pid, approved_by="b")
    assert any((store.kb_dir / "decided").glob("*")), "decided/ should be populated"

    bundle_path = tmp_path / "out.tar.gz"
    bundle.export(store.kb_dir, dest=bundle_path)

    dest = KBStore.init(tmp_path / "dest")
    diff = bundle.import_check(dest.kb_dir, bundle_path)
    assert not diff.ok
    assert any("decided" in issue for issue in diff.issues)
    with pytest.raises(RuntimeError):
        bundle.import_apply(dest.kb_dir, bundle_path)


def test_manifest_paths_match_tar_member_names(store: KBStore, tmp_path: Path) -> None:
    # Regression for the Windows path-separator bug: when build_manifest used
    # str(rel) the manifest stored "sources\<sha>\meta.yaml" while the tar
    # member name was "sources/<sha>/meta.yaml", silently breaking both
    # export_check and import_apply on every Windows host.
    src = store.put_source(b"e", title="doc")
    store.put_claim(Claim(id="c1", text="alpha", evidence=[src.id]))
    store.put_page(Page(id="p1", title="Page one"))
    bundle_path = tmp_path / "out.tar.gz"
    manifest = bundle.export(store.kb_dir, dest=bundle_path)

    for f in manifest["files"]:
        assert "\\" not in f["path"], f"manifest path uses native separator: {f['path']!r}"

    with tarfile.open(bundle_path, "r:gz") as tar:
        member_names = {m.name for m in tar.getmembers() if m.isfile()} - {bundle.MANIFEST_NAME}
    manifest_paths = {f["path"] for f in manifest["files"]}
    assert member_names == manifest_paths

    assert manifest["counts"]["claims"] == 1
    assert manifest["counts"]["pages"] == 1
    assert manifest["counts"]["sources"] == 2


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


def _write_malicious_bundle(
    bundle_path: Path,
    member_name: str,
    payload: bytes,
    *,
    safety: dict[str, bool] | None = None,
) -> None:
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
        "safety": safety
        or {"has_proposed": False, "has_state_db": False, "has_audit_log": False},
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


def test_import_check_rejects_manifest_listing_missing_file(store: KBStore, tmp_path: Path) -> None:
    """Manifest entries without a matching tar member must be flagged."""
    bundle_path = tmp_path / "missing.tar.gz"
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": "deadbeef",
        "files": [
            {
                "path": "claims/c1.yaml",
                "size": 16,
                "sha256": hashlib.sha256(b"text: any\n").hexdigest(),
            },
        ],
        "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False, "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))

    result = bundle.import_check(store.kb_dir, bundle_path)
    assert not result.ok
    assert any("missing file" in i for i in result.issues)


def test_import_apply_rejects_bundle_with_missing_manifest_file(
    store: KBStore, tmp_path: Path
) -> None:
    """import_apply must refuse a bundle whose manifest lists a file absent from the tarball."""
    bundle_path = tmp_path / "missing.tar.gz"
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": "deadbeef",
        "files": [
            {
                "path": "claims/c1.yaml",
                "size": 16,
                "sha256": hashlib.sha256(b"text: any\n").hexdigest(),
            },
        ],
        "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False, "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))

    with pytest.raises(RuntimeError, match="missing file"):
        bundle.import_apply(store.kb_dir, bundle_path)


def test_import_check_flags_path_traversal(store: KBStore, tmp_path: Path) -> None:
    bundle_path = tmp_path / "evil.tar.gz"
    _write_malicious_bundle(bundle_path, "../../evil.txt", b"pwned")
    result = bundle.import_check(store.kb_dir, bundle_path)
    assert not result.ok
    assert any("traversal" in i or "unsafe" in i or "absolute path" in i for i in result.issues)


@pytest.mark.parametrize(
    ("member_name", "payload", "expected"),
    [
        ("audit.log.jsonl", b'{"event":"fake"}\n', "audit.log.jsonl"),
        ("state.db", b"sqlite bytes", "state.db"),
        ("state.db-wal", b"sqlite wal bytes", "state.db-wal"),
        ("proposed/pending.yaml", b"id: pending\n", "proposed/pending.yaml"),
    ],
)
def test_import_rejects_non_committable_bundle_paths(
    store: KBStore, tmp_path: Path, member_name: str, payload: bytes, expected: str
) -> None:
    bundle_path = tmp_path / "non-committable.tar.gz"
    _write_malicious_bundle(bundle_path, member_name, payload)

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any(expected in issue for issue in diff.issues), diff.issues

    audit_path = store.kb_dir / "audit.log.jsonl"
    audit_path.write_text('{"event":"canary"}\n')
    before_audit = audit_path.read_text()
    with pytest.raises(RuntimeError, match="forbidden path"):
        bundle.import_apply(store.kb_dir, bundle_path, on_conflict="overwrite")
    assert audit_path.read_text() == before_audit


@pytest.mark.parametrize(
    ("safety", "expected"),
    [
        (
            {"has_proposed": True, "has_state_db": False, "has_audit_log": False},
            "has_proposed",
        ),
        (
            {"has_proposed": False, "has_state_db": True, "has_audit_log": False},
            "has_state_db",
        ),
        (
            {"has_proposed": False, "has_state_db": False, "has_audit_log": True},
            "has_audit_log",
        ),
    ],
)
def test_import_rejects_manifest_safety_flags(
    store: KBStore, tmp_path: Path, safety: dict[str, bool], expected: str
) -> None:
    payload = b"version: 1\n"
    bundle_path = tmp_path / "unsafe-flag.tar.gz"
    _write_malicious_bundle(bundle_path, "config.yaml", payload, safety=safety)

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any(expected in issue for issue in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match=expected):
        bundle.import_apply(store.kb_dir, bundle_path, on_conflict="overwrite")


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


def test_import_rejects_member_with_mismatched_sha256(store: KBStore, tmp_path: Path) -> None:
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


def test_import_rejects_source_content_mismatch(store: KBStore, tmp_path: Path) -> None:
    """`_validate_content` skips `sources/*/content` files, so the manifest
    sha256 is the only thing that can detect substituted source bytes."""
    legitimate = b"original source bytes"
    tampered = b"attacker-controlled bytes"
    bundle_path = tmp_path / "tampered.tar.gz"
    _write_hash_mismatched_bundle(bundle_path, "sources/deadbeef/content", legitimate, tampered)

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("hash mismatch" in i for i in diff.issues)

    with pytest.raises(RuntimeError, match="hash mismatch"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "sources" / "deadbeef" / "content").exists()


def test_import_apply_raises_on_write_time_hash_mismatch(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #74 review feedback: if a tampered bundle slips past
    import_check (e.g. TOCTOU between the check and the apply re-open),
    import_apply must raise rather than silently dropping the member and
    still logging a clean `bundle.import` audit event with the legitimate
    bundle_id — that is exactly the audit-truthfulness anti-pattern #74
    was filed to fix."""
    legitimate = b"text: original\n"
    tampered = b"text: TAMPERED\n"
    bundle_path = tmp_path / "tampered.tar.gz"
    _write_hash_mismatched_bundle(
        bundle_path,
        "claims/c1.yaml",
        legitimate,
        tampered,
    )

    # Force the pre-write check to look clean so the apply path reaches
    # the write-time re-verify branch.
    monkeypatch.setattr(
        bundle,
        "import_check",
        lambda *_a, **_k: bundle.ImportCheckResult(
            ok=True,
            bundle_id="deadbeef",
            new_files=["claims/c1.yaml"],
            conflicts=[],
            identical=[],
            issues=[],
        ),
    )

    with pytest.raises(RuntimeError, match="hash mismatch at write time"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "claims" / "c1.yaml").exists()
    audit_path = store.kb_dir / "audit.log.jsonl"
    audit_text = audit_path.read_text() if audit_path.exists() else ""
    assert "bundle.import" not in audit_text, audit_text


def test_import_treats_missing_manifest_sha256_as_mismatch(store: KBStore, tmp_path: Path) -> None:
    """Regression for #74 review feedback: a hand-crafted manifest entry
    without a `sha256` field used to raise a bare KeyError in import_check
    and import_apply. Treat the missing field as a hash mismatch so the
    bundle is rejected with a clean issue / RuntimeError."""
    payload = b"text: any content\n"
    bundle_path = tmp_path / "no-sha.tar.gz"
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": "deadbeef",
        "files": [{"path": "claims/c1.yaml", "size": len(payload)}],
        "counts": {},
        "safety": {
            "has_proposed": False,
            "has_state_db": False,
            "has_audit_log": False,
        },
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        info = tarfile.TarInfo("claims/c1.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("hash mismatch" in i for i in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match="hash mismatch"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "claims" / "c1.yaml").exists()


def test_import_rejects_uncited_claim(store: KBStore, tmp_path: Path) -> None:
    """Regression for #81: a bundle whose claim YAML has evidence: []
    must be rejected by import_check / import_apply because the Claim
    model now enforces the 'must cite at least one' invariant. Before
    this fix, _validate_content deferred to pydantic, which accepted
    evidence=[] and silently landed an uncited claim."""
    uncited_yaml = (
        b"id: bundle-uncited\n"
        b'text: "shipped via bundle, no citations"\n'
        b"type: fact\n"
        b"status: stable\n"
        b"confidence: 1.0\n"
        b"evidence: []\n"
    )
    bundle_path = tmp_path / "uncited.tar.gz"
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": "deadbeef",
        "files": [{
            "path": "claims/bundle-uncited.yaml",
            "size": len(uncited_yaml),
            "sha256": hashlib.sha256(uncited_yaml).hexdigest(),
        }],
        "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False,
                   "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        info = tarfile.TarInfo("claims/bundle-uncited.yaml")
        info.size = len(uncited_yaml)
        tar.addfile(info, io.BytesIO(uncited_yaml))
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("schema validation failed" in i for i in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match="schema validation failed"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "claims" / "bundle-uncited.yaml").exists()


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


# --- graph integrity on the bundle path ----------------------------------
#
# `import_apply` writes member bytes directly to disk and never goes
# through `put_relation` / `put_page`, so the storage-layer validators
# can't reach this surface. `_check_graph_integrity` is the bundle's
# equivalent gate: every Relation / Page reference must resolve against
# the post-merge id set (destination KB plus incoming bundle).


def _write_multi_member_bundle(
    bundle_path: Path, members: dict[str, bytes]
) -> None:
    """Build a manifest-consistent bundle with the given member bodies."""
    files = [
        {
            "path": name,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        for name, body in members.items()
    ]
    bundle_id = hashlib.sha256(
        b"".join(sorted(f["sha256"].encode() for f in files))
    ).hexdigest()
    manifest = {
        "spec": bundle.SPEC_VERSION,
        "bundle_id": bundle_id,
        "files": files,
        "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False,
                   "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))


# --- source content-addressing on the bundle path ------------------------
#
# A Source's id is the sha256 of its content (README: "content-addressed
# by sha256"). import_apply writes sources/<sha>/{meta.yaml,content}
# straight from the tarball, so a manifest-consistent bundle could ship a
# source whose content does NOT hash to its claimed id. The per-file sha256
# gate (#74) only proves bytes match the manifest, not the content-address.


def _write_source_bundle(
    bundle_path: Path, *, dir_id: str, content: bytes,
    meta_id: str | None = None, meta_hash: str | None = "__dir__",
) -> None:
    """Build a one-source bundle with full control over the (lying) ids.

    `meta_id` / `meta_hash` default to the directory id so an honest
    bundle is the default; pass explicit values to model an attack.
    The manifest always records each member's real sha256, so the #74
    per-file integrity check passes and the content-address check is the
    only thing that can catch the lie.
    """
    import hashlib as _hashlib

    import yaml as _yaml

    if meta_id is None:
        meta_id = dir_id
    if meta_hash == "__dir__":
        meta_hash = dir_id
    meta = {
        "id": meta_id, "type": "file", "locator": "x.txt", "title": "t",
        "hash": meta_hash, "immutable": True, "scope": "project",
        "byte_size": len(content), "media_type": "text/plain",
        "created_at": "2026-05-27T00:00:00+00:00", "metadata": {}, "tags": [],
    }
    meta_bytes = _yaml.safe_dump(meta, sort_keys=False).encode()
    members = {
        f"sources/{dir_id}/meta.yaml": meta_bytes,
        f"sources/{dir_id}/content": content,
    }
    files = [
        {"path": p, "size": len(b), "sha256": _hashlib.sha256(b).hexdigest()}
        for p, b in members.items()
    ]
    manifest = {
        "spec": bundle.SPEC_VERSION, "bundle_id": "deadbeef",
        "files": files, "counts": {},
        "safety": {"has_proposed": False, "has_state_db": False,
                   "has_audit_log": False},
    }
    with tarfile.open(bundle_path, "w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        mf_bytes = json.dumps(manifest).encode()
        mf_info = tarfile.TarInfo(bundle.MANIFEST_NAME)
        mf_info.size = len(mf_bytes)
        tar.addfile(mf_info, io.BytesIO(mf_bytes))


def _relation_yaml(rid: str, source: str, target: str, evidence: list[str]) -> bytes:
    import yaml as _yaml
    return _yaml.safe_dump({
        "id": rid, "source": source, "relation": "uses",
        "target": target, "confidence": 0.7, "evidence": evidence,
        "created_at": "2026-05-27T00:00:00+00:00",
        "updated_at": "2026-05-27T00:00:00+00:00",
    }, sort_keys=False).encode()


def _page_md(pid: str, entities: list[str], sources: list[str]) -> bytes:
    import yaml as _yaml
    meta = {
        "id": pid, "title": pid, "type": "concept", "status": "draft",
        "claims": [], "entities": entities, "sources": sources, "tags": [],
        "created_at": "2026-05-27T00:00:00+00:00",
        "updated_at": "2026-05-27T00:00:00+00:00",
    }
    return f"---\n{_yaml.safe_dump(meta, sort_keys=False)}---\nbody".encode()


def _claim_yaml(
    cid: str,
    evidence: list[str],
    *,
    entities: list[str] | None = None,
    supersedes: list[str] | None = None,
    contradicts: list[str] | None = None,
    superseded_by: str | None = None,
) -> bytes:
    import yaml as _yaml
    return _yaml.safe_dump({
        "id": cid, "text": "t", "type": "observation", "status": "working",
        "confidence": 0.7, "evidence": evidence,
        "entities": entities or [], "supersedes": supersedes or [],
        "superseded_by": superseded_by, "contradicts": contradicts or [],
        "scope": "project", "tags": [],
        "created_at": "2026-05-27T00:00:00+00:00",
        "updated_at": "2026-05-27T00:00:00+00:00",
        "last_confirmed_at": None, "approved_by": None,
    }, sort_keys=False).encode()


def test_import_check_rejects_relation_with_dangling_endpoints(
    store: KBStore, tmp_path: Path
) -> None:
    """A bundle that ships a relation whose source / target are absent
    from both the bundle and the destination is rejected — the bundle
    integrity story extends from per-file sha256 (#74) to referential
    integrity of the graph it carries."""
    bundle_path = tmp_path / "evil-rel.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "relations/r-dangling.yaml": _relation_yaml(
            "r-dangling", "ghost-source", "ghost-target", [],
        ),
    })

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("dangling reference" in i and "source" in i for i in diff.issues)
    assert any("dangling reference" in i and "target" in i for i in diff.issues)


def test_import_apply_refuses_dangling_relation_endpoints(
    store: KBStore, tmp_path: Path
) -> None:
    bundle_path = tmp_path / "evil-rel.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "relations/r-dangling.yaml": _relation_yaml(
            "r-dangling", "ghost", "ghost2", [],
        ),
    })

    with pytest.raises(RuntimeError, match="dangling reference"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "relations" / "r-dangling.yaml").exists()


def test_import_check_rejects_page_with_dangling_refs(
    store: KBStore, tmp_path: Path
) -> None:
    bundle_path = tmp_path / "evil-page.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "pages/evil-page.md": _page_md(
            "evil-page",
            entities=["ghost-entity"],
            sources=["ghost-source"],
        ),
    })

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("dangling reference" in i and "page entity" in i for i in diff.issues)
    assert any("dangling reference" in i and "page source" in i for i in diff.issues)

    with pytest.raises(RuntimeError, match="dangling reference"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "pages" / "evil-page.md").exists()


def test_import_check_rejects_claim_with_dangling_graph_refs(
    store: KBStore, tmp_path: Path
) -> None:
    """A bundle claim whose `entities` / `contradicts` (and siblings) point
    at artifacts absent from both bundle and destination is rejected — the
    Claim counterpart of the relation / page graph-integrity checks. Without
    it, import_apply writes the claim YAML straight to disk carrying links
    that fsck only flags after the fact (#196)."""
    src = store.put_source(b"e")  # destination source the claim can cite
    bundle_path = tmp_path / "evil-claim.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "claims/c-ent.yaml": _claim_yaml(
            "c-ent", [src.id], entities=["ghost-entity"],
        ),
        "claims/c-graph.yaml": _claim_yaml(
            "c-graph", [src.id], contradicts=["ghost-claim"],
        ),
    })

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("dangling reference" in i and "claim entity" in i
               for i in diff.issues), diff.issues
    assert any("dangling reference" in i and "claim graph ref" in i
               for i in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match="dangling reference"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "claims" / "c-ent.yaml").exists()


def test_import_check_accepts_claim_with_resolvable_graph_refs(
    store: KBStore, tmp_path: Path
) -> None:
    """The honest round-trip guard: a claim whose graph refs all resolve
    (here, to an entity shipped in the same bundle) imports cleanly."""
    src = store.put_source(b"e")
    import yaml as _yaml
    ent_yaml = _yaml.safe_dump({
        "id": "ent-x", "name": "X", "type": "project",
        "aliases": [], "description": None, "page": None,
        "created_at": "2026-05-27T00:00:00+00:00",
        "updated_at": "2026-05-27T00:00:00+00:00",
    }, sort_keys=False).encode()
    bundle_path = tmp_path / "good-claim.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "entities/ent-x.yaml": ent_yaml,
        "claims/c-ok.yaml": _claim_yaml("c-ok", [src.id], entities=["ent-x"]),
    })

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert diff.ok, diff.issues
    bundle.import_apply(store.kb_dir, bundle_path)
    assert (store.kb_dir / "claims" / "c-ok.yaml").exists()


def test_import_check_accepts_self_contained_bundle(
    store: KBStore, tmp_path: Path
) -> None:
    """A bundle that ships an entity alongside the relation that points
    at it imports cleanly. The post-merge id set is the union of
    destination + incoming, so a self-contained bundle does not get
    penalised for not having its dependencies already on disk."""
    import yaml as _yaml
    ent_yaml = _yaml.safe_dump({
        "id": "ent-x", "name": "X", "type": "project",
        "aliases": [], "description": None, "page": None,
        "created_at": "2026-05-27T00:00:00+00:00",
        "updated_at": "2026-05-27T00:00:00+00:00",
    }, sort_keys=False).encode()
    ent2_yaml = _yaml.safe_dump({
        "id": "ent-y", "name": "Y", "type": "project",
        "aliases": [], "description": None, "page": None,
        "created_at": "2026-05-27T00:00:00+00:00",
        "updated_at": "2026-05-27T00:00:00+00:00",
    }, sort_keys=False).encode()
    bundle_path = tmp_path / "self-contained.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "entities/ent-x.yaml": ent_yaml,
        "entities/ent-y.yaml": ent2_yaml,
        "relations/ent-x--uses--ent-y.yaml": _relation_yaml(
            "ent-x--uses--ent-y", "ent-x", "ent-y", [],
        ),
    })

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert diff.ok, diff.issues
    result = bundle.import_apply(store.kb_dir, bundle_path)
    assert "relations/ent-x--uses--ent-y.yaml" in result["written"]


def test_import_check_resolves_refs_against_destination_kb(
    store: KBStore, tmp_path: Path
) -> None:
    """If the destination already has the entity, a bundle shipping just
    the relation imports cleanly."""
    from vouch.models import Entity, EntityType
    store.put_entity(Entity(id="local-a", name="A", type=EntityType.PROJECT))
    store.put_entity(Entity(id="local-b", name="B", type=EntityType.PROJECT))
    bundle_path = tmp_path / "rel-only.tar.gz"
    _write_multi_member_bundle(bundle_path, {
        "relations/local-a--uses--local-b.yaml": _relation_yaml(
            "local-a--uses--local-b", "local-a", "local-b", [],
        ),
    })

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert diff.ok, diff.issues


def test_import_rejects_source_content_address_mismatch(
    store: KBStore, tmp_path: Path
) -> None:
    """A source whose content does not hash to its claimed id is rejected.
    Without this the KB lands internally inconsistent (verify_source would
    report stored_ok=False) while the import logs a clean bundle.import."""
    dir_id = "a" * 64  # well-formed hex, but not the hash of `content`
    bundle_path = tmp_path / "lying-source.tar.gz"
    _write_source_bundle(bundle_path, dir_id=dir_id, content=b"not-the-hashed-bytes")

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("content-address mismatch" in i for i in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match="content-address mismatch"):
        bundle.import_apply(store.kb_dir, bundle_path)
    assert not (store.kb_dir / "sources" / dir_id / "content").exists()


def test_import_rejects_source_meta_id_mismatch(
    store: KBStore, tmp_path: Path
) -> None:
    """meta.yaml's id must equal its content-address directory."""
    content = b"real content"
    dir_id = hashlib.sha256(content).hexdigest()
    bundle_path = tmp_path / "meta-id-lie.tar.gz"
    # Content honestly hashes to dir_id, but meta claims a different id.
    _write_source_bundle(
        bundle_path, dir_id=dir_id, content=content,
        meta_id="b" * 64, meta_hash="b" * 64,
    )

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not diff.ok
    assert any("source id mismatch" in i for i in diff.issues), diff.issues

    with pytest.raises(RuntimeError, match="source id mismatch"):
        bundle.import_apply(store.kb_dir, bundle_path)


def test_import_accepts_honest_content_addressed_source(
    store: KBStore, tmp_path: Path
) -> None:
    """A source whose content hashes to its id imports cleanly — the new
    check must not reject legitimate bundles produced by `vouch export`."""
    content = b"genuine source bytes"
    dir_id = hashlib.sha256(content).hexdigest()
    bundle_path = tmp_path / "honest.tar.gz"
    _write_source_bundle(bundle_path, dir_id=dir_id, content=content)

    diff = bundle.import_check(store.kb_dir, bundle_path)
    assert not any("content-address" in i or "source id" in i for i in diff.issues), diff.issues
    result = bundle.import_apply(store.kb_dir, bundle_path)
    assert f"sources/{dir_id}/content" in result["written"]
    assert store.read_source_content(dir_id) == content


def test_export_then_import_roundtrip_passes_content_address_check(
    store: KBStore, tmp_path: Path
) -> None:
    """End-to-end: a real bundle from `vouch export` imports without
    tripping the content-address check (guards against false positives)."""
    src = store.put_source(b"round-trip bytes", title="doc")
    store.put_claim(Claim(id="c1", text="alpha", evidence=[src.id]))
    bundle_path = tmp_path / "out.tar.gz"
    bundle.export(store.kb_dir, dest=bundle_path)

    dest = KBStore.init(tmp_path / "dest")
    diff = bundle.import_check(dest.kb_dir, bundle_path)
    assert diff.ok, diff.issues
    bundle.import_apply(dest.kb_dir, bundle_path)
    assert dest.read_source_content(src.id) == b"round-trip bytes"
