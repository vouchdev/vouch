"""Deterministic sync for reconciling another vouch KB or bundle.

The sync surface is deliberately conservative: it imports files that are
absent locally, reports identical files, and never overwrites divergent
reviewed knowledge. Conflicts can fail the operation, be skipped, or be
captured in a local conflict report under ``proposed/sync-reports/``.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import audit, bundle
from .storage import sha256_hex

_SYNC_EXCLUDED_PATHS = {"config.yaml"}


@dataclass
class IncomingFile:
    path: str
    size: int
    sha256: str


@dataclass
class SyncConflict:
    path: str
    kind: str
    artifact_id: str | None
    reason: str
    local_sha256: str
    incoming_sha256: str


@dataclass
class SyncCheckResult:
    ok: bool
    source_type: str
    source_id: str
    source: str
    new_files: list[str]
    identical: list[str]
    conflicts: list[SyncConflict]
    semantic_conflicts: list[SyncConflict]
    decided_conflicts: list[SyncConflict]
    issues: list[str]


@dataclass
class _SyncSource:
    source_type: str
    source_id: str
    display: str
    files: dict[str, IncomingFile]
    root: Path | None = None
    bundle_path: Path | None = None


def _artifact_kind(path: str) -> tuple[str, str | None]:
    if path == "config.yaml":
        return "config", None
    parts = path.split("/")
    top = parts[0]
    if top == "sources" and len(parts) >= 3:
        return "source", parts[1]
    if len(parts) < 2:
        return top, None
    artifact_id = Path(parts[-1]).stem
    singular = {
        "claims": "claim",
        "pages": "page",
        "entities": "entity",
        "relations": "relation",
        "evidence": "evidence",
        "sessions": "session",
        "decided": "decided-proposal",
    }.get(top, top)
    return singular, artifact_id


def _conflict(path: str, local_sha: str, incoming_sha: str) -> SyncConflict:
    kind, artifact_id = _artifact_kind(path)
    if kind == "config":
        reason = "local config differs from incoming config"
    elif kind == "decided-proposal":
        reason = f"decided proposal {artifact_id} differs"
    elif artifact_id is not None:
        reason = f"{kind} {artifact_id} exists with different content"
    else:
        reason = f"{kind} file exists with different content"
    return SyncConflict(
        path=path,
        kind=kind,
        artifact_id=artifact_id,
        reason=reason,
        local_sha256=local_sha,
        incoming_sha256=incoming_sha,
    )


def _source_id(files: dict[str, IncomingFile]) -> str:
    h = sha256_hex(
        "\n".join(
            f"{path}\0{file.sha256}" for path, file in sorted(files.items())
        ).encode()
    )
    return h


def _syncable_files(files: dict[str, IncomingFile]) -> dict[str, IncomingFile]:
    return {
        path: file
        for path, file in files.items()
        if path not in _SYNC_EXCLUDED_PATHS
    }


def _resolve_kb_dir(source_path: Path) -> Path:
    if (source_path / ".vouch").is_dir():
        return source_path / ".vouch"
    if source_path.name == ".vouch" and source_path.is_dir():
        return source_path
    raise RuntimeError(f"sync source is not a vouch KB or bundle: {source_path}")


def _load_directory_source(source_path: Path) -> _SyncSource:
    kb_dir = _resolve_kb_dir(source_path)
    files: dict[str, IncomingFile] = {}
    for rel, abs_path in bundle._iter_export_files(kb_dir):
        path = rel.as_posix()
        data = abs_path.read_bytes()
        files[path] = IncomingFile(path=path, size=len(data), sha256=sha256_hex(data))
    files = _syncable_files(files)
    return _SyncSource(
        source_type="kb",
        source_id=_source_id(files),
        display=str(source_path),
        files=files,
        root=kb_dir,
    )


def _load_bundle_source(source_path: Path) -> _SyncSource:
    with tarfile.open(source_path, "r:gz") as tar:
        try:
            member = tar.getmember(bundle.MANIFEST_NAME)
        except KeyError as exc:
            raise RuntimeError("bundle missing manifest.json") from exc
        manifest = json.loads(tar.extractfile(member).read().decode())  # type: ignore[union-attr]
    files = {
        f["path"]: IncomingFile(
            path=f["path"],
            size=int(f.get("size", 0)),
            sha256=str(f.get("sha256", "")),
        )
        for f in manifest.get("files", [])
    }
    files = _syncable_files(files)
    return _SyncSource(
        source_type="bundle",
        source_id=_source_id(files),
        display=str(source_path),
        files=files,
        bundle_path=source_path,
    )


def _load_source(source_path: Path) -> _SyncSource:
    source_path = source_path.resolve()
    if source_path.is_dir():
        return _load_directory_source(source_path)
    if source_path.is_file():
        return _load_bundle_source(source_path)
    raise RuntimeError(f"sync source does not exist: {source_path}")


def _read_source_file(src: _SyncSource, path: str) -> bytes:
    if src.root is not None:
        return (src.root / path).read_bytes()
    if src.bundle_path is None:
        raise RuntimeError("sync source has no readable backing store")
    with tarfile.open(src.bundle_path, "r:gz") as tar:
        return tar.extractfile(tar.getmember(path)).read()  # type: ignore[union-attr]


def _validation_issues_for_source(
    src: _SyncSource, kb_dir: Path | None = None
) -> list[str]:
    issues: list[str] = []
    if src.bundle_path is not None:
        check = bundle.export_check(src.bundle_path)
        issues.extend(check.issues)
    incoming_bodies: dict[str, bytes] = {}
    for path, incoming in sorted(src.files.items()):
        reason = bundle._unsafe_name_reason(path)
        if reason is not None:
            issues.append(reason)
            continue
        try:
            data = _read_source_file(src, path)
        except Exception as exc:
            issues.append(f"cannot read sync source member {path}: {exc}")
            continue
        if sha256_hex(data) != incoming.sha256:
            issues.append(f"hash mismatch: {path}")
            continue
        bundle._validate_content(path, data, issues)
        bundle._check_source_content_address(path, data, issues)
        incoming_bodies[path] = data
    # Graph-integrity pass mirrors the bundle import path so sync
    # doesn't accept Relations / Pages whose endpoints / references
    # neither exist locally nor arrive in the same sync.
    if kb_dir is not None:
        bundle._check_graph_integrity(kb_dir, incoming_bodies, issues)
    return issues


def sync_check(kb_dir: Path, source_path: Path) -> SyncCheckResult:
    """Compare another KB or bundle with ``kb_dir`` without writing."""
    return _sync_check_with_src(kb_dir, _load_source(source_path))


def _sync_check_with_src(kb_dir: Path, src: _SyncSource) -> SyncCheckResult:
    """Core sync check logic operating on an already-loaded _SyncSource.

    Separated from the public `sync_check` so `sync_apply` can pass its
    already-loaded source directly without reloading, closing the TOCTOU
    window described in #217.
    """
    issues = _validation_issues_for_source(src, kb_dir=kb_dir)
    new_files: list[str] = []
    identical: list[str] = []
    conflicts: list[SyncConflict] = []

    for path, incoming in sorted(src.files.items()):
        try:
            dest = bundle._safe_member_path(kb_dir, path)
        except RuntimeError as exc:
            issues.append(str(exc))
            continue
        if not dest.exists():
            new_files.append(path)
            continue
        local_sha = sha256_hex(dest.read_bytes())
        if local_sha == incoming.sha256:
            identical.append(path)
        else:
            conflicts.append(_conflict(path, local_sha, incoming.sha256))

    semantic_conflicts = [
        c for c in conflicts
        if c.kind in {"claim", "page", "entity", "relation", "evidence", "session"}
    ]
    decided_conflicts = [c for c in conflicts if c.kind == "decided-proposal"]
    return SyncCheckResult(
        ok=not issues,
        source_type=src.source_type,
        source_id=src.source_id,
        source=src.display,
        new_files=new_files,
        identical=identical,
        conflicts=conflicts,
        semantic_conflicts=semantic_conflicts,
        decided_conflicts=decided_conflicts,
        issues=issues,
    )


def _write_conflict_report(
    kb_dir: Path,
    check: SyncCheckResult,
    *,
    on_conflict: str,
) -> str:
    report_dir = kb_dir / "proposed" / "sync-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{check.source_id}.json"
    report = asdict(check)
    report["on_conflict"] = on_conflict
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return str(report_path.relative_to(kb_dir))


def sync_apply(
    kb_dir: Path,
    source_path: Path,
    *,
    on_conflict: str = "fail",
    actor: str = "vouch-sync",
) -> dict[str, Any]:
    """Apply non-conflicting incoming files from another KB or bundle.

    ``on_conflict`` may be:
    - ``fail``: abort if any incoming path conflicts.
    - ``skip``: import new files and leave conflicts untouched.
    - ``propose``: import new files and write a local sync conflict report.
    """
    if on_conflict not in {"fail", "skip", "propose"}:
        raise ValueError(f"on_conflict must be fail|skip|propose, got {on_conflict}")

    src = _load_source(source_path)
    check = _sync_check_with_src(kb_dir, src)
    if check.issues:
        raise RuntimeError(f"refusing to sync: {check.issues[0]}")
    if on_conflict == "fail" and check.conflicts:
        raise RuntimeError(f"refusing to sync: {len(check.conflicts)} conflicts")

    written: list[str] = []
    skipped_conflicts: list[str] = []
    for path, incoming in sorted(src.files.items()):
        dest = bundle._safe_member_path(kb_dir, path)
        if dest.exists():
            local_sha = sha256_hex(dest.read_bytes())
            if local_sha == incoming.sha256:
                continue
            if on_conflict == "fail":
                raise RuntimeError(f"refusing to sync conflicting path: {path}")
            skipped_conflicts.append(path)
            continue

        data = _read_source_file(src, path)
        if sha256_hex(data) != incoming.sha256:
            raise RuntimeError(f"refusing to sync: hash mismatch at write time: {path}")
        val_issues: list[str] = []
        bundle._validate_content(path, data, val_issues)
        if val_issues:
            raise RuntimeError(f"refusing to sync: {val_issues[0]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written.append(path)

    report_path = None
    if on_conflict == "propose" and check.conflicts:
        report_path = _write_conflict_report(
            kb_dir, check, on_conflict=on_conflict,
        )

    result = {
        "source_type": check.source_type,
        "source_id": check.source_id,
        "written": written,
        "skipped_conflicts": skipped_conflicts,
        "identical": check.identical,
        "conflicts": [asdict(c) for c in check.conflicts],
        "conflict_report": report_path,
        "on_conflict": on_conflict,
    }
    audit.log_event(
        kb_dir,
        event="sync.apply",
        actor=actor,
        object_ids=[check.source_id],
        data={
            "source_type": check.source_type,
            "written": len(written),
            "skipped_conflicts": len(skipped_conflicts),
            "on_conflict": on_conflict,
            "conflict_report": report_path,
        },
    )
    return result
