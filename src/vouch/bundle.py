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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import audit
from .models import Claim, Entity, Evidence, Proposal, Relation, Session, Source
from .storage import KBStore, _deserialize_page, sha256_hex

MANIFEST_NAME = "manifest.json"
SPEC_VERSION = "vouch-bundle-0.1"

EXPORT_SUBDIRS = (
    "claims",
    "pages",
    "sources",
    "entities",
    "relations",
    "evidence",
    "sessions",
    "decided",
)

IMPORT_ROOT_FILES = {"config.yaml"}
FORBIDDEN_SAFETY_FLAGS = {
    "has_proposed": "proposed/",
    "has_state_db": "state.db",
    "has_audit_log": "audit.log.jsonl",
}

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
        files.append(
            {
                # tarfile member names use POSIX `/` on every platform; the
                # manifest path must match so set lookups and the per-subdir
                # counter below work on Windows too.
                "path": rel.as_posix(),
                "size": len(data),
                "sha256": sha256_hex(data),
            }
        )
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
            sub: sum(1 for f in files if f["path"].startswith(f"{sub}/")) for sub in EXPORT_SUBDIRS
        },
        "safety": {
            "has_proposed": False,
            "has_state_db": False,
            "has_audit_log": False,
        },
    }


def fenced_bundle_path(store: KBStore, raw: str) -> Path:
    """A client-supplied bundle/export path, confined to the project root on
    remote surfaces.

    Export writes to the path and import reads from it, so an unfenced path on
    /rpc or /mcp is a remote arbitrary-file clobber (export) or read (import).
    On remote trust the path is resolved and contained to the project root;
    local CLI and stdio are unfenced by design, since a human choosing where to
    write a backup is not a threat.
    """
    from . import trust

    if trust.current().remote:
        return store.resolve_under_root(raw)
    return Path(raw)


def export(kb_dir: Path, *, dest: Path, actor: str = "vouch-export") -> dict[str, Any]:
    manifest = build_manifest(kb_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for rel, abs_path in _iter_export_files(kb_dir):
            tar.add(abs_path, arcname=rel.as_posix())
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    audit.log_event(
        kb_dir,
        event="bundle.export",
        actor=actor,
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


def _import_path_issue(name: str) -> str | None:
    reason = _unsafe_name_reason(name)
    if reason is not None:
        return reason
    if name in IMPORT_ROOT_FILES:
        return None
    if name == "audit.log.jsonl":
        return "forbidden path in bundle: 'audit.log.jsonl'"
    if name == "state.db" or name.startswith("state.db-"):
        return f"forbidden path in bundle: {name!r}"
    if name == "proposed" or name.startswith("proposed/"):
        return f"forbidden path in bundle: {name!r}"
    subdir = name.split("/", 1)[0]
    if subdir not in EXPORT_SUBDIRS:
        return f"path outside importable bundle artifacts: {name!r}"
    return None


def _manifest_safety_issues(manifest: dict[str, Any]) -> list[str]:
    safety = manifest.get("safety") or {}
    if not isinstance(safety, dict):
        return ["manifest safety must be an object"]
    issues: list[str] = []
    for flag, path_desc in FORBIDDEN_SAFETY_FLAGS.items():
        if safety.get(flag) is True:
            issues.append(f"manifest safety flag {flag}=true includes forbidden {path_desc}")
    return issues


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
        ok=not issues,
        bundle_id=bundle_id,
        files_checked=files_checked,
        issues=issues,
    )


# --- import ---------------------------------------------------------------


def _safe_member_path(kb_dir: Path, member_name: str) -> Path:
    reason = _import_path_issue(member_name)
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


def _artifact_id_from_path(path: str) -> tuple[str, str] | None:
    """Return (kind, id) for a manifest path, or None for unrelated files.

    Mirrors `sync._artifact_kind` semantics but only emits the entries the
    referential pass needs. `sources/<sha>/...` collapses to ("source",
    "<sha>") regardless of whether the path is meta.yaml or content.
    """
    parts = path.split("/")
    if len(parts) < 2:
        return None
    top = parts[0]
    if top == "sources" and len(parts) >= 3:
        return "source", parts[1]
    singular = {
        "claims": "claim",
        "pages": "page",
        "entities": "entity",
        "relations": "relation",
        "evidence": "evidence",
    }.get(top)
    if singular is None:
        return None
    return singular, Path(parts[-1]).stem


def _existing_ids(kb_dir: Path) -> dict[str, set[str]]:
    """Snapshot the destination KB's artifact ids per kind.

    Used by the post-merge referential pass: an incoming relation /
    page reference is satisfied if the target id is either already on
    disk OR is being delivered by this same bundle.
    """
    out: dict[str, set[str]] = {
        "claim": set(), "page": set(), "entity": set(),
        "source": set(), "evidence": set(),
    }
    for sub, kind, suffix in (
        ("claims", "claim", ".yaml"),
        ("entities", "entity", ".yaml"),
        ("relations", "relation", ".yaml"),
        ("evidence", "evidence", ".yaml"),
    ):
        d = kb_dir / sub
        if not d.is_dir():
            continue
        for p in d.glob(f"*{suffix}"):
            out[kind].add(p.stem)
    pages_dir = kb_dir / "pages"
    if pages_dir.is_dir():
        for p in pages_dir.glob("*.md"):
            out["page"].add(p.stem)
    sources_dir = kb_dir / "sources"
    if sources_dir.is_dir():
        for sdir in sources_dir.iterdir():
            if (sdir / "meta.yaml").exists():
                out["source"].add(sdir.name)
    return out


def _check_graph_integrity(
    kb_dir: Path,
    incoming: dict[str, bytes],
    issues: list[str],
) -> None:
    """Verify every Relation/Page/Claim in `incoming` resolves its refs.

    Closes the bundle / sync equivalent of `put_relation` + `put_page`
    validation. References are satisfied against the post-merge id set
    (destination KB ids plus incoming bundle ids), so a self-contained
    bundle that ships an entity alongside the relation that points at
    it still imports cleanly. Skips files whose bytes already failed
    schema validation upstream so a malformed file does not produce a
    second cryptic error.
    """
    ids = _existing_ids(kb_dir)
    incoming_meta: list[tuple[str, str, bytes]] = []
    for path, body in incoming.items():
        kind_id = _artifact_id_from_path(path)
        if kind_id is None:
            continue
        kind, aid = kind_id
        # Only artifacts that can satisfy a reference go into `ids`.
        # `relation` is intentionally absent — relations aren't valid
        # Relation endpoints. The incoming relation itself is still
        # appended below so its refs get checked.
        if kind in ids:
            ids[kind].add(aid)
        incoming_meta.append((path, kind, body))
    node_kinds = ("claim", "page", "entity", "source")
    evidence_kinds = ("source", "evidence")
    failed_schema = {
        i.split(": ", 2)[1] for i in issues
        if i.startswith("schema validation failed: ")
    }
    for path, kind, body in incoming_meta:
        if path in failed_schema:
            continue
        try:
            if kind == "relation":
                rel = Relation.model_validate(yaml.safe_load(body))
                if not any(rel.source in ids[k] for k in node_kinds):
                    issues.append(
                        f"dangling reference: {path}: relation source "
                        f"{rel.source!r} not in bundle or destination"
                    )
                if not any(rel.target in ids[k] for k in node_kinds):
                    issues.append(
                        f"dangling reference: {path}: relation target "
                        f"{rel.target!r} not in bundle or destination"
                    )
                for eid in rel.evidence:
                    if not any(eid in ids[k] for k in evidence_kinds):
                        issues.append(
                            f"dangling reference: {path}: relation "
                            f"evidence {eid!r} not in bundle or destination"
                        )
            elif kind == "page":
                page = _deserialize_page(body.decode())
                for cid in page.claims:
                    if cid not in ids["claim"]:
                        issues.append(
                            f"dangling reference: {path}: page claim "
                            f"{cid!r} not in bundle or destination"
                        )
                for eid in page.entities:
                    if eid not in ids["entity"]:
                        issues.append(
                            f"dangling reference: {path}: page entity "
                            f"{eid!r} not in bundle or destination"
                        )
                for sid in page.sources:
                    if sid not in ids["source"]:
                        issues.append(
                            f"dangling reference: {path}: page source "
                            f"{sid!r} not in bundle or destination"
                        )
            elif kind == "claim":
                claim = Claim.model_validate(yaml.safe_load(body))
                for ref in claim.evidence:
                    if not any(ref in ids[k] for k in evidence_kinds):
                        issues.append(
                            f"dangling reference: {path}: claim citation "
                            f"{ref!r} not in bundle or destination"
                        )
                # The Claim's own graph refs mirror the page checks above:
                # entities -> entity ids, supersedes/contradicts/superseded_by
                # -> claim ids. Without this, import_apply writes the claim
                # YAML straight to disk (no put_claim guard) carrying links to
                # artifacts that exist in neither the bundle nor the
                # destination — the dangling_* errors fsck reports after the
                # fact (see storage._validate_claim_refs).
                for eid in claim.entities:
                    if eid not in ids["entity"]:
                        issues.append(
                            f"dangling reference: {path}: claim entity "
                            f"{eid!r} not in bundle or destination"
                        )
                claim_refs = [*claim.supersedes, *claim.contradicts]
                if claim.superseded_by is not None:
                    claim_refs.append(claim.superseded_by)
                for cid in claim_refs:
                    if cid not in ids["claim"]:
                        issues.append(
                            f"dangling reference: {path}: claim graph ref "
                            f"{cid!r} not in bundle or destination"
                        )
        except Exception:
            # Schema validation already ran on `body` in `_validate_content`
            # and recorded any structural issue. Swallow here so a single
            # malformed file doesn't mask the more useful upstream error.
            continue


def _check_source_content_address(path: str, body: bytes, issues: list[str]) -> None:
    """Enforce the Source content-addressing invariant on import.

    A Source's id is the sha256 of its content (README: "content-addressed
    by sha256"; `storage.put_source` derives the id from the bytes, and
    `verify.verify_source` re-checks `sha256(content) == id`). But
    `import_apply` writes `sources/<sha>/{meta.yaml,content}` straight from
    the tarball, so without this check a hand-built bundle can land a
    source whose content does not hash to its claimed id. The per-file
    sha256 gate (#74) only proves the bytes match the manifest, not that
    they match the content-address — so a manifest-consistent bundle could
    substitute the evidence behind a legitimate-looking source id. A claim
    that "cites source X" would then point at bytes that were never hashed
    to X; `verify_source` would report `stored_ok=False` only after the
    import already succeeded with a clean `bundle.import` audit event.
    """
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "sources":
        return
    claimed_id = parts[1]
    leaf = parts[-1]
    if leaf == "content":
        actual = sha256_hex(body)
        if actual != claimed_id:
            issues.append(
                f"source content-address mismatch: {path}: content hashes "
                f"to {actual} but is stored under id {claimed_id}"
            )
    elif leaf == "meta.yaml":
        try:
            meta = yaml.safe_load(body)
        except Exception:
            return  # a parse failure is already surfaced by _validate_content
        if not isinstance(meta, dict):
            return
        mid = meta.get("id")
        if mid is not None and mid != claimed_id:
            issues.append(
                f"source id mismatch: {path}: meta.yaml id {mid!r} does not "
                f"match its content-address directory {claimed_id!r}"
            )
        mhash = meta.get("hash")
        if mhash is not None and mhash != claimed_id:
            issues.append(
                f"source hash mismatch: {path}: meta.yaml hash {mhash!r} does "
                f"not match its content-address directory {claimed_id!r}"
            )


def import_check(kb_dir: Path, bundle_path: Path) -> ImportCheckResult:
    """Diff a bundle against the destination KB without writing anything."""
    new_files: list[str] = []
    conflicts: list[str] = []
    identical: list[str] = []
    issues: list[str] = []
    bundle_id = ""
    incoming: dict[str, bytes] = {}

    with tarfile.open(bundle_path, "r:gz") as tar:
        try:
            mf_member = tar.getmember(MANIFEST_NAME)
        except KeyError:
            return ImportCheckResult(False, "", [], [], [], ["bundle missing manifest.json"])
        manifest = json.loads(tar.extractfile(mf_member).read().decode())  # type: ignore[union-attr]
        bundle_id = manifest.get("bundle_id", "")
        issues.extend(_manifest_safety_issues(manifest))
        recorded = {f["path"]: f for f in manifest["files"]}
        manifest_paths = set(recorded)
        # decided/ holds approved decisions; import writes members straight to
        # disk, so a bundle carrying decided/ would land approved claims/pages
        # without a receiving-side proposal — a write past the review gate.
        # Refuse until gated import exists (roadmap 8.2). Scanned directly from
        # the manifest rather than via a self-reported safety flag, which a
        # hand-crafted bundle could lie about.
        decided_members = sorted(p for p in manifest_paths if p.startswith("decided/"))
        if decided_members:
            issues.append(
                "bundle carries decided/ members that cannot be imported past "
                f"the review gate: {decided_members[0]}"
            )
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
            _check_source_content_address(member.name, body, issues)
            incoming[member.name] = body
        for path in manifest_paths:
            try:
                tar.getmember(path)
            except KeyError:
                issues.append(f"manifest lists missing file: {path}")
    # Graph-integrity pass: the storage layer's put_relation / put_page
    # validators run on direct writes, but import_apply writes member
    # bytes straight to disk, so the only place to enforce referential
    # integrity for a bundle is here. Refs are resolved against the
    # post-merge id set (destination KB plus incoming bundle), so a
    # self-contained bundle that ships an entity alongside the relation
    # pointing at it still imports cleanly.
    _check_graph_integrity(kb_dir, incoming, issues)

    return ImportCheckResult(
        ok=not issues,
        bundle_id=bundle_id,
        new_files=new_files,
        conflicts=conflicts,
        identical=identical,
        issues=issues,
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
                    f"refusing to import: hash mismatch at write time: {member.name}"
                )
            val_issues: list[str] = []
            _validate_content(member.name, body, val_issues)
            if val_issues:
                skipped.append(member.name)
                continue
            dest.write_bytes(body)
            written.append(member.name)
    result = {
        "bundle_id": check.bundle_id,
        "written": written,
        "skipped_conflicts": skipped,
        "identical": check.identical,
        "on_conflict": on_conflict,
    }
    audit.log_event(
        kb_dir,
        event="bundle.import",
        actor=actor,
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
