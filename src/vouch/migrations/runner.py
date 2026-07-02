"""The semver model-schema migration runner: status / plan / apply / rollback /
verify.

The runner walks *consecutive* manifests from the KB's current
``.vouch/schema_version`` toward a target, rewriting one artifact kind per
manifest atomically and journalling the prior content so the step is reversible.
The schema-version stamp is bumped **last**, so an interrupted apply leaves the
KB reporting its prior version with a journal available for rollback.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import audit
from ..models import (
    Claim,
    Entity,
    Evidence,
    Page,
    Relation,
    Session,
    Source,
)
from ..models import (
    ProposalStatus as _ProposalStatus,
)
from ..storage import _FRONTMATTER_RE, KBStore, _yaml_load
from . import journal as journal_mod
from . import schema, semver
from ._legacy import MigrationError
from .journal import JournalEntry
from .manifest import Manifest, default_manifests_dir, load_manifests
from .rewriter import artifact_files, atomic_write_text, transform_text


class CrashSimulated(RuntimeError):
    """Raised by the ``_fail_after`` test hook to emulate a mid-apply crash."""


@dataclass(frozen=True)
class SchemaPlanStep:
    manifest_id: str
    from_version: str
    to_version: str
    artifact: str
    description: str
    changed: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.changed)


@dataclass(frozen=True)
class SchemaPlan:
    current_version: str
    target_version: str
    steps: list[SchemaPlanStep]

    @property
    def needed(self) -> bool:
        return bool(self.steps)


def _resolve_dir(manifests_dir: Path | None) -> Path | None:
    return manifests_dir if manifests_dir is not None else default_manifests_dir()


def _by_from(manifests: list[Manifest]) -> dict[str, Manifest]:
    return {m.from_version: m for m in manifests}


def _latest_reachable(by_from: dict[str, Manifest], current: str) -> str:
    version = current
    while version in by_from:
        version = by_from[version].to_version
    return version


def _changed_files(store: KBStore, manifest: Manifest) -> list[tuple[Path, str, str]]:
    """Files whose content the manifest would actually change: (path, old, new)."""
    out: list[tuple[Path, str, str]] = []
    for path in artifact_files(store.kb_dir, manifest.artifact):
        old = path.read_text(encoding="utf-8")
        new = transform_text(old, manifest.artifact, manifest.transforms)
        if new != old:
            out.append((path, old, new))
    return out


def build_schema_plan(
    store: KBStore, *, to_version: str | None = None, manifests_dir: Path | None = None
) -> SchemaPlan:
    current = schema.read_schema_version(store)
    manifests = load_manifests(_resolve_dir(manifests_dir))
    by_from = _by_from(manifests)
    target = to_version if to_version is not None else _latest_reachable(by_from, current)
    if not semver.is_valid(target):
        raise MigrationError(f"invalid target schema version {target!r}")
    if semver.lt(target, current):
        raise MigrationError(
            f"cannot migrate backwards from schema version {current} to {target}"
        )

    steps: list[SchemaPlanStep] = []
    version = current
    while version != target:
        manifest = by_from.get(version)
        if manifest is None:
            raise MigrationError(f"no migration registered from schema version {version}")
        if semver.lt(target, manifest.to_version):
            raise MigrationError(
                f"no manifest stops at {target}; "
                f"{manifest.manifest_id} jumps {version} -> {manifest.to_version}"
            )
        changed = [str(p.relative_to(store.kb_dir)) for p, _, _ in _changed_files(store, manifest)]
        steps.append(
            SchemaPlanStep(
                manifest_id=manifest.manifest_id,
                from_version=manifest.from_version,
                to_version=manifest.to_version,
                artifact=manifest.artifact,
                description=manifest.description,
                changed=sorted(changed),
            )
        )
        version = manifest.to_version
    return SchemaPlan(current_version=current, target_version=target, steps=steps)


def status(store: KBStore, *, manifests_dir: Path | None = None) -> dict[str, Any]:
    current = schema.read_schema_version(store)
    manifests = load_manifests(_resolve_dir(manifests_dir))
    by_from = _by_from(manifests)
    target = _latest_reachable(by_from, current)
    pending: list[str] = []
    version = current
    while version in by_from and version != target:
        pending.append(by_from[version].manifest_id)
        version = by_from[version].to_version
    journals = [p.name for p in journal_mod.list_journals(store)]
    return {
        "schema_version": current,
        "target_version": target,
        "up_to_date": current == target,
        "pending": pending,
        "applied_journals": journals,
    }


def plan(
    store: KBStore, *, to_version: str | None = None, manifests_dir: Path | None = None
) -> dict[str, Any]:
    sp = build_schema_plan(store, to_version=to_version, manifests_dir=manifests_dir)
    return {
        "schema_version": 1,
        "current_version": sp.current_version,
        "target_version": sp.target_version,
        "needed": sp.needed,
        "total_files": sum(s.file_count for s in sp.steps),
        "steps": [{**asdict(s), "file_count": s.file_count} for s in sp.steps],
    }


def _new_journal_id(index: int) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{ts}-{index:03d}-{uuid.uuid4().hex[:8]}"


def _pending_proposals_guard(store: KBStore) -> None:
    pending = store.list_proposals(_ProposalStatus.PENDING)
    if pending:
        raise MigrationError(
            f"KB has {len(pending)} pending proposal(s); "
            "run `vouch list-pending` and resolve before migrating"
        )


def apply(
    store: KBStore,
    *,
    to_version: str | None = None,
    manifests_dir: Path | None = None,
    actor: str = "vouch",
    _fail_after: int | None = None,
) -> dict[str, Any]:
    _pending_proposals_guard(store)
    sp = build_schema_plan(store, to_version=to_version, manifests_dir=manifests_dir)
    if not sp.needed:
        return {
            "applied": False,
            "from_version": sp.current_version,
            "to_version": sp.current_version,
            "manifests": [],
            "files": 0,
        }
    manifests = {m.manifest_id: m for m in load_manifests(_resolve_dir(manifests_dir))}
    applied: list[str] = []
    total = 0
    written = 0
    for index, step in enumerate(sp.steps):
        manifest = manifests[step.manifest_id]
        changes = _changed_files(store, manifest)
        journal_id = _new_journal_id(index)
        entries = [
            JournalEntry(rel_path=str(path.relative_to(store.kb_dir)), before=old)
            for path, old, _ in changes
        ]
        # Journal first (fsynced) so an interrupted apply is always recoverable.
        journal_mod.write_journal(
            store,
            journal_id,
            {
                "manifest": manifest.manifest_id,
                "from": manifest.from_version,
                "to": manifest.to_version,
            },
            entries,
        )
        for path, _old, new in changes:
            if _fail_after is not None and written >= _fail_after:
                raise CrashSimulated(f"simulated crash after {written} file(s)")
            atomic_write_text(path, new)
            written += 1
        # Version stamp bumped last: a crash above leaves the prior version.
        schema.write_schema_version(store, manifest.to_version)
        audit.log_event(
            store.kb_dir,
            event="kb.migrate.apply",
            actor=actor,
            object_ids=[journal_id],
            reversible=True,
            data={
                "manifest": manifest.manifest_id,
                "from": manifest.from_version,
                "to": manifest.to_version,
                "files": len(changes),
                "journal": journal_id,
            },
        )
        applied.append(manifest.manifest_id)
        total += len(changes)
    return {
        "applied": True,
        "from_version": sp.current_version,
        "to_version": sp.target_version,
        "manifests": applied,
        "files": total,
    }


def rollback(store: KBStore, *, actor: str = "vouch") -> dict[str, Any]:
    path = journal_mod.latest_journal(store)
    if path is None:
        raise MigrationError("no applied migration to roll back")
    header, entries = journal_mod.read_journal(path)
    for entry in entries:
        target = store.kb_dir / entry.rel_path
        if entry.before is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write_text(target, entry.before)
    from_version = str(header.get("from", schema.BASELINE_SCHEMA_VERSION))
    to_version = str(header.get("to", ""))
    schema.write_schema_version(store, from_version)
    path.unlink()
    audit.log_event(
        store.kb_dir,
        event="kb.migrate.rollback",
        actor=actor,
        object_ids=[str(header.get("manifest", ""))],
        reversible=False,
        data={
            "manifest": header.get("manifest"),
            "from": to_version,
            "to": from_version,
            "files": len(entries),
        },
    )
    return {
        "rolled_back": True,
        "from_version": to_version,
        "to_version": from_version,
        "manifest": header.get("manifest"),
        "files": len(entries),
    }


# artifact kind -> (subdir, pydantic model) for verify's per-file load check.
_YAML_MODELS: dict[str, type[BaseModel]] = {
    "claims": Claim,
    "entities": Entity,
    "relations": Relation,
    "evidence": Evidence,
    "sessions": Session,
}


def verify(store: KBStore) -> dict[str, Any]:
    """Parse-load every artifact under the current version; collect any failures."""
    errors: list[dict[str, str]] = []
    checked = 0
    for kind, model in _YAML_MODELS.items():
        for path in artifact_files(store.kb_dir, kind):
            checked += 1
            try:
                model.model_validate(_yaml_load(path.read_text(encoding="utf-8")))
            except Exception as e:
                errors.append({"path": str(path.relative_to(store.kb_dir)), "error": str(e)})
    for path in artifact_files(store.kb_dir, "pages"):
        checked += 1
        try:
            match = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
            front = _yaml_load(match.group(1)) if match else {}
            Page.model_validate({**(front or {}), "body": match.group(2) if match else ""})
        except Exception as e:
            errors.append({"path": str(path.relative_to(store.kb_dir)), "error": str(e)})
    for meta in sorted((store.kb_dir / "sources").glob("*/meta.yaml")):
        checked += 1
        try:
            Source.model_validate(_yaml_load(meta.read_text(encoding="utf-8")))
        except Exception as e:
            errors.append({"path": str(meta.relative_to(store.kb_dir)), "error": str(e)})
    return {
        "schema_version": schema.read_schema_version(store),
        "checked": checked,
        "ok": not errors,
        "errors": errors,
    }
