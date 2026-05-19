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
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import audit
from .models import (
    Claim,
    Entity,
    Evidence,
    Page,
    Proposal,
    Relation,
    Session,
    Source,
)
from .storage import sha256_hex

MANIFEST_NAME = "manifest.json"
SPEC_VERSION = "vouch-bundle-0.1"

EXPORT_SUBDIRS = (
    "claims", "pages", "sources", "entities", "relations",
    "evidence", "sessions", "decided",
)

_VALIDATORS: dict[str, Any] = {}


def _init_validators():
    if _VALIDATORS:
        return
    _VALIDATORS.update({
        "claims": lambda data: Claim.model_validate(yaml.safe_load(data)),
        "pages": lambda data: _deserialize_page(data.decode()),
        "sources": lambda data: Source.model_validate(yaml.safe_load(data)),
        "entities": lambda data: Entity.model_validate(yaml.safe_load(data)),
        "relations": lambda data: Relation.model_validate(yaml.safe_load(data)),
        "evidence": lambda data: Evidence.model_validate(yaml.safe_load(data)),
        "sessions": lambda data: Session.model_validate(yaml.safe_load(data)),
        "decided": lambda data: Proposal.model_validate(yaml.safe_load(data)),
    })


_PAGE_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _deserialize_page(data: str) -> Page:
    m = _PAGE_FRONTMATTER_RE.match(data)
    if not m:
        raise ValueError("missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    return Page(**{**meta, "body": body})


_VALID_EXTENSIONS = {".yaml", ".yml", ".md"}


def _validate_bundle_content(
    member_name: str, body: bytes,
) -> str | None:
    if not any(member_name.endswith(ext) for ext in _VALID_EXTENSIONS):
        return None
    subdir = member_name.split("/")[0]
    validator = _VALIDATORS.get(subdir)
    if validator is None:
        return None
    try:
        validator(body)
    except Exception as exc:
        return f"schema validation failed: {member_name}: {exc}"
    return None


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
            "path": str(rel),
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


def export(kb_dir: Path, *, dest: Path, actor: str = "vouch-export") -> dict[str, Any]:
    manifest = build_manifest(kb_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for rel, abs_path in _iter_export_files(kb_dir):
            tar.add(abs_path, arcname=str(rel))
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
        for member in tar.getmembers():
            if member.name == MANIFEST_NAME:
                continue
            if not member.isfile():
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


@dataclass
class ImportCheckResult:
    ok: bool
    bundle_id: str
    new_files: list[str]
    conflicts: list[str]  # paths whose content differs from the destination
    identical: list[str]  # paths already present with matching hash
    issues: list[str]


def import_check(kb_dir: Path, bundle_path: Path) -> ImportCheckResult:
    """Diff a bundle against the destination KB without writing anything."""
    _init_validators()
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
        for f in manifest["files"]:
            dest = kb_dir / f["path"]
            if not dest.exists():
                new_files.append(f["path"])
                continue
            if sha256_hex(dest.read_bytes()) == f["sha256"]:
                identical.append(f["path"])
            else:
                conflicts.append(f["path"])
        recorded_map = {f["path"]: f for f in manifest["files"]}
        for member in tar.getmembers():
            if member.name == MANIFEST_NAME or not member.isfile():
                continue
            if member.name not in recorded_map:
                continue
            body = tar.extractfile(member).read()  # type: ignore[union-attr]
            err = _validate_bundle_content(member.name, body)
            if err is not None:
                issues.append(err)

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
) -> dict[str, Any]:
    """Apply a bundle. `on_conflict` ∈ {"skip", "overwrite", "fail"}.

    The default is `skip` so an import is never destructive without an
    explicit choice.
    """
    _init_validators()
    if on_conflict not in {"skip", "overwrite", "fail"}:
        raise ValueError(f"on_conflict must be skip|overwrite|fail, got {on_conflict}")
    check = import_check(kb_dir, bundle_path)
    if on_conflict == "fail" and check.conflicts:
        raise RuntimeError(f"refusing to import: {len(check.conflicts)} conflicts")
    written: list[str] = []
    skipped_conflicts: list[str] = []
    skipped_schema: list[str] = []
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
            dest = kb_dir / member.name
            if (
                dest.exists()
                and on_conflict == "skip"
                and sha256_hex(dest.read_bytes())
                != recorded[member.name]["sha256"]
            ):
                skipped_conflicts.append(member.name)
                continue
            body = tar.extractfile(member).read()  # type: ignore[union-attr]
            err = _validate_bundle_content(member.name, body)
            if err is not None:
                skipped_schema.append(member.name)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            written.append(member.name)
    result = {
        "bundle_id": check.bundle_id,
        "written": written,
        "skipped_conflicts": skipped_conflicts,
        "skipped_schema": skipped_schema,
        "identical": check.identical,
        "on_conflict": on_conflict,
    }
    audit.log_event(
        kb_dir, event="bundle.import", actor=actor,
        object_ids=[check.bundle_id],
        data={
            "written": len(written),
            "skipped_conflicts": len(skipped_conflicts),
            "skipped_schema": len(skipped_schema),
            "on_conflict": on_conflict,
        },
    )
    return result


def _yaml_dump(obj: Any) -> str:  # pragma: no cover — kept for symmetry
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
