"""Portable bundles: export-import for moving a KB between repos.

A bundle is a `.tar.gz` of every committed artifact (claims, pages,
sources, entities, relations, evidence, sessions, decided proposals) plus
a `manifest.json` with:

  - bundle id (sha256 of the sorted artifact-hash list)
  - spec version
  - per-file path + size + sha256
  - object counts
  - safety flags (no proposed/, no state.db, no audit.log)

Import is two-step: `import_check` validates the manifest and produces a
diff (new/conflict/skip per id), `import_apply` performs the merge with
explicit conflict resolution.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import audit
from .models import Claim, Entity, Evidence, Proposal, Relation, Session, Source
from .storage import _deserialize_page, sha256_hex

MANIFEST_NAME = "manifest.json"
SPEC_VERSION = "vouch-bundle-0.1"

EXPORT_SUBDIRS = (
    "claims", "pages", "sources", "entities", "relations",
    "evidence", "sessions", "decided",
)

VALIDATORS: dict[str, Any] = {
    "claims": lambda data: Claim.model_validate(yaml.safe_load(data)),
    "pages": lambda data: _deserialize_page(data.decode()),
    "sources": lambda data: Source.model_validate(yaml.safe_load(data)),
    "entities": lambda data: Entity.model_validate(yaml.safe_load(data)),
    "relations": lambda data: Relation.model_validate(yaml.safe_load(data)),
    "evidence": lambda data: Evidence.model_validate(yaml.safe_load(data)),
    "sessions": lambda data: Session.model_validate(yaml.safe_load(data)),
    "decided": lambda data: Proposal.model_validate(yaml.safe_load(data)),
}


# --- export ---------------------------------------------------------------


def _iter_export_files(kb_dir: Path):
    """Yield (relative path, absolute path) for every committable file."""
    for sub in EXPORT_SUBDIRS:
        root = kb_dir / sub
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                yield p.relative_to(kb_dir), p
    cfg = kb_dir / "config.yaml"
    if cfg.exists():
        yield cfg.relative_to(kb_dir), cfg


def build_manifest(kb_dir: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for rel, abs_path in _iter_export_files(kb_dir):
        data = abs_path.read_bytes()
        files.append({
            # tarfile member names use POSIX `/` on every platform; the
            # manifest path must match so set lookups and the per-subdir
            # counter below work on Windows too.
            "path": rel.as_posix(),
            "size": len(data),
            "sha256": sha256_hex(data),
        })
    # Bundle id is the sha256 of the sorted per-file hashes — same inputs
    # always produce the same id, so duplicate exports are recognisable.
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: f["path"]):
        h.update(f["sha256"].encode())
    return {
        "spec": SPEC_VERSION,
        "bundle_id": h.hexdigest(),
        "files": files,
        "counts": {
            sub: sum(1 for f in files if f["path"].startswith(f"{sub}/"))
            for sub in EXPORT_SUBDIRS
        },
        "safety": {
            "has_proposed": False,
            "has_state_db": False,
            "has_audit_log": False,
        },
    }


def export(
    kb_dir: Path, *, dest: Path, actor: str = "vouch-export",
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    manifest = build_manifest(kb_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for rel, abs_path in _iter_export_files(kb_dir):
            if on_progress is not None:
                on_progress(rel.as_posix())
            tar.add(abs_path, arcname=rel.as_posix())
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    audit.log_event(
        kb_dir, event="bundle.export", actor=actor,
        object_ids=[manifest["bundle_id"]],
        data={"dest": str(dest), "files": len(manifest["files"])},
    )
    return manifest


@dataclass
class ExportCheckResult:
    ok: bool
    bundle_id: str
    files_checked: int
    issues: list[str]


def _unsafe_name_reason(name: str) -> str | None:
    if not name:
        return "empty path in bundle"
    if name.startswith("/"):
        return f"absolute path in bundle: {name!r}"
    if "\x00" in name:
        return f"nul byte in bundle path: {name!r}"
    if ".." in Path(name).parts:
        return f"path traversal in bundle: {name!r}"
    return None


def export_check(bundle_path: Path) -> ExportCheckResult:
    """Verify every file in the bundle matches its manifest hash."""
    issues: list[str] = []
    bundle_id = ""
    files_checked = 0
    with tarfile.open(bundle_path, "r:gz") as tar:
        try:
            mf_member = tar.getmember(MANIFEST_NAME)
        except KeyError:
            return ExportCheckResult(False, "", 0, ["missing manifest.json"])
        manifest = json.loads(tar.extractfile(mf_member).read().decode())  # type: ignore[union-attr]
        bundle_id = manifest.get("bundle_id", "")
        recorded = {f["path"]: f for f in manifest["files"]}
        for path in recorded:
            reason = _unsafe_name_reason(path)
            if reason is not None:
                issues.append(f"unsafe path in manifest: {reason}")
        for member in tar.getmembers():
            if member.name == MANIFEST_NAME:
                continue
            if not member.isfile():
                continue
            reason = _unsafe_name_reason(member.name)
            if reason is not None:
                issues.append(reason)
                continue
            files_checked += 1
            rec = recorded.get(member.name)
            if rec is None:
                issues.append(f"file in bundle but not in manifest: {member.name}")
                continue
            body = tar.extractfile(member).read()  # type: ignore[union-attr]
            actual = sha256_hex(body)
            if actual != rec["sha256"]:
                issues.append(f"hash mismatch: {member.name}")
        for path in recorded:
            try:
                tar.getmember(path)
            except KeyError:
                issues.append(f"manifest lists missing file: {path}")
    return ExportCheckResult(
        ok=not issues, bundle_id=bundle_id,
        files_checked=files_checked, issues=issues,
    )


# --- import ---------------------------------------------------------------


def _safe_member_path(kb_dir: Path, member_name: str) -> Path:
    reason = _unsafe_name_reason(member_name)
    if reason is not None:
        raise RuntimeError(reason)
    kb_root = kb_dir.resolve()
    dest = (kb_root / member_name).resolve()
    try:
        dest.relative_to(kb_root)
    except ValueError as exc:
        raise RuntimeError(f"path traversal in bundle: {member_name!r}") from exc
    return dest


@dataclass
class ImportCheckResult:
    ok: bool
    bundle_id: str
    new_files: list[str]
    conflicts: list[str]  # paths whose content differs from the destination
    identical: list[str]  # paths already present with matching hash
    issues: list[str]


def _validate_content(path: str, data: bytes, issues: list[str]) -> None:
    subdir = path.split("/")[0]
    # Source artifacts have two file kinds:
    #   sources/<sha>/meta.yaml  -- the Source pydantic model (validate)
    #   sources/<sha>/content    -- the raw source bytes (skip validation)
    # The opaque content file isn't a pydantic model, so model_validate
    # on raw bytes raises spuriously.
    if subdir == "sources" and not path.endswith("/meta.yaml"):
        return
    validator = VALIDATORS.get(subdir)
    if validator is None:
        return
    if not any(path.lower().endswith(ext) for ext in (".yaml", ".yml", ".md")):
        return
    try:
        validator(data)
    except Exception as e:
        issues.append(f"schema validation failed: {path}: {e}")


def import_check(kb_dir: Path, bundle_path: Path) -> ImportCheckResult:
    """Diff a bundle against the destination KB without writing anything."""
    new_files: list[str] = []
    conflicts: list[str] = []
    identical: list[str] = []
    issues: list[str] = []
    bundle_id = ""

    with tarfile.open(bundle_path, "r:gz") as tar:
        try:
            mf_member = tar.getmember(MANIFEST_NAME)
        except KeyError:
            return ImportCheckResult(
                False, "", [], [], [], ["bundle missing manifest.json"]
            )
        manifest = json.loads(tar.extractfile(mf_member).read().decode())  # type: ignore[union-attr]
        bundle_id = manifest.get("bundle_id", "")
        recorded = {f["path"]: f for f in manifest["files"]}
        manifest_paths = set(recorded)
        for f in manifest["files"]:
            try:
                dest = _safe_member_path(kb_dir, f["path"])
            except RuntimeError as exc:
                issues.append(str(exc))
                continue
            if not dest.exists():
                new_files.append(f["path"])
            elif sha256_hex(dest.read_bytes()) == f.get("sha256"):
                identical.append(f["path"])
            else:
                conflicts.append(f["path"])
        for member in tar.getmembers():
            if member.name == MANIFEST_NAME or not member.isfile():
                continue
            if member.name not in manifest_paths:
                continue
            body = tar.extractfile(member).read()  # type: ignore[union-attr]
            # Manifest integrity: without this, a tampered tar member with an
            # unchanged manifest.json would pass import_check and land in the
            # KB via import_apply — defeating the per-file sha256 guarantee
            # that export_check already enforces. `.get("sha256")` so a
            # hand-crafted manifest entry missing the field is reported as a
            # mismatch rather than raising a bare KeyError on import.
            if sha256_hex(body) != recorded[member.name].get("sha256"):
                issues.append(f"hash mismatch: {member.name}")
                continue
            _validate_content(member.name, body, issues)

    return ImportCheckResult(
        ok=not issues, bundle_id=bundle_id,
        new_files=new_files, conflicts=conflicts,
        identical=identical, issues=issues,
    )


def import_apply(
    kb_dir: Path,
    bundle_path: Path,
    *,
    on_conflict: str = "skip",
    actor: str = "vouch-import",
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply a bundle. `on_conflict` ∈ {"skip", "overwrite", "fail"}.

    The default is `skip` so an import is never destructive without an
    explicit choice.
    """
    if on_conflict not in {"skip", "overwrite", "fail"}:
        raise ValueError(f"on_conflict must be skip|overwrite|fail, got {on_conflict}")
    check = import_check(kb_dir, bundle_path)
    if check.issues:
        raise RuntimeError(f"refusing to import: {check.issues[0]}")
    if on_conflict == "fail" and check.conflicts:
        raise RuntimeError(f"refusing to import: {len(check.conflicts)} conflicts")
    written: list[str] = []
    skipped: list[str] = []
    with tarfile.open(bundle_path, "r:gz") as tar:
        manifest = json.loads(
            tar.extractfile(tar.getmember(MANIFEST_NAME)).read().decode()  # type: ignore[union-attr]
        )
        recorded = {f["path"]: f for f in manifest["files"]}
        for member in tar.getmembers():
            if member.name == MANIFEST_NAME or not member.isfile():
                continue
            if member.name not in recorded:
                continue
            dest = _safe_member_path(kb_dir, member.name)
            expected_sha = recorded[member.name].get("sha256")
            if (
                dest.exists()
                and on_conflict == "skip"
                and sha256_hex(dest.read_bytes()) != expected_sha
            ):
                skipped.append(member.name)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            body = tar.extractfile(member).read()  # type: ignore[union-attr]
            # Re-verify the manifest sha256 at write time as defence in
            # depth against a TOCTOU between import_check (which already
            # ran above) and this re-open of the tarball. The only way to
            # reach here is mid-import tampering — raise rather than skip
            # so the audit log doesn't record a `bundle.import` event for
            # an import that silently dropped a member. This is exactly
            # the audit-truthfulness anti-pattern #74 was about.
            if sha256_hex(body) != expected_sha:
                raise RuntimeError(
                    f"refusing to import: hash mismatch at write time: "
                    f"{member.name}"
                )
            val_issues: list[str] = []
            _validate_content(member.name, body, val_issues)
            if val_issues:
                skipped.append(member.name)
                continue
            dest.write_bytes(body)
            written.append(member.name)
            if on_progress is not None:
                on_progress(member.name)
    result = {
        "bundle_id": check.bundle_id,
        "written": written,
        "skipped_conflicts": skipped,
        "identical": check.identical,
        "on_conflict": on_conflict,
    }
    audit.log_event(
        kb_dir, event="bundle.import", actor=actor,
        object_ids=[check.bundle_id],
        data={
            "written": len(written),
            "skipped": len(skipped),
            "on_conflict": on_conflict,
        },
    )
    return result


def _yaml_dump(obj: Any) -> str:  # pragma: no cover — kept for symmetry
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
