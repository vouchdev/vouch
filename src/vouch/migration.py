"""On-disk format migration for vouch KBs.

Each `Migration` covers exactly one version step (from_version → to_version)
and carries a list of `Transform` callables. A `Transform` receives a raw
YAML-parsed dict and the artifact subdirectory name and returns the
(possibly mutated) dict. Transforms operate on raw dicts before Pydantic
deserialisation so they remain correct even after old model classes are
removed.

`migrate_kb()` is the public entry point:

    result = migrate_kb(project_root, from_v="0.1", to_v="0.2", dry_run=False)

It chains all intermediate steps, writes migrated files to a temp directory,
validates every file under the target Pydantic models, then atomically
replaces the live `.vouch/` directory. Any failure leaves the original
untouched.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit as _audit
from .models import VOUCH_SCHEMA_VERSION
from .storage import CONFIG_FILENAME, KB_DIRNAME, _yaml_dump, _yaml_load

Transform = Callable[[dict[str, Any], str], dict[str, Any]]

# Subdirectories whose YAML files are migrated by field-level transforms.
# Pages (.md) are included via a separate branch because they carry YAML
# frontmatter rather than being pure YAML files.
MIGRATABLE_YAML_SUBDIRS = (
    "claims", "sources", "entities", "relations",
    "evidence", "sessions", "decided",
)


@dataclass
class Migration:
    from_version: str
    to_version: str
    transforms: list[Transform] = field(default_factory=list)


@dataclass
class MigrateResult:
    from_version: str
    to_version: str
    changed: list[str]
    skipped: list[str]
    dry_run: bool


# Registry of all known version steps, in order. Extend this list whenever
# the on-disk format changes. A multi-hop upgrade (0.1 → 0.2 → 0.3) is
# supported by chaining consecutive Migration entries.
MIGRATIONS: list[Migration] = [
    # No transforms needed for the 0.1 baseline — this entry exists so
    # migrate_kb can find a valid path when from_v == to_v == "0.1".
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _chain(from_v: str, to_v: str) -> list[Migration]:
    """Return the ordered list of Migration steps from from_v to to_v.

    Raises ValueError if no contiguous path exists in MIGRATIONS.
    """
    if from_v == to_v:
        return []
    steps: list[Migration] = []
    current = from_v
    # Build a quick lookup: from_version → Migration
    by_from = {m.from_version: m for m in MIGRATIONS}
    while current != to_v:
        step = by_from.get(current)
        if step is None:
            raise ValueError(
                f"No migration path from {current!r} to {to_v!r}. "
                f"Available steps: {[m.from_version + '->' + m.to_version for m in MIGRATIONS]}"
            )
        steps.append(step)
        current = step.to_version
    return steps


def _apply_transforms(raw: dict[str, Any], subdir: str, steps: list[Migration]) -> dict[str, Any]:
    for step in steps:
        for transform in step.transforms:
            raw = transform(raw, subdir)
    return raw


def _read_page_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a page .md file into (frontmatter_dict, body)."""
    import re
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    return _yaml_load(m.group(1)) or {}, m.group(2)


def _write_page_frontmatter(meta: dict[str, Any], body: str) -> str:
    return f"---\n{_yaml_dump(meta)}---\n{body}"


def _validate_migrated(tmp_dir: Path) -> list[str]:
    """Load every migrated artifact under target Pydantic models. Returns error list."""
    from .bundle import VALIDATORS
    from .storage import _deserialize_page

    errors: list[str] = []
    for sub in MIGRATABLE_YAML_SUBDIRS:
        subdir = tmp_dir / sub
        if not subdir.is_dir():
            continue
        for p in subdir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in (".yaml", ".yml"):
                # source content files — not YAML models
                continue
            if p.parent.name in (s for s in ["sources"]) and p.name != "meta.yaml":
                continue
            validator = VALIDATORS.get(sub)
            if validator is None:
                continue
            try:
                validator(p.read_bytes())
            except Exception as e:
                errors.append(f"{p.relative_to(tmp_dir)}: {e}")

    pages_dir = tmp_dir / "pages"
    if pages_dir.is_dir():
        for p in pages_dir.glob("*.md"):
            try:
                _deserialize_page(p.read_text())
            except Exception as e:
                errors.append(f"pages/{p.name}: {e}")

    return errors


def _atomic_swap(kb_dir: Path, tmp_dir: Path) -> None:
    """Replace kb_dir with tmp_dir atomically.

    Renames kb_dir → kb_dir/../.vouch-pre-migrate, then tmp_dir → kb_dir.
    On POSIX this pair of renames is as atomic as the filesystem allows.
    """
    pre = kb_dir.parent / ".vouch-pre-migrate"
    if pre.exists():
        shutil.rmtree(pre)
    kb_dir.rename(pre)
    try:
        tmp_dir.rename(kb_dir)
    except Exception:
        # Best-effort rollback of the first rename.
        pre.rename(kb_dir)
        raise


def _copy_tree_for_migration(kb_dir: Path, tmp_dir: Path, steps: list[Migration]) -> list[str]:
    """Copy and transform every migratable file from kb_dir into tmp_dir."""
    changed: list[str] = []

    for sub in MIGRATABLE_YAML_SUBDIRS:
        src_sub = kb_dir / sub
        dst_sub = tmp_dir / sub
        if not src_sub.is_dir():
            continue
        dst_sub.mkdir(parents=True, exist_ok=True)
        for p in sorted(src_sub.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src_sub)
            dst = dst_sub / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix in (".yaml", ".yml") and not (
                sub == "sources" and p.name != "meta.yaml"
            ):
                original_text = p.read_text()
                raw = _yaml_load(original_text) or {}
                transformed = _apply_transforms(raw, sub, steps)
                new_text = _yaml_dump(transformed)
                dst.write_text(new_text)
                if new_text != _yaml_dump(_yaml_load(original_text) or {}):
                    changed.append(str(p.relative_to(kb_dir)))
            else:
                dst.write_bytes(p.read_bytes())

    # Pages (frontmatter + body)
    pages_src = kb_dir / "pages"
    pages_dst = tmp_dir / "pages"
    if pages_src.is_dir():
        pages_dst.mkdir(parents=True, exist_ok=True)
        for p in sorted(pages_src.glob("*.md")):
            meta, body = _read_page_frontmatter(p.read_text())
            transformed_meta = _apply_transforms(meta, "pages", steps)
            new_text = _write_page_frontmatter(transformed_meta, body)
            (pages_dst / p.name).write_text(new_text)
            if new_text != _write_page_frontmatter(meta, body):
                changed.append(f"pages/{p.name}")

    # config.yaml — bump schema_version
    cfg_src = kb_dir / CONFIG_FILENAME
    if cfg_src.exists():
        cfg = _yaml_load(cfg_src.read_text()) or {}
        cfg["schema_version"] = steps[-1].to_version if steps else VOUCH_SCHEMA_VERSION
        (tmp_dir / CONFIG_FILENAME).write_text(_yaml_dump(cfg))

    return changed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def migrate_kb(
    root: Path,
    from_v: str,
    to_v: str,
    *,
    dry_run: bool = False,
) -> MigrateResult:
    """Migrate the KB at `root` from `from_v` to `to_v`.

    All-or-nothing: writes to a temp dir, validates, then atomically swaps.
    Rolls back and raises on any failure.

    In dry_run mode, reports what would change without writing anything.
    """
    kb_dir = root / KB_DIRNAME
    if not kb_dir.is_dir():
        raise FileNotFoundError(f"No KB directory found at {kb_dir}")

    steps = _chain(from_v, to_v)
    if not steps:
        return MigrateResult(
            from_version=from_v, to_version=to_v,
            changed=[], skipped=[], dry_run=dry_run,
        )

    # tmp_dir must be a SIBLING of kb_dir, not a child. If it were inside
    # kb_dir, the first rename in _atomic_swap (kb_dir → pre) would make
    # tmp_dir's path vanish before the second rename can move it into place.
    tmp_dir = kb_dir.parent / ".vouch-migrate-tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()

    try:
        changed = _copy_tree_for_migration(kb_dir, tmp_dir, steps)
        errors = _validate_migrated(tmp_dir)
        if errors:
            _audit.log_event(
                kb_dir, event="migration.rollback", actor="vouch-migrate",
                reversible=False, dry_run=dry_run,
                data={"from_version": from_v, "to_version": to_v, "errors": errors},
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError(
                f"Migration validation failed ({len(errors)} error(s)):\n"
                + "\n".join(f"  {e}" for e in errors)
            )

        if dry_run:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return MigrateResult(
                from_version=from_v, to_version=to_v,
                changed=changed, skipped=[], dry_run=True,
            )

        _audit.log_event(
            kb_dir, event="migration.start", actor="vouch-migrate",
            reversible=True,
            data={"from_version": from_v, "to_version": to_v, "files": len(changed)},
        )
        _atomic_swap(kb_dir, tmp_dir)
        # kb_dir now points at the new layout; log to it.
        _audit.log_event(
            kb_dir, event="migration.complete", actor="vouch-migrate",
            reversible=False,
            data={"from_version": from_v, "to_version": to_v, "files": len(changed)},
        )

    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return MigrateResult(
        from_version=from_v, to_version=to_v,
        changed=changed, skipped=[], dry_run=False,
    )


# ---------------------------------------------------------------------------
# Built-in transform factories (used when adding entries to MIGRATIONS)
# ---------------------------------------------------------------------------


def rename_field(subdir: str, *, old: str, new: str) -> Transform:
    """Return a Transform that renames a top-level field in artifacts of `subdir`."""
    def _transform(raw: dict[str, Any], artifact_subdir: str) -> dict[str, Any]:
        if artifact_subdir == subdir and old in raw:
            raw[new] = raw.pop(old)
        return raw
    return _transform


def add_default(subdir: str, *, field_name: str, default: Any) -> Transform:
    """Return a Transform that adds `field_name` with `default` if absent."""
    def _transform(raw: dict[str, Any], artifact_subdir: str) -> dict[str, Any]:
        if artifact_subdir == subdir and field_name not in raw:
            raw[field_name] = default() if callable(default) else default
        return raw
    return _transform
